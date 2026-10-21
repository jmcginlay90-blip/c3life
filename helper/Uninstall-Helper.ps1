<#
Takes the C3 Life helper off this PC: stops it, removes its logon task and firewall rule. Its key is kept unless
-RemoveKey, so installing it again later doesn't need the key typed on the C3 Life server again.
  Double-click Uninstall-Helper.cmd, or:  powershell -ExecutionPolicy Bypass -File Uninstall-Helper.ps1 [-Yes] [-RemoveKey]
#>
param([switch]$Yes, [switch]$RemoveKey, [string]$User = '')
$name = 'C3 Life helper'
if (-not $Yes) {
  $a = Read-Host 'Stop the C3 Life helper and stop it starting at logon? [y/N]'
  if ($a -notmatch '^[yY]') { Write-Host 'Nothing changed.'; exit 0 }
}
$appData = $env:LOCALAPPDATA
if ($User -and $User -ne [System.Security.Principal.WindowsIdentity]::GetCurrent().Name) {
  $sid = (New-Object System.Security.Principal.NTAccount($User)).Translate([System.Security.Principal.SecurityIdentifier]).Value
  $appData = Join-Path (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath 'AppData\Local'
}
$dir = Join-Path $appData 'C3 Life Helper'
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*C3 Life Helper*c3life_helper.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item -Force (Join-Path $dir 'c3life_helper.py') -ErrorAction SilentlyContinue
if ($RemoveKey) { Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue; Write-Host 'Removed, with its key.' }
else { Write-Host "Removed. Its key is kept in $dir\helper.json for next time." }
if (-not $Yes) { Read-Host 'Press Enter to close this window' | Out-Null }
