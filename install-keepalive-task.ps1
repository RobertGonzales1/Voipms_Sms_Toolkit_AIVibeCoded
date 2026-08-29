# install-keepalive-task.ps1 - run the SIP keepalive daemon continuously.
#
# Registers a Scheduled Task that starts at logon and restarts the daemon if it
# ever exits. The daemon must run 24/7 - a registration only lasts as long as
# something is refreshing it.
#
#   powershell -ExecutionPolicy Bypass -File .\install-keepalive-task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install-keepalive-task.ps1 -Uninstall
#
# Note: this runs at LOGON, so the machine must be signed in. To survive a
# reboot with no one logged in, run it as SYSTEM instead - see the README.

param(
    [switch]$Uninstall,
    [switch]$AtStartup
)

$ErrorActionPreference = 'Stop'
$TaskName = 'VoIPms SIP Keepalive'
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script   = Join-Path $Here 'sip_keepalive.py'

if ($Uninstall) {
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed scheduled task '$TaskName'."
    } catch {
        Write-Host "No scheduled task named '$TaskName' was found."
    }
    return
}

if (-not (Test-Path $Script)) { throw "Missing $Script" }
if (-not (Test-Path (Join-Path $Here 'sip_config.json'))) {
    throw "Missing sip_config.json - copy sip_config.example.json and fill it in first."
}

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { $pythonw = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $pythonw) { throw 'python was not found on PATH.' }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Script`"" -WorkingDirectory $Here

$trigger = if ($AtStartup) {
    New-ScheduledTaskTrigger -AtStartup
} else {
    New-ScheduledTaskTrigger -AtLogOn
}

# ExecutionTimeLimit 0 = never kill it. RestartCount keeps it alive across crashes.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Keep it running when the laptop idles or unplugs.
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries     = $false
$settings.IdleSettings.StopOnIdleEnd = $false

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Keeps VoIP.ms subaccounts SIP-registered so inbound SMS is accepted.' `
    -Force | Out-Null

Write-Host "Registered '$TaskName'."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Then check with:    python sip_keepalive.py --status"
