[CmdletBinding()]
param(
    [string]$SshHost = "reminder-vps",
    [string]$DestinationDirectory = (
        Join-Path $env:USERPROFILE "telegram-predictor-bot-backups"
    ),
    [string]$ReminderDestinationDirectory = (
        Join-Path $env:USERPROFILE "telegram-reminder-bot-backups"
    ),
    [string]$PartyathlonDestinationDirectory = (
        Join-Path $env:USERPROFILE "partyathlon-daily-backups"
    ),
    [ValidateRange(1, 3650)]
    [int]$RetentionDays = 14,
    [ValidateRange(1, 720)]
    [int]$MaxBackupAgeHours = 36
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$retentionCutoff = (Get-Date).ToUniversalTime().AddDays(-$RetentionDays)

$backupSources = @(
    [pscustomobject]@{
        Name = "predictor"
        RemoteDirectory = "/opt/telegram-predictor-bot-backups"
        RemoteGlob = "predictor_*.db"
        NamePattern = "^predictor_(?<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.db$"
        TimestampFormat = "yyyy-MM-dd_HH-mm-ss"
        DestinationDirectory = $DestinationDirectory
    },
    [pscustomobject]@{
        Name = "reminder"
        RemoteDirectory = "/opt/telegram-reminder-bot-backups"
        RemoteGlob = "reminders_*.db"
        NamePattern = "^reminders_(?<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_\d+)?\.db$"
        TimestampFormat = "yyyy-MM-dd_HH-mm-ss"
        DestinationDirectory = $ReminderDestinationDirectory
    },
    [pscustomobject]@{
        Name = "partyathlon"
        RemoteDirectory = "/opt/partyathlon-daily-backups"
        RemoteGlob = "partyathlon_*.db"
        NamePattern = "^partyathlon_(?<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.db$"
        TimestampFormat = "yyyy-MM-dd_HH-mm-ss"
        DestinationDirectory = $PartyathlonDestinationDirectory
    }
)

function Write-SyncLog {
    param(
        [Parameter(Mandatory)]
        [string]$LogPath,
        [Parameter(Mandatory)]
        [string]$Message
    )

    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -LiteralPath $LogPath -Value "$timestamp $Message" -Encoding utf8
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

function Get-BackupTimestamp {
    param(
        [Parameter(Mandatory)]
        [string]$FileName,
        [Parameter(Mandatory)]
        [pscustomobject]$Source
    )

    $nameMatch = [regex]::Match(
        $FileName,
        $Source.NamePattern,
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $nameMatch.Success) {
        return $null
    }

    return [DateTime]::ParseExact(
        $nameMatch.Groups["timestamp"].Value,
        $Source.TimestampFormat,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor
            [Globalization.DateTimeStyles]::AdjustToUniversal
    )
}

function Sync-BackupSource {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Source
    )

    New-Item `
        -ItemType Directory `
        -Path $Source.DestinationDirectory `
        -Force | Out-Null
    $resolvedDestination = (
        Resolve-Path -LiteralPath $Source.DestinationDirectory
    ).Path
    $logPath = Join-Path $resolvedDestination "sync.log"
    $temporaryFile = $null
    $temporaryChecksumFile = $null

    try {
        $manifestCommand = (
            "find '$($Source.RemoteDirectory)' -maxdepth 1 -type f " +
            "-name '$($Source.RemoteGlob)' -exec sha256sum -- {} \; | sort -k2"
        )
        $manifestLines = @(
            & ssh.exe `
                -o BatchMode=yes `
                -o ConnectTimeout=30 `
                $SshHost `
                $manifestCommand
        )
        Assert-SuccessfulProcess -Name "ssh ($($Source.Name))"

        $remoteBackups = @()
        $ignoredCount = 0
        foreach ($line in $manifestLines) {
            if ([string]::IsNullOrWhiteSpace($line)) {
                continue
            }
            $manifestMatch = [regex]::Match(
                $line,
                "^(?<hash>[0-9a-f]{64})\s+(?<path>/.+)$"
            )
            if (-not $manifestMatch.Success) {
                throw "The server returned an invalid backup manifest line."
            }

            $remoteHash = $manifestMatch.Groups["hash"].Value
            $remotePath = $manifestMatch.Groups["path"].Value
            $fileName = [IO.Path]::GetFileName($remotePath)
            if ($remotePath -ne "$($Source.RemoteDirectory)/$fileName") {
                throw "The server returned an unexpected backup path."
            }

            $backupTimestamp = Get-BackupTimestamp `
                -FileName $fileName `
                -Source $Source
            if ($null -eq $backupTimestamp) {
                $ignoredCount += 1
                continue
            }
            if ($backupTimestamp -lt $retentionCutoff) {
                continue
            }

            $remoteBackups += [pscustomobject]@{
                Hash = $remoteHash
                Path = $remotePath
                Name = $fileName
                Timestamp = $backupTimestamp
            }
        }

        if ($remoteBackups.Count -eq 0) {
            throw "No current production backups were found on the server."
        }
        $newestBackup = $remoteBackups |
            Sort-Object Timestamp -Descending |
            Select-Object -First 1
        if (
            $newestBackup.Timestamp -lt
                (Get-Date).ToUniversalTime().AddHours(-$MaxBackupAgeHours)
        ) {
            throw (
                "Newest production backup is older than " +
                "$MaxBackupAgeHours hours: $($newestBackup.Name)."
            )
        }

        $downloadedCount = 0
        foreach ($backup in $remoteBackups) {
            $destinationFile = Join-Path $resolvedDestination $backup.Name
            $requiresDownload = $true
            if (Test-Path -LiteralPath $destinationFile) {
                $localHash = (
                    Get-FileHash -LiteralPath $destinationFile -Algorithm SHA256
                ).Hash
                if ($localHash.Equals(
                    $backup.Hash,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    $requiresDownload = $false
                }
            }

            if ($requiresDownload) {
                $temporaryFile = (
                    "$destinationFile.$([Guid]::NewGuid().ToString('N')).tmp"
                )
                & scp.exe `
                    -p `
                    -o BatchMode=yes `
                    -o ConnectTimeout=30 `
                    ("${SshHost}:$($backup.Path)") `
                    $temporaryFile
                Assert-SuccessfulProcess -Name "scp ($($Source.Name))"

                $downloadedHash = (
                    Get-FileHash -LiteralPath $temporaryFile -Algorithm SHA256
                ).Hash
                if (-not $downloadedHash.Equals(
                    $backup.Hash,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    throw "SHA-256 verification failed for $($backup.Name)."
                }

                Move-Item `
                    -LiteralPath $temporaryFile `
                    -Destination $destinationFile `
                    -Force
                $temporaryFile = $null
                $downloadedCount += 1
            }

            $checksumFile = "$destinationFile.sha256"
            $temporaryChecksumFile = (
                "$checksumFile.$([Guid]::NewGuid().ToString('N')).tmp"
            )
            [IO.File]::WriteAllText(
                $temporaryChecksumFile,
                "$($backup.Hash.ToLowerInvariant())  $($backup.Name)`n",
                [Text.Encoding]::ASCII
            )
            Move-Item `
                -LiteralPath $temporaryChecksumFile `
                -Destination $checksumFile `
                -Force
            $temporaryChecksumFile = $null
        }

        $removedCount = 0
        Get-ChildItem -LiteralPath $resolvedDestination -File |
            ForEach-Object {
                $backupTimestamp = Get-BackupTimestamp `
                    -FileName $_.Name `
                    -Source $Source
                if (
                    $null -ne $backupTimestamp -and
                    $backupTimestamp -lt $retentionCutoff
                ) {
                    if ($_.DirectoryName -ne $resolvedDestination) {
                        throw (
                            "Refusing to remove a backup outside the " +
                            "destination directory."
                        )
                    }
                    Remove-Item -LiteralPath $_.FullName -Force
                    $checksumFile = "$($_.FullName).sha256"
                    if (Test-Path -LiteralPath $checksumFile) {
                        Remove-Item -LiteralPath $checksumFile -Force
                    }
                    $removedCount += 1
                }
            }

        Write-SyncLog -LogPath $logPath -Message (
            "Backup sync succeeded: source=$($Source.Name), " +
            "downloaded=$downloadedCount, available=$($remoteBackups.Count), " +
            "removed=$removedCount, ignored=$ignoredCount."
        )
    } finally {
        if ($null -ne $temporaryFile -and (Test-Path -LiteralPath $temporaryFile)) {
            $temporaryDirectory = [IO.Path]::GetDirectoryName(
                [IO.Path]::GetFullPath($temporaryFile)
            )
            if ($temporaryDirectory -eq $resolvedDestination) {
                Remove-Item -LiteralPath $temporaryFile -Force
            }
        }
        if (
            $null -ne $temporaryChecksumFile -and
            (Test-Path -LiteralPath $temporaryChecksumFile)
        ) {
            $temporaryChecksumDirectory = [IO.Path]::GetDirectoryName(
                [IO.Path]::GetFullPath($temporaryChecksumFile)
            )
            if ($temporaryChecksumDirectory -eq $resolvedDestination) {
                Remove-Item -LiteralPath $temporaryChecksumFile -Force
            }
        }
    }
}

$failures = @()
foreach ($source in $backupSources) {
    try {
        Sync-BackupSource -Source $source
    } catch {
        $failures += "$($source.Name): $($_.Exception.Message)"
        $destination = $source.DestinationDirectory
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        $failureLog = Join-Path (
            (Resolve-Path -LiteralPath $destination).Path
        ) "sync.log"
        Write-SyncLog -LogPath $failureLog -Message (
            "Backup sync failed: source=$($source.Name), " +
            "error=$($_.Exception.Message)"
        )
    }
}

if ($failures.Count -gt 0) {
    throw "Backup synchronization failed: $($failures -join '; ')"
}
