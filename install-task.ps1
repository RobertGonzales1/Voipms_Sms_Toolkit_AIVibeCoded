# install-task.ps1 - register the VoIP.ms watchdog as a Windows Scheduled Task.
#
# Runs 'check' every 6 hours and pops a toast/message box only when something is wrong.
# Run this from an ELEVATED PowerShell prompt, or drop -RunLevel if you prefer.
#
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall

param(
    [int]$IntervalHours = 6,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'VoIPms SMS Watchdog'
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner   = Join-Path $Here 'run-check.ps1'

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed scheduled task '$TaskName'."
    } catch {
        Write-Host "No scheduled task named '$TaskName' was found."
    }
    return
}

if (-not (Test-Path $Runner)) { throw "Missing $Runner" }

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python was not found on PATH.' }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $Here

# Start 5 minutes from now, then repeat indefinitely.
$startAt = (Get-Date).AddMinutes(5)
$trigger = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Checks VoIP.ms balance and per-DID SMS forwarding settings for drift.' `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - runs every $IntervalHours hour(s), first run at $startAt."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName '$TaskName'"
