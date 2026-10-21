<#
Installs the C3 Life helper on this Windows PC, so it starts by itself (hidden) whenever this person logs in, and
restarts if it stops. No window to keep open. Run it again to update the helper (the key stays the same).

  Double-click Install-Helper.cmd (next to this file). Windows asks once for permission (for the firewall rule).
  Or in PowerShell:  powershell -ExecutionPolicy Bypass -File Install-Helper.ps1 [-Yes]

It copies c3life_helper.py to %LOCALAPPDATA%\C3 Life Helper, opens TCP port 8099 for private networks only
("C3 Life helper"), adds a task that starts it at logon ("C3 Life helper"), starts it, and shows its key once.
On Windows the helper can press Spotify's Play/Pause, Next and Previous, and open ProPresenter.
To take it off again: Uninstall-Helper.cmd
#>
param(
  [switch]$Yes,              # don't ask
  [string]$User = ''         # who it runs for (default: whoever ran this, even when Windows elevated it)
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = 'C3 Life helper'

function Done([int]$code = 0) {
  if (-not $Yes) { Write-Host ''; Read-Host 'Press Enter to close this window' | Out-Null }
  exit $code
}

try {
  Write-Host 'C3 Life helper for this PC'
  Write-Host 'It lets the C3 Life server press Play/Pause, Next and Previous for Spotify here, and open ProPresenter.'
  Write-Host ''
  if (-not (Test-Path (Join-Path $here 'c3life_helper.py'))) { Write-Host "c3life_helper.py isn't next to this file. Run it from the helper folder."; Done 1 }

  # who it's for: the person logged in (the .cmd passes them in when Windows elevates)
  if (-not $User) { $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name }
  $appData = $env:LOCALAPPDATA
  if ($User -ne [System.Security.Principal.WindowsIdentity]::GetCurrent().Name) {
    $sid = (New-Object System.Security.Principal.NTAccount($User)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $prof = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath
    $appData = Join-Path $prof 'AppData\Local'
  }
  $dir = Join-Path $appData 'C3 Life Helper'

  # Python 3 (pythonw.exe runs it with no window)
  $py = $null
  foreach ($c in @('py', 'python')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
      $exe = & $cmd.Source -3 -c "import sys; print(sys.executable)" 2>$null
      if (-not $exe) { $exe = & $cmd.Source -c "import sys; print(sys.executable)" 2>$null }
      if ($exe -and (Test-Path $exe)) { $py = $exe.Trim(); break }
    }
  }
  if (-not $py) {
    Write-Host "Python 3 isn't installed."
    if (-not $Yes) {
      $a = Read-Host 'Install it now with winget (Python 3.12, from Microsoft''s package list)? [Y/n]'
      if ($a -match '^[nN]') { Write-Host 'Install Python 3 from python.org, then run this again.'; Done 1 }
    }
    winget install --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $py = Get-ChildItem 'C:\Program Files\Python3*\python.exe' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $py) { Write-Host 'Python didn''t install. Install Python 3 from python.org, then run this again.'; Done 1 }
  }
  $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
  if (-not (Test-Path $pyw)) { $pyw = $py }

  if (-not $Yes) {
    $a = Read-Host 'Install the C3 Life helper so it starts by itself at logon? [Y/n]'
    if ($a -match '^[nN]') { Write-Host 'Nothing changed.'; Done 0 }
  }

  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Copy-Item -Force (Join-Path $here 'c3life_helper.py') (Join-Path $dir 'c3life_helper.py')
  $script = Join-Path $dir 'c3life_helper.py'
  $config = Join-Path $dir 'helper.json'
  $key = (& $py $script --config $config --show-key).Trim()   # the same key when it's installed again

  # the firewall: port 8099, private networks only (the booth network), not public Wi-Fi
  Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort 8099 -Profile Private,Domain -Action Allow | Out-Null

  # start at logon, for this person, hidden; restart if it stops
  $act = New-ScheduledTaskAction -Execute $pyw -Argument ('"{0}" --config "{1}"' -f $script, $config) -WorkingDirectory $dir
  $trig = New-ScheduledTaskTrigger -AtLogOn -User $User
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  $prin = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
  Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $name -Action $act -Trigger $trig -Settings $set -Principal $prin `
    -Description 'The C3 Life helper: lets the C3 Life server control Spotify and open ProPresenter on this PC.' | Out-Null
  Start-ScheduledTask -TaskName $name

  $ok = $false
  foreach ($i in 1..20) {
    try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8099/hello).Content -match 'c3life-helper') { $ok = $true; break } } catch { }
    Start-Sleep -Milliseconds 500
  }
  Write-Host ''
  if ($ok) { Write-Host 'Done. The C3 Life helper is running, and starts by itself (hidden) whenever you log in to this PC.' }
  else { Write-Host 'Installed, but it isn''t answering yet. Try logging out and in again.' }
  Write-Host ''
  Write-Host 'Its key (an Admin types it once on the C3 Life server: Show > Music > Computers with the C3 Life helper):'
  Write-Host ''
  Write-Host "    $key"
  Write-Host ''
  Write-Host 'The C3 Life server finds this PC by itself within 5 minutes (or tap Find it now on Music).'
  Write-Host 'Make sure this PC''s network is set to Private (Settings > Network & internet > the connection), or the server can''t reach it.'
  Done 0
} catch {
  Write-Host ''
  Write-Host ("Something went wrong: {0}" -f $_.Exception.Message)
  Done 1
}
