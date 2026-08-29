# dashboard.ps1 - live registration status for every VoIP.ms number.
#
# Reads sip_status.json, which the keepalive daemon rewrites every 15 seconds.
# Works whether the daemon runs in a console or as a hidden Scheduled Task.
#
#   powershell -ExecutionPolicy Bypass -File .\dashboard.ps1
#   powershell -ExecutionPolicy Bypass -File .\dashboard.ps1 -Once
#   powershell -ExecutionPolicy Bypass -File .\dashboard.ps1 -RefreshSeconds 5

param(
    [int]$RefreshSeconds = 2,
    [switch]$Once
)

$Here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$StatusPath = Join-Path $Here 'sip_status.json'

# Anything older than this means the daemon stopped writing.
$StaleSeconds = 180

function Format-Did {
    param([string]$Did)
    if ([string]::IsNullOrWhiteSpace($Did)) { return '' }
    $digits = ($Did -replace '\D', '')
    if ($digits.Length -eq 11 -and $digits.StartsWith('1')) { $digits = $digits.Substring(1) }
    if ($digits.Length -eq 10) {
        return '({0}) {1}-{2}' -f $digits.Substring(0,3), $digits.Substring(3,3), $digits.Substring(6,4)
    }
    return $Did
}

function Write-Cell {
    param([string]$Text, [int]$Width, [string]$Color = 'Gray', [switch]$NoPad)
    $out = if ($NoPad) { $Text } else { $Text.PadRight($Width) }
    if ($out.Length -gt $Width -and -not $NoPad) { $out = $out.Substring(0, $Width) }
    Write-Host $out -ForegroundColor $Color -NoNewline
}

function Show-Dashboard {
    $lines = @()

    if (-not (Test-Path $StatusPath)) {
        Clear-Host
        Write-Host ''
        Write-Host '  VoIP.ms SIP Registration' -ForegroundColor White
        Write-Host ''
        Write-Host '  No sip_status.json found.' -ForegroundColor Yellow
        Write-Host '  The keepalive daemon has not run yet. Start it with:' -ForegroundColor Gray
        Write-Host ''
        Write-Host '      python sip_keepalive.py' -ForegroundColor Cyan
        Write-Host ''
        return
    }

    try {
        $payload = Get-Content -Raw -Path $StatusPath -ErrorAction Stop | ConvertFrom-Json
    } catch {
        # A read that lands mid-write is normal; keep the previous frame.
        return
    }

    $age = $null
    try { $age = [int]((Get-Date) - [datetime]$payload.updated).TotalSeconds } catch { }

    $accounts = @($payload.accounts)
    $registered = @($accounts | Where-Object { $_.registered }).Count
    $total = $accounts.Count

    Clear-Host
    Write-Host ''
    Write-Host '  VoIP.ms SIP Registration' -ForegroundColor White -NoNewline
    Write-Host ('  ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')).PadLeft(46) -ForegroundColor DarkGray
    Write-Host ('  ' + ('=' * 76)) -ForegroundColor DarkGray

    # Daemon health line
    Write-Host '  daemon: ' -ForegroundColor Gray -NoNewline
    if ($null -eq $age) {
        Write-Host 'UNKNOWN' -ForegroundColor Yellow
    } elseif ($age -gt $StaleSeconds) {
        Write-Host "STOPPED" -ForegroundColor Red -NoNewline
        Write-Host "  (status is ${age}s old - registrations are expiring)" -ForegroundColor DarkGray
    } else {
        Write-Host 'RUNNING' -ForegroundColor Green -NoNewline
        Write-Host "  (pid $($payload.pid), updated ${age}s ago)" -ForegroundColor DarkGray
    }
    Write-Host ''

    # Header
    Write-Cell '  PHONE NUMBER' 20 'DarkGray'
    Write-Cell 'LABEL' 14 'DarkGray'
    Write-Cell 'SUBACCOUNT' 18 'DarkGray'
    Write-Cell 'STATUS' 16 'DarkGray'
    Write-Cell 'RENEWS' 8 'DarkGray'
    Write-Host 'SMS' -ForegroundColor DarkGray
    Write-Host ('  ' + ('-' * 76)) -ForegroundColor DarkGray

    foreach ($acct in $accounts) {
        $dids = @($acct.dids)
        # @(...) is required: PowerShell unwraps a single-element array into a
        # bare string, and indexing a string yields characters, not the number.
        $numbers = @(
            if ($dids.Count -gt 0) { $dids | ForEach-Object { Format-Did $_ } }
            else { '(no DID set)' }
        )

        for ($i = 0; $i -lt $numbers.Count; $i++) {
            if ($i -eq 0) {
                Write-Cell ('  ' + $numbers[$i]) 20 'White'
                Write-Cell $acct.label 14 'Gray'
                Write-Cell $acct.account 18 'DarkGray'

                if ($acct.registered) {
                    Write-Cell 'REGISTERED' 16 'Green'
                    Write-Cell ("{0}s" -f $acct.seconds_until_expiry) 8 'DarkGray'
                    Write-Host $acct.messages_received -ForegroundColor DarkGray
                } else {
                    Write-Cell 'NOT REGISTERED' 16 'Red'
                    Write-Cell '--' 8 'DarkGray'
                    Write-Host $acct.messages_received -ForegroundColor DarkGray
                }
            } else {
                # Extra DIDs on the same subaccount, listed underneath.
                Write-Cell ('  ' + $numbers[$i]) 20 'White'
                Write-Host ''
            }
        }

        if (-not $acct.registered -and $acct.last_error) {
            Write-Host ('      ' + $acct.last_error) -ForegroundColor DarkYellow
        }
    }

    Write-Host ('  ' + ('=' * 76)) -ForegroundColor DarkGray
    $summaryColor = if ($registered -eq $total -and $total -gt 0) { 'Green' } else { 'Red' }
    Write-Host '  ' -NoNewline
    Write-Host "$registered of $total registered" -ForegroundColor $summaryColor -NoNewline
    if (-not $Once) {
        Write-Host '                                   Ctrl+C to exit' -ForegroundColor DarkGray
    } else {
        Write-Host ''
    }
    Write-Host ''
}

if ($Once) {
    Show-Dashboard
    return
}

try {
    while ($true) {
        Show-Dashboard
        Start-Sleep -Seconds $RefreshSeconds
    }
} finally {
    # Leave the cursor somewhere sane after Ctrl+C.
    Write-Host ''
}
