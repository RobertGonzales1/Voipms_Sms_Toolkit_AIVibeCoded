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

# A boot trigger is useless under the interactive user - the task would sit
# waiting for a logon that never comes on an unattended box. Run it as SYSTEM.
if ($AtStartup) {
    if ($pythonw -like "$env:USERPROFILE*" -or $pythonw -like '*\Users\*') {
        Write-Warning "Python is installed per-user ($pythonw)."
        Write-Warning 'SYSTEM cannot reach it. Reinstall Python for all users, or use logon mode instead.'
    }
    if (-not (Test-Path (Join-Path $Here 'sip_config.json'))) {
        throw 'sip_config.json is required before installing.'
    }
    # SYSTEM does not see User-scoped environment variables, so passwords must
    # be reachable another way.
    $cfg = Get-Content -Raw (Join-Path $Here 'sip_config.json') | ConvertFrom-Json
    $missingPasswords = @($cfg.accounts | Where-Object { -not $_.password })
    if ($missingPasswords.Count -gt 0) {
        Write-Warning 'Some accounts have no password in sip_config.json, so they rely on'
        Write-Warning 'VOIPMS_SIP_PASSWORD_* environment variables. Running as SYSTEM, only'
        Write-Warning "MACHINE-scoped variables are visible. Set them with:"
        Write-Warning "  [Environment]::SetEnvironmentVariable('VOIPMS_SIP_PASSWORD_LABEL','pw','Machine')"
    }
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

$register = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $trigger
    Settings    = $settings
    Description = 'Keeps VoIP.ms subaccounts SIP-registered so inbound SMS is accepted.'
    Force       = $true
}
if ($AtStartup) {
    $register.User     = 'SYSTEM'
    $register.RunLevel = 'Highest'
}

Register-ScheduledTask @register | Out-Null

Write-Host "Registered '$TaskName'$(if ($AtStartup) { ' (runs as SYSTEM at boot)' } else { ' (runs at logon)' })."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Then check with:    python sip_keepalive.py --status"
