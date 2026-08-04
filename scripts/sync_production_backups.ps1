[CmdletBinding()]
param(
    [string]$SshHost = "reminder-vps",
    [string]$DestinationDirectory = (
        Join-Path $env:USERPROFILE "telegram-predictor-bot-backups"
    ),
    [ValidateRange(1, 3650)]
    [int]$RetentionDays = 14
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$remoteBackupDirectory = "/opt/telegram-predictor-bot-backups"
$backupNamePattern = "^predictor_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.db$"
$temporaryFile = $null
$retentionCutoff = (Get-Date).ToUniversalTime().AddDays(-$RetentionDays)

New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
$resolvedDestination = (Resolve-Path -LiteralPath $DestinationDirectory).Path
$logPath = Join-Path $resolvedDestination "sync.log"

function Write-SyncLog {
    param(
        [Parameter(Mandatory)]
        [string]$Message
    )

    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -LiteralPath $logPath -Value "$timestamp $Message" -Encoding utf8
}

function Assert-SuccessfulProcess {
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    if ($LASTEXITCODE -ne 0) {
        throw "$Name exited with code $LASTEXITCODE."
    }
}

try {
    $manifestCommand = (
        "find '$remoteBackupDirectory' -maxdepth 1 -type f " +
        "-name 'predictor_*.db' -exec sha256sum -- {} \; | sort -k2"
    )
    $manifestLines = @(
        & ssh.exe -o BatchMode=yes -o ConnectTimeout=30 $SshHost $manifestCommand
    )
    Assert-SuccessfulProcess -Name "ssh"

    $remoteBackups = @()
    foreach ($line in $manifestLines) {
        if ($line -notmatch "^(?<hash>[0-9a-f]{64})\s+(?<path>/.+)$") {
            throw "The server returned an invalid backup manifest line."
        }

        $remoteHash = $Matches.hash
        $remotePath = $Matches.path
        $fileName = [IO.Path]::GetFileName($remotePath)
        if (
            $fileName -notmatch $backupNamePattern -or
            $remotePath -ne "$remoteBackupDirectory/$fileName"
        ) {
            throw "The server returned an unexpected backup path."
        }

        $backupTimestamp = [DateTime]::ParseExact(
            $fileName.Substring(10, 19),
            "yyyy-MM-dd_HH-mm-ss",
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal -bor
                [Globalization.DateTimeStyles]::AdjustToUniversal
        )
        if ($backupTimestamp -lt $retentionCutoff) {
            continue
        }

        $remoteBackups += [pscustomobject]@{
            Hash = $remoteHash
            Path = $remotePath
            Name = $fileName
        }
    }

    if ($remoteBackups.Count -eq 0) {
        throw "No production backups were found on the server."
    }

    $downloadedCount = 0
    foreach ($backup in $remoteBackups) {
        $destinationFile = Join-Path $resolvedDestination $backup.Name
        if (Test-Path -LiteralPath $destinationFile) {
            $localHash = (Get-FileHash -LiteralPath $destinationFile -Algorithm SHA256).Hash
            if ($localHash.Equals($backup.Hash, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
        }

        $temporaryFile = "$destinationFile.$([Guid]::NewGuid().ToString('N')).tmp"
        & scp.exe -p -o BatchMode=yes -o ConnectTimeout=30 (
            "${SshHost}:$($backup.Path)"
        ) $temporaryFile
        Assert-SuccessfulProcess -Name "scp"

        $downloadedHash = (
            Get-FileHash -LiteralPath $temporaryFile -Algorithm SHA256
        ).Hash
        if (-not $downloadedHash.Equals(
            $backup.Hash,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "SHA-256 verification failed for $($backup.Name)."
        }

        Move-Item -LiteralPath $temporaryFile -Destination $destinationFile -Force
        $temporaryFile = $null
        $downloadedCount += 1
    }

    $removedCount = 0
    Get-ChildItem -LiteralPath $resolvedDestination -Filter "predictor_*.db" -File |
        Where-Object { $_.LastWriteTimeUtc -lt $retentionCutoff } |
        ForEach-Object {
            if ($_.DirectoryName -ne $resolvedDestination) {
                throw "Refusing to remove a backup outside the destination directory."
            }
            Remove-Item -LiteralPath $_.FullName -Force
            $removedCount += 1
        }

    Write-SyncLog (
        "Backup sync succeeded: downloaded=$downloadedCount, " +
        "available=$($remoteBackups.Count), removed=$removedCount."
    )
} catch {
    if ($null -ne $temporaryFile -and (Test-Path -LiteralPath $temporaryFile)) {
        $temporaryDirectory = [IO.Path]::GetDirectoryName(
            [IO.Path]::GetFullPath($temporaryFile)
        )
        if ($temporaryDirectory -eq $resolvedDestination) {
            Remove-Item -LiteralPath $temporaryFile -Force
        }
    }

    Write-SyncLog "Backup sync failed: $($_.Exception.Message)"
    throw
}
