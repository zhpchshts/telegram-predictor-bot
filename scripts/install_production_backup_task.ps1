[CmdletBinding()]
param(
    [string]$TaskName = "Klever Production Backup Sync",
    [datetime]$DailyAt = "08:30"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$syncScript = Join-Path $PSScriptRoot "sync_production_backups.ps1"
if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
    throw "Backup synchronization script was not found: $syncScript"
}

$powershellPath = Join-Path $PSHOME "powershell.exe"
$actionArguments = (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
    "-File `"$syncScript`""
)
$action = New-ScheduledTaskAction `
    -Execute $powershellPath `
    -Argument $actionArguments
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description (
        "Downloads verified Klever production database backups from the VPS " +
        "and keeps them locally for 14 days."
    ) `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName
