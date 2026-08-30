# start.ps1 - run the VoIP.ms SIP keepalive and watch it live, in one window.
#
# Starts the keepalive daemon, then renders per-number registration status until
# you press Ctrl+C - at which point the daemon is asked to shut down cleanly and
# deregister.
#
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -RefreshSeconds 5
#
# If a daemon is already running (for example the Scheduled Task), this attaches
# to it and only displays - it will not start a second one. Registering the same
# subaccount twice is exactly what VoIP.ms warns against.

param(
    [int]$RefreshSeconds = 2
)

$ErrorActionPreference = 'Stop'

$Here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script     = Join-Path $Here 'sip_keepalive.py'
$ConfigPath = Join-Path $Here 'sip_config.json'
$StatusPath = Join-Path $Here 'sip_status.json'
$StopFlag   = Join-Path $Here 'sip_stop.flag'
$OutLog     = Join-Path $Here 'sip_stdout.log'
$ErrLog     = Join-Path $Here 'sip_stderr.log'

# Status older than this means whoever was writing it has stopped.
$StaleSeconds = 180

$Proc         = $null
$StartedByUs  = $false
$Mutex        = $null
$HoldsMutex   = $false


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

function Write-Cell {
    param([string]$Text, [int]$Width, [string]$Color = 'Gray')
    $out = $Text.PadRight($Width)
    if ($out.Length -gt $Width) { $out = $out.Substring(0, $Width) }
    Write-Host $out -ForegroundColor $Color -NoNewline
}

function Get-Status {
    if (-not (Test-Path $StatusPath)) { return $null }
    try {
        return Get-Content -Raw -Path $StatusPath -ErrorAction Stop | ConvertFrom-Json
    } catch {
        # A read landing mid-write is normal; the caller keeps the previous frame.
        return $null
    }
}

function Get-StatusAge {
    param($Payload)
    if ($null -eq $Payload) { return $null }
    try { return [int]((Get-Date) - [datetime]$Payload.updated).TotalSeconds } catch { return $null }
}

function Test-ExistingDaemon {
    # True only if the status file is fresh AND the pid it names is still a
    # python process. Windows recycles PIDs, so a bare id match can point at an
    # unrelated process and make us refuse to start while showing a stale file.
    $payload = Get-Status
    if ($null -eq $payload) { return $false }

    $age = Get-StatusAge $payload
    if ($null -eq $age -or $age -gt $StaleSeconds) { return $false }

    if ($payload.pid) {
        $running = Get-Process -Id $payload.pid -ErrorAction SilentlyContinue
        if (-not $running) { return $false }
        if ($running.ProcessName -notmatch 'python') { return $false }
    }
    return $true
}

function Show-Tail {
    param([string]$Path, [int]$Lines = 8)
    if (-not (Test-Path $Path)) { return }
    $content = Get-Content -Path $Path -Tail $Lines -ErrorAction SilentlyContinue
    foreach ($line in $content) { Write-Host "      $line" -ForegroundColor DarkYellow }
}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

function Show-Dashboard {
    $payload = Get-Status
    if ($null -eq $payload) { return }

    $age      = Get-StatusAge $payload
    $accounts = @($payload.accounts)
    $ok       = @($accounts | Where-Object { $_.registered }).Count
    $total    = $accounts.Count

    # Clear-Host throws when output is redirected (no real console buffer).
    try { Clear-Host } catch { }
    Write-Host ''
    Write-Host '  VoIP.ms SIP Keepalive' -ForegroundColor White -NoNewline
    Write-Host ('  ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')).PadLeft(49) -ForegroundColor DarkGray
    Write-Host ('  ' + ('=' * 76)) -ForegroundColor DarkGray

    Write-Host '  daemon: ' -ForegroundColor Gray -NoNewline
    if ($null -ne $Proc -and $Proc.HasExited) {
        Write-Host 'EXITED' -ForegroundColor Red -NoNewline
        Write-Host "  (exit code $($Proc.ExitCode))" -ForegroundColor DarkGray
    } elseif ($null -eq $age) {
        Write-Host 'UNKNOWN' -ForegroundColor Yellow
    } elseif ($age -gt $StaleSeconds) {
        Write-Host 'STOPPED' -ForegroundColor Red -NoNewline
        Write-Host "  (status is ${age}s old - registrations are expiring)" -ForegroundColor DarkGray
    } else {
        Write-Host 'RUNNING' -ForegroundColor Green -NoNewline
        $owner = if ($StartedByUs) { 'this window' } else { 'another process' }
        Write-Host "  (pid $($payload.pid), started by $owner, updated ${age}s ago)" -ForegroundColor DarkGray
    }
    Write-Host ''

    Write-Cell '  PHONE NUMBER' 20 'DarkGray'
    Write-Cell 'LABEL' 14 'DarkGray'
    Write-Cell 'SUBACCOUNT' 18 'DarkGray'
    Write-Cell 'STATUS' 16 'DarkGray'
    Write-Cell 'RENEWS' 8 'DarkGray'
    Write-Host 'SMS' -ForegroundColor DarkGray
    Write-Host ('  ' + ('-' * 76)) -ForegroundColor DarkGray

    foreach ($acct in $accounts) {
        # The daemon pre-formats these, so number formatting lives in one place.
        # @(...) is required: PowerShell unwraps a single-element array into a
        # bare string, and indexing a string yields characters, not the number.
        $numbers = @(
            if ($acct.display) { $acct.display }
            else { '(no DID set)' }
        )

        for ($i = 0; $i -lt $numbers.Count; $i++) {
            if ($i -gt 0) {
                # Extra numbers on the same subaccount, listed underneath.
                Write-Host ('  ' + $numbers[$i]) -ForegroundColor White
                continue
            }
            Write-Cell ('  ' + $numbers[$i]) 20 'White'
            Write-Cell $acct.label 14 'Gray'
            Write-Cell $acct.account 18 'DarkGray'
            if ($acct.registered) {
                Write-Cell 'REGISTERED' 16 'Green'
                # Prefer the absolute deadline so the countdown moves every
                # refresh rather than jumping when status is next written.
                $remaining = $acct.seconds_until_expiry
                if ($acct.expires_at) {
                    try {
                        $live = [int](([datetime]$acct.expires_at) - (Get-Date)).TotalSeconds
                        if ($live -ge 0) { $remaining = $live }
                    } catch { }
                }
                Write-Cell ("{0}s" -f $remaining) 8 'DarkGray'
            } else {
                Write-Cell 'NOT REGISTERED' 16 'Red'
                Write-Cell '--' 8 'DarkGray'
            }
            Write-Host $acct.messages_received -ForegroundColor DarkGray
        }

        if (-not $acct.registered -and $acct.last_error) {
            Write-Host ('      ' + $acct.last_error) -ForegroundColor DarkYellow
        }
    }

    Write-Host ('  ' + ('=' * 76)) -ForegroundColor DarkGray
    Write-Host '  ' -NoNewline
    $summaryColor = if ($total -gt 0 -and $ok -eq $total) { 'Green' } else { 'Red' }
    Write-Host "$ok of $total registered" -ForegroundColor $summaryColor -NoNewline

    $note = if ($StartedByUs) { 'Ctrl+C to stop the daemon and exit' } else { 'Ctrl+C to close this view' }
    Write-Host ('  ' + $note).PadLeft(76 - "$ok of $total registered".Length) -ForegroundColor DarkGray
    Write-Host ''
}


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------

try { $Host.UI.RawUI.WindowTitle = 'VoIP.ms SIP Keepalive' } catch { }

