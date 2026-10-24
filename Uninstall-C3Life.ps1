<#
.SYNOPSIS
  Removes C3 Life from the C3 Life server. Keeps the logs and config unless you add -RemoveData.
  It doesn't touch Windows settings, Dante, R Remote or Companion.
#>
[CmdletBinding()]
param([string]$BoothUser = 'booth', [string]$InstallDir = 'C:\C3Life', [switch]$RemoveData)
$ErrorActionPreference = 'Continue'
$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in an Administrator PowerShell.' }

Stop-ScheduledTask -TaskName 'C3 Life' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'C3 Life' -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*c3life_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-NetFirewallRule -DisplayName 'C3 Life (booth network)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName 'C3 Life Listen (from home)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName 'C3 Life Wing meters' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName 'C3 Life network MIDI (Playback)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$user = Get-LocalUser -Name $BoothUser -ErrorAction SilentlyContinue
if ($user) {
  $prof = Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $user.SID.Value } | Select-Object -First 1
  if ($prof) { Remove-Item (Join-Path $prof.LocalPath 'AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\booth-screen.cmd') -Force -ErrorAction SilentlyContinue }
}
Remove-Item -Recurse -Force (Join-Path $InstallDir 'app'), (Join-Path $InstallDir 'server\c3life_server.py'), (Join-Path $InstallDir 'server\listen.py'), (Join-Path $InstallDir 'server\access.py'), (Join-Path $InstallDir 'server\dante.py'), (Join-Path $InstallDir 'server\nametags.py'), (Join-Path $InstallDir 'server\scripture.py'), (Join-Path $InstallDir 'server\cues.py'), (Join-Path $InstallDir 'server\reports.py'), (Join-Path $InstallDir 'server\pco.py'), (Join-Path $InstallDir 'server\wing.py'), (Join-Path $InstallDir 'server\tio.py'), (Join-Path $InstallDir 'server\monitors.py'), (Join-Path $InstallDir 'server\scan.py'), (Join-Path $InstallDir 'server\hwcheck.py'), (Join-Path $InstallDir 'server\foyer.py'), (Join-Path $InstallDir 'server\rta_inputs.py'), (Join-Path $InstallDir 'server\rta_patch.py'), (Join-Path $InstallDir 'server\dante_names.py'), (Join-Path $InstallDir 'server\lightkey.py'), (Join-Path $InstallDir 'server\rta_live.py'), (Join-Path $InstallDir 'server\midi_in.py'), (Join-Path $InstallDir 'server\desk.py'), (Join-Path $InstallDir 'server\music.py'), (Join-Path $InstallDir 'server\helpers.py'), (Join-Path $InstallDir 'server\health.py'), (Join-Path $InstallDir 'server\ringout.py'), (Join-Path $InstallDir 'server\macros.py'), (Join-Path $InstallDir 'server\audio_engine.py'), (Join-Path $InstallDir 'server\recorder.py'), (Join-Path $InstallDir 'server\propresenter.py'), (Join-Path $InstallDir 'server\power.py'), (Join-Path $InstallDir 'server\chat.py'), (Join-Path $InstallDir 'server\syscheck.py'), (Join-Path $InstallDir 'server\setup.py'), (Join-Path $InstallDir 'server\mdtalk.py'), (Join-Path $InstallDir 'server\shure.py'), (Join-Path $InstallDir 'server\midi_buttons.py'), (Join-Path $InstallDir 'server\netwin.py'), (Join-Path $InstallDir 'server\loadtest.py'), (Join-Path $InstallDir 'server\daily.py'), (Join-Path $InstallDir 'server\bibles') -ErrorAction SilentlyContinue
if ($RemoveData) { Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue }
Write-Host 'C3 Life removed.' $(if (-not $RemoveData) { "Logs and config are still in $InstallDir." })
Write-Host 'The Companion firewall rule and the Dante Virtual Soundcard start-up shortcut were left in place.'
