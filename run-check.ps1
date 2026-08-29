# run-check.ps1 - wrapper the Scheduled Task calls.
# Runs the watchdog and only bothers you when the exit code says something is wrong.

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

# If you are not using machine-level environment variables, uncomment and set these:
# $env:VOIPMS_API_USERNAME = 'you@example.com'
# $env:VOIPMS_API_PASSWORD = 'your-api-password'

$output = & python (Join-Path $Here 'voipms_watch.py') check 2>&1 | Out-String
$code   = $LASTEXITCODE

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path (Join-Path $Here 'watch-runs.log') -Value "=== $stamp (exit $code) ===`r`n$output" -Encoding utf8

if ($code -eq 0) { exit 0 }

$title = if ($code -ge 2) { 'VoIP.ms SMS: CRITICAL' } else { 'VoIP.ms SMS: warning' }

# Prefer a toast; fall back to a message box on systems without the BurntToast module.
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $texts = $xml.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($xml.CreateTextNode($title))  | Out-Null
    $texts.Item(1).AppendChild($xml.CreateTextNode(($output -split "`n" | Select-Object -First 6) -join ' ')) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('VoIP.ms Watchdog').Show($toast)
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($output, $title) | Out-Null
}

exit $code