if (-not (Test-Path $Script))     { throw "Missing $Script" }
if (-not (Test-Path $ConfigPath)) {
    throw "Missing sip_config.json - copy sip_config.example.json and fill it in first."
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python was not found on PATH.' }

# Clear any stop flag left behind by a previous hard kill.
if (Test-Path $StopFlag) { Remove-Item $StopFlag -Force -ErrorAction SilentlyContinue }

# Serialise the check-then-start below. Without this, two near-simultaneous
# launches both see no daemon and both start one - registering the same
# subaccount twice, which is precisely what VoIP.ms warns breaks SMS delivery.
$Mutex = New-Object System.Threading.Mutex($false, 'Global\VoIPmsSipKeepalive')
try {
    $HoldsMutex = $Mutex.WaitOne(5000)
} catch [System.Threading.AbandonedMutexException] {
    # A previous holder was killed without releasing; we inherit it cleanly.
    $HoldsMutex = $true
}

if (-not $HoldsMutex) {
    Write-Host ''
    Write-Host '  Another start.ps1 is launching the daemon right now.' -ForegroundColor Yellow
    Write-Host '  Wait a moment and run this again.' -ForegroundColor Gray
    Write-Host ''
    exit 3
}

if (Test-ExistingDaemon) {
    Write-Host ''
    Write-Host '  A keepalive daemon is already running.' -ForegroundColor Yellow
    Write-Host '  Attaching to it - this window will display only, and will not' -ForegroundColor Gray
    Write-Host '  start a second daemon or stop the existing one on exit.' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  (Registering the same subaccount twice breaks SMS delivery.)' -ForegroundColor DarkGray
    Write-Host ''
    Start-Sleep -Seconds 3
} else {
    Write-Host ''
    Write-Host '  Starting keepalive daemon...' -ForegroundColor Gray
    if (Test-Path $StatusPath) { Remove-Item $StatusPath -Force -ErrorAction SilentlyContinue }

    $Proc = Start-Process -FilePath $python `
        -ArgumentList "`"$Script`"" `
        -WorkingDirectory $Here `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog
    $StartedByUs = $true

    # Give it a moment to register and write its first status file.
    $waited = 0
    while ($waited -lt 20 -and -not (Test-Path $StatusPath)) {
        if ($Proc.HasExited) { break }
        Start-Sleep -Milliseconds 500
        $waited++
    }

    if ($Proc.HasExited) {
        Write-Host ''
        # Start-Process -PassThru does not always populate ExitCode until the
        # process has been waited on, even once it has exited.
        try { $Proc.WaitForExit(2000) | Out-Null } catch { }
        $code = if ($null -ne $Proc.ExitCode) { $Proc.ExitCode } else { 'unknown' }
        Write-Host "  Daemon exited immediately (exit $code):" -ForegroundColor Red
        Show-Tail $ErrLog
        Show-Tail $OutLog
        Write-Host ''
        exit 3
    }
}


# The check-then-start critical section is over; another window may now attach.
if ($HoldsMutex) {
    try { $Mutex.ReleaseMutex() } catch { }
    $HoldsMutex = $false
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

try {
    while ($true) {
        Show-Dashboard

        if ($null -ne $Proc -and $Proc.HasExited) {
            Write-Host '  The daemon stopped unexpectedly. Last output:' -ForegroundColor Red
            Show-Tail $ErrLog
            Show-Tail $OutLog
            Write-Host ''
            break
        }

        Start-Sleep -Seconds $RefreshSeconds
    }
} finally {
    if ($StartedByUs -and $null -ne $Proc -and -not $Proc.HasExited) {
        Write-Host ''
        Write-Host '  Stopping daemon (deregistering)...' -ForegroundColor Gray

        # Ask for a clean shutdown so each subaccount deregisters properly.
        New-Item -ItemType File -Path $StopFlag -Force | Out-Null

        $waited = 0
        while ($waited -lt 40 -and -not $Proc.HasExited) {
            Start-Sleep -Milliseconds 500
            $waited++
        }

        if (-not $Proc.HasExited) {
            Write-Host '  Clean stop timed out - forcing.' -ForegroundColor Yellow
            Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $StopFlag -Force -ErrorAction SilentlyContinue
        Write-Host '  Stopped.' -ForegroundColor Gray
        Write-Host ''
    }
}
