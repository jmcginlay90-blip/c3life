<#
.SYNOPSIS
  Checks the C3 Life server is set up right. Changes nothing.

.DESCRIPTION
  Use it after setting up (guide Step 9), after every monthly Windows update, and whenever something seems off.
  Each line says OK, CHECK (worth a look) or PROBLEM (fix before a service). Ends with how many of each.
  Works in a normal or an Administrator PowerShell.

.EXAMPLE
  Set-ExecutionPolicy -Scope Process Bypass -Force
  .\Check-C3LifeServer.ps1
#>
[CmdletBinding()]
param(
  [string]$ComputerName = 'C3LIFE',
  [string]$BoothIp = '192.168.10.10',
  [string]$BoothUser = 'booth',
  [int]$Port = 80,
  [string]$SmartPlug = '192.168.10.5',
  [string]$InstallDir = 'C:\C3Life'
)
$ErrorActionPreference = 'Continue'
$results = New-Object System.Collections.ArrayList
function Out-Check([string]$area, [string]$state, [string]$detail) {
  [void]$results.Add([pscustomobject]@{ Area = $area; State = $state; Detail = $detail })
  $c = @{ OK = 'Green'; CHECK = 'Yellow'; PROBLEM = 'Red' }[$state]
  Write-Host ('{0,-8} {1,-28} {2}' -f $state, $area, $detail) -ForegroundColor $c
}
function Get-RegVal($path, $name) { try { (Get-ItemProperty -Path $path -Name $name -ErrorAction Stop).$name } catch { $null } }

Write-Host ''
Write-Host "C3 Life server check  $(Get-Date -Format 'ddd d MMM yyyy HH:mm')" -ForegroundColor Cyan
Write-Host ''

# Windows and the PC
$os = Get-CimInstance Win32_OperatingSystem
if ($os.Caption -match 'LTSC|LTSB|IoT') { Out-Check 'Windows' 'OK' $os.Caption } else { Out-Check 'Windows' 'CHECK' "$($os.Caption). LTSC 2021 is recommended for this PC" }
if ($env:COMPUTERNAME -eq $ComputerName) { Out-Check 'PC name' 'OK' "$ComputerName (phones use c3life.local)" } else { Out-Check 'PC name' 'PROBLEM' "$env:COMPUTERNAME. Should be $ComputerName" }
$bios = Get-CimInstance Win32_BIOS
Out-Check 'BIOS version' 'OK' "$($bios.SMBIOSBIOSVersion) ($($bios.ReleaseDate.ToString('MMM yyyy'))). Check AC Recovery = Power On after any BIOS update"
$up = (Get-Date) - $os.LastBootUpTime
Out-Check 'Last start-up' 'OK' ("{0:dd MMM HH:mm} ({1} days ago)" -f $os.LastBootUpTime, [int]$up.TotalDays)

# Sign-in
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
if ((Get-RegVal $wl 'AutoAdminLogon') -eq '1' -and (Get-RegVal $wl 'DefaultUserName') -eq $BoothUser) { Out-Check 'Automatic sign-in' 'OK' "as $BoothUser" }
else { Out-Check 'Automatic sign-in' 'PROBLEM' "Not set for $BoothUser. Run Sysinternals Autologon (guide Step 3)" }

# Network
$ip = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $BoothIp -ErrorAction SilentlyContinue
if ($ip) {
  $nic = Get-NetAdapter -InterfaceIndex $ip.InterfaceIndex
  Out-Check 'Booth network address' 'OK' "$BoothIp on $($nic.Name), $($nic.LinkSpeed)"
  $prof = Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue
  if ($prof.NetworkCategory -eq 'Private') { Out-Check 'Network type' 'OK' 'Private' } else { Out-Check 'Network type' 'PROBLEM' "$($prof.NetworkCategory). Dante and phones need Private. Run Setup-C3LifeServer.ps1. If it goes back to Public after a restart: secpol.msc > Network List Manager Policies > Unidentified Networks > Location type = Private" }
  if ($nic.LinkSpeed -notmatch '1 Gbps') { Out-Check 'Link speed' 'CHECK' "$($nic.LinkSpeed). Expected 1 Gbps: check cable C01 and switch port P1" }
  $eee = Get-NetAdapterAdvancedProperty -Name $nic.Name -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Energy.?Efficient|Green Ethernet|\bEEE\b' }
  $eeeOn = @($eee | Where-Object { $_.DisplayValue -notmatch '^(Off|Disabled)$' })
  if ($eeeOn.Count) { Out-Check 'Energy Efficient Ethernet' 'PROBLEM' "On ($($eeeOn[0].DisplayName)). Dante can drop out. Run Setup-C3LifeServer.ps1" } else { Out-Check 'Energy Efficient Ethernet' 'OK' 'Off' }
} else { Out-Check 'Booth network address' 'PROBLEM' "$BoothIp isn't on any adapter. Check cable C01, or run Setup-C3LifeServer.ps1" }
$net = Test-NetConnection -ComputerName 1.1.1.1 -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($net) { Out-Check 'Internet' 'OK' 'Working (Planning Center and reports can sync)' } else { Out-Check 'Internet' 'CHECK' 'None right now. Fine for a service; reports wait on the drive' }

# Power and sleep
$scheme = (& powercfg /getactivescheme) -join ' '
if ($scheme -match '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c') { Out-Check 'Power plan' 'OK' 'High performance' } else { Out-Check 'Power plan' 'CHECK' 'Not High performance. Run Setup-C3LifeServer.ps1' }
$sleep = (& powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -join ' '
if ($sleep -match 'Current AC Power Setting Index:\s+0x00000000') { Out-Check 'Sleep' 'OK' 'Never' } else { Out-Check 'Sleep' 'PROBLEM' 'The PC can go to sleep. Run Setup-C3LifeServer.ps1' }
if ((Get-RegVal 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled') -eq 0) { Out-Check 'Fast start-up' 'OK' 'Off' } else { Out-Check 'Fast start-up' 'CHECK' 'On. Run Setup-C3LifeServer.ps1' }
$bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
if ($bat) {
  $st = @{ 1 = 'on battery (power cut?)'; 2 = 'on mains' }[[int]$bat.BatteryStatus]
  if (-not $st) { $st = "status $($bat.BatteryStatus)" }
  if ([int]$bat.BatteryStatus -eq 1) { Out-Check 'UPS' 'PROBLEM' "$st, $($bat.EstimatedChargeRemaining)% left" } else { Out-Check 'UPS' 'OK' "$st, $($bat.EstimatedChargeRemaining)% charged" }
} else { Out-Check 'UPS' 'CHECK' 'Not seen. Plug the UPS''s USB lead into the PC' }
if (Test-Connection -ComputerName $SmartPlug -Count 1 -Quiet -ErrorAction SilentlyContinue) { Out-Check 'Smart plug' 'OK' "Answering at $SmartPlug" } else { Out-Check 'Smart plug' 'CHECK' "No answer at $SmartPlug (the hard reset won't work)" }

# Windows Update
$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
if ((Get-RegVal $au 'NoAutoUpdate') -eq 1) { Out-Check 'Windows Update' 'OK' 'Automatic updates off. Update by hand monthly' }
elseif ((Get-RegVal $au 'AUOptions') -eq 2) { Out-Check 'Windows Update' 'OK' 'Notify only. Update by hand monthly' }
else { Out-Check 'Windows Update' 'PROBLEM' 'Windows can download and restart by itself. Run Setup-C3LifeServer.ps1' }
if ((Get-RegVal $au 'NoAutoRebootWithLoggedOnUsers') -ne 1) { Out-Check 'Update restarts' 'PROBLEM' 'Windows may restart while signed in' }
if ((Get-RegVal $wu 'ExcludeWUDriversInQualityUpdate') -ne 1) { Out-Check 'Driver updates' 'CHECK' 'Windows Update may change drivers' }
try {
  $last = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().QueryHistory(0, 1) | Select-Object -First 1
  if ($last) {
    $age = [int]((Get-Date) - $last.Date).TotalDays
    if ($age -gt 45) { Out-Check 'Last update' 'CHECK' ("{0:d MMM yyyy} ({1} days ago). Do the monthly update" -f $last.Date, $age) } else { Out-Check 'Last update' 'OK' ("{0:d MMM yyyy} ({1} days ago)" -f $last.Date, $age) }
  }
} catch { }
$def = Get-MpComputerStatus -ErrorAction SilentlyContinue
if ($def) {
  $dAge = [int]((Get-Date) - $def.AntivirusSignatureLastUpdated).TotalDays
  if ($def.RealTimeProtectionEnabled -and $dAge -le 14) { Out-Check 'Antivirus' 'OK' "On, definitions $dAge days old" } else { Out-Check 'Antivirus' 'CHECK' "Real-time $($def.RealTimeProtectionEnabled), definitions $dAge days old" }
}

# Booth software
$pf = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
function Find-Exe([string[]]$subdirs, [string]$pattern) {
  foreach ($r in $pf) { foreach ($d in $subdirs) { $dir = Join-Path $r $d; if (Test-Path $dir) { $h = Get-ChildItem -Path $dir -Recurse -Depth 3 -Filter $pattern -ErrorAction SilentlyContinue | Select-Object -First 1; if ($h) { return $h.FullName } } } }
  return ''
}
foreach ($s in @(@('Dante Controller', @('Audinate'), 'DanteController.exe'), @('R Remote', @('Yamaha'), '*Remote*.exe'), @('Companion', @('Companion', 'Bitfocus'), '*Companion*.exe'))) {
  if (Find-Exe $s[1] $s[2]) { Out-Check $s[0] 'OK' 'Installed' } else { Out-Check $s[0] 'PROBLEM' 'Not installed (guide Step 5)' }
}
$dvsProc = Get-Process | Where-Object { $_.ProcessName -match 'Dante Virtual Soundcard|^DVS' } | Select-Object -First 1
$dvsSvc = Get-Service | Where-Object { $_.DisplayName -match 'Dante Virtual Soundcard|Dante Audio' } | Select-Object -First 1
if ($dvsProc -or ($dvsSvc -and $dvsSvc.Status -eq 'Running')) { Out-Check 'Dante Virtual Soundcard' 'OK' 'Running' }
elseif (Find-Exe @('Audinate') 'Dante Virtual Soundcard*.exe') { Out-Check 'Dante Virtual Soundcard' 'PROBLEM' 'Installed but not running: the RTA and MD log get no audio. Open it and click Start' }
else { Out-Check 'Dante Virtual Soundcard' 'PROBLEM' 'Not installed (guide Step 5)' }
$comp = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($comp) { Out-Check 'Companion running' 'OK' 'http://c3life.local:8000' } else { Out-Check 'Companion running' 'CHECK' 'Nothing on port 8000. Open Companion' }

# C3 Life
$task = Get-ScheduledTask -TaskName 'C3 Life' -ErrorAction SilentlyContinue
if ($task) { Out-Check 'C3 Life start-up' 'OK' "Starts when $BoothUser signs in" } else { Out-Check 'C3 Life start-up' 'PROBLEM' 'Not installed. Run Install-C3Life.ps1' }
$utask = Get-ScheduledTask -TaskName 'C3 Life Update' -ErrorAction SilentlyContinue
if (-not $utask) { Out-Check 'Updates' 'CHECK' 'The update task is missing. Run Install-C3Life.ps1 again' }
elseif (-not (Test-Path 'C:\C3Life\secrets\github-key.dat')) { Out-Check 'Updates' 'CHECK' 'No GitHub key yet, so C3 Life can''t look for updates. Run .\Set-GitHubKey.ps1 (you type the key)' }
else {
  $ust = $null; try { $ust = Get-Content 'C:\C3Life\data\update\status.json' -Raw | ConvertFrom-Json } catch { }
  if ($ust -and $ust.ok -eq $false) { Out-Check 'Updates' 'CHECK' $ust.error }
  elseif ($ust -and $ust.checked) { Out-Check 'Updates' 'OK' ("Looks once a day; last looked {0}{1}" -f $ust.checked, $(if ($ust.behind) { '. A new version is ready (Saved setup > Updates)' } else { '' })) }
  else { Out-Check 'Updates' 'OK' 'Key set; it looks for the first time tonight' }
}
try {
  $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
  Out-Check 'C3 Life' 'OK' ("Running, version {0}, up {1} min" -f $h.version, [int]($h.uptime_s / 60))
  if ($h.sound.kind -eq 'asio') {
    if ($h.sound.status -eq 'running') { Out-Check 'Sound (DVS in ASIO)' 'OK' ("{0} channels: Listen, the RTA, the MD log and recording" -f $h.sound.channels) }
    else { Out-Check 'Sound (DVS in ASIO)' 'PROBLEM' ("Not hearing DVS: {0}. In Dante Virtual Soundcard: Stop, Settings > Audio Interface = ASIO, 64 x 64, Start" -f $h.sound.reason) }
    if ($h.sound.channels -and $h.sound.channels -lt 64) { Out-Check 'DVS channels' 'CHECK' ("{0}. Set 64 x 64 in DVS's Settings to record up to 64" -f $h.sound.channels) }
  }
} catch { Out-Check 'C3 Life' 'PROBLEM' "Not answering on port $Port. C3 Life server power > Restart C3 Life, or restart the PC" }
# Listen
$cfgFile = Join-Path $InstallDir 'server\config.json'
if (Test-Path $cfgFile) {
  $cf = Get-Content $cfgFile -Raw | ConvertFrom-Json
  if ($cf.ffmpeg -and (Test-Path $cf.ffmpeg)) { Out-Check 'Listen: ffmpeg' 'OK' $cf.ffmpeg } else { Out-Check 'Listen: ffmpeg' 'PROBLEM' 'Not found: Listen has no sound. winget install Gyan.FFmpeg --scope machine, then Install-C3Life.ps1' }
  if ($cf.audio_engine -ne 'asio') {  # in ASIO mode C3 Life opens DVS itself (checked above)
    if ($cf.listen_device) { Out-Check 'Listen: sound device' 'OK' $cf.listen_device } else { Out-Check 'Listen: sound device' 'CHECK' 'DVS device not set. Start DVS, then run Install-C3Life.ps1 again' }
  }
  if ($cf.captions_model -and (Test-Path (Join-Path $cf.captions_model 'am'))) { Out-Check 'Listen: subtitles' 'OK' $cf.captions_model } else { Out-Check 'Listen: subtitles' 'CHECK' 'Speech model not installed: subtitles are off. Run Install-C3Life.ps1 again (needs internet once)' }
  $tr = @(Get-ChildItem (Join-Path $InstallDir 'translate') -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'metadata.json') })
  if ($tr.Count) { Out-Check 'Listen: translation' 'OK' ("Languages: " + ($tr.Name -join ', ')) } else { Out-Check 'Listen: translation' 'OK' 'No languages installed (Install-C3Life.ps1 -Languages es,fa)' }
  $py = (Get-Command py -ErrorAction SilentlyContinue).Source
  if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
  if ($py) {
    $na = & $py -m netaudio --json --timeout 3 subscription list 2>$null | Out-String
    $dvs = if ($cf.dante_dvs_name) { $cf.dante_dvs_name } else { $env:COMPUTERNAME }
    if ($LASTEXITCODE -eq 0 -and $na -match [regex]::Escape($dvs)) { Out-Check 'Dante names (netaudio)' 'OK' "Reads the routes into $dvs" }
    elseif ($LASTEXITCODE -eq 0) { Out-Check 'Dante names (netaudio)' 'CHECK' "No routes into $dvs found. Is DVS running, and named $dvs in Dante Controller?" }
    else { Out-Check 'Dante names (netaudio)' 'CHECK' 'netaudio not installed: run Install-C3Life.ps1 again (Listen uses saved names meanwhile)' }
  }
  # Name tags: Planning Center token, and ProPresenter's network API with its props
  # signed in from the app (data\pco.json) or by the installer (config.json)
  $pf2 = Join-Path $InstallDir 'data\pco.json'
  if (Test-Path $pf2) { $pj2 = Get-Content $pf2 -Raw | ConvertFrom-Json; if ($pj2.app_id) { $cf | Add-Member -Force pco_app_id $pj2.app_id; $cf | Add-Member -Force pco_secret $pj2.secret } elseif ($pj2.signed_out) { $cf | Add-Member -Force pco_app_id '' } }
  $tzName = (Get-TimeZone).Id
  Out-Check 'Time zone' 'OK' "$tzName (Planning Center times are shown in this time zone)"
  if ($cf.pco_app_id -and $cf.pco_secret) {
    try {
      $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($cf.pco_app_id):$($cf.pco_secret)"))
      $null = Invoke-RestMethod -Uri 'https://api.planningcenteronline.com/services/v2/service_types?per_page=1' -Headers @{ Authorization = "Basic $auth" } -TimeoutSec 8
      Out-Check 'Name tags: Planning Center' 'OK' 'Token works'
    } catch { Out-Check 'Name tags: Planning Center' 'PROBLEM' 'Token refused or no internet. Make a new Personal Access Token and run Install-C3Life.ps1 -PlanningCenterAppId ... -PlanningCenterSecret ...' }
  } else { Out-Check 'Name tags: Planning Center' 'CHECK' 'Not set up (Help > Set up name tags)' }
  $pp = if ($cf.propresenter) { $cf.propresenter.TrimEnd('/') } else { 'http://192.168.10.30:50001' }
  try {
    $props = @(Invoke-RestMethod -Uri "$pp/v1/props" -TimeoutSec 4)
    $msgs = @(Invoke-RestMethod -Uri "$pp/v1/messages" -TimeoutSec 4)
    $tag = $msgs | Where-Object { $_.id.name -eq $(if ($cf.nametag_message) { $cf.nametag_message } else { 'Name tag' }) }
    Out-Check 'Name tags: ProPresenter' $(if ($tag) { 'OK' } else { 'CHECK' }) ("{0} props{1}" -f $props.Count, $(if ($tag) { ', Name tag message found' } else { '. No "Name tag" message: guests and couples can''t be shown' }))
  } catch { Out-Check 'Name tags: ProPresenter' 'CHECK' "No answer from $pp. Is ProPresenter open with Settings > Network > Enable Network on?" }
  # The Wing: address, Watch only / Control, and does it answer (OSC on UDP 2223)
  $wj = Join-Path (Split-Path $cfgFile) 'data\wing.json'
  $wset = if (Test-Path $wj) { Get-Content $wj -Raw | ConvertFrom-Json } else { $null }
  $wip = if ($wset -and $wset.ip) { $wset.ip } elseif ($cf.wing_ip) { $cf.wing_ip } else { '' }
  $wmode = if ($wset -and $wset.mode) { $wset.mode } elseif ($wip) { 'watch' } else { 'off' }
  if (-not $wip) { Out-Check 'Wing' 'CHECK' 'No address yet. Mixer > The Wing''s address > Find the Wing, or run Install-C3Life.ps1 -WingIp 192.168.10.20' }
  else {
    $u = New-Object System.Net.Sockets.UdpClient
    try {
      $u.Client.ReceiveTimeout = 1500
      $q = [byte[]](0x2f,0x3f,0,0,0x2c,0,0,0)   # OSC "/?" with no arguments
      [void]$u.Send($q, $q.Length, $wip, 2223)
      $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
      $ans = [Text.Encoding]::ASCII.GetString($u.Receive([ref]$ep))
      $info = ($ans -csplit 'WING,', 2)[1]
      $t = if ($info) { $info.Trim([char]0) -split ',' } else { @() }
      Out-Check 'Wing' 'OK' ("{0} at {1}, firmware {2}. App mode: {3}" -f $(if ($t.Count -gt 1) { $t[1] } else { 'Wing' }), $wip, $(if ($t.Count -gt 4) { $t[4] } else { '?' }), $wmode)
    } catch { Out-Check 'Wing' 'PROBLEM' "No answer from $wip on UDP 2223. Is the Wing on, on the booth network, and is the address right (Wing: Setup > Network)?" }
    finally { $u.Close() }
    if (-not (Get-NetFirewallRule -DisplayName 'C3 Life Wing meters' -ErrorAction SilentlyContinue)) { Out-Check 'Wing meters' 'CHECK' 'Firewall rule missing: meters may not arrive. Run Install-C3Life.ps1 again.' }
  }
  # The Tio1608-D: does it answer Yamaha's control protocol (TCP 49280)?
  $tj = Join-Path (Split-Path $cfgFile) 'data\tio.json'
  $tset = if (Test-Path $tj) { Get-Content $tj -Raw | ConvertFrom-Json } else { $null }
  $tip = if ($tset -and $tset.ip) { $tset.ip } elseif ($cf.tio_ip) { $cf.tio_ip } else { '' }
  if (-not $tip) { Out-Check 'Tio1608-D' 'CHECK' 'No address yet (Mixer > Tio1608-D card, or Install-C3Life.ps1 -TioIp 192.168.10.41). Use R Remote meanwhile.' }
  else {
    $tc = New-Object System.Net.Sockets.TcpClient
    try {
      if (-not $tc.ConnectAsync($tip, 49280).Wait(2000)) { throw 'timeout' }
      $st = $tc.GetStream(); $st.ReadTimeout = 2000
      $q = [Text.Encoding]::ASCII.GetBytes("get IO:Current/InCh/HAGain 0 0`n"); $st.Write($q, 0, $q.Length)
      $buf = New-Object byte[] 256; $n = $st.Read($buf, 0, 256)
      $ans = [Text.Encoding]::ASCII.GetString($buf, 0, $n)
      if ($ans -match '^OK get IO:Current/InCh/HAGain 0 0 (-?\d+)') { Out-Check 'Tio1608-D' 'OK' ("Answers at {0}. Input 1 gain {1} dB" -f $tip, $Matches[1]) }
      else { Out-Check 'Tio1608-D' 'CHECK' "Answered at $tip but not as expected: $($ans.Trim())" }
    } catch { Out-Check 'Tio1608-D' 'PROBLEM' "No answer from $tip on TCP 49280. Is the Tio on, and on the Dante network (primary port)?" }
    finally { $tc.Close() }
  }
  # Bible Scripture: which versions are set up, and the Scripture looks in ProPresenter
  $vers = @(@('KJV', 'WEBBE') | Where-Object { Test-Path (Join-Path $InstallDir "server\bibles\$_.txt.gz") })
  if ($cf.esv_key) { $vers += 'ESV' }
  if ($cf.nlt_key) { $vers += 'NLT' }
  if ($cf.apibible_key -and $cf.apibible_ids -and $cf.apibible_ids.NIV) { $vers += 'NIV' }
  Out-Check 'Bible Scripture: versions' $(if ($vers -contains 'KJV') { 'OK' } else { 'PROBLEM' }) ($(if ($vers.Count) { $vers -join ', ' } else { 'None: run Install-C3Life.ps1 again' }))
  try {
    $looks = @(Invoke-RestMethod -Uri "$pp/v1/messages" -TimeoutSec 4 | Where-Object { $_.id.name -like 'Scripture*' })
    Out-Check 'Bible Scripture: looks' $(if ($looks.Count) { 'OK' } else { 'CHECK' }) $(if ($looks.Count) { ($looks | ForEach-Object { $_.id.name }) -join ', ' } else { 'No ProPresenter message starts with "Scripture" (Help > Set up Bible Scripture)' })
  } catch { Out-Check 'Bible Scripture: looks' 'CHECK' "No answer from $pp" }
  $pf = Join-Path $InstallDir 'data\listen-public.json'
  if (Test-Path $pf) { $pj = Get-Content $pf -Raw | ConvertFrom-Json; Out-Check 'Listen: public feed' 'OK' $(if ($pj.host) { "Set up for $($pj.host). Starts only when someone presses Start" } else { 'Not set up' }) } else { Out-Check 'Listen: public feed' 'OK' 'Not set up' }
  if ($cf.remote_networks -and @($cf.remote_networks).Count) {
    $ts = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '100.*' } | Select-Object -First 1
    if ($ts) { Out-Check 'Listen from home' 'OK' "On, through Tailscale ($($ts.IPAddress))" } else { Out-Check 'Listen from home' 'PROBLEM' 'On, but Tailscale isn''t connected on this PC' }
  } else { Out-Check 'Listen from home' 'OK' 'Off' }
  # Issue reports: can they be emailed, and are any waiting?
  $waiting = @(Get-ChildItem (Join-Path $InstallDir 'data\reports') -Filter report.json -Recurse -ErrorAction SilentlyContinue | Where-Object { (Get-Content $_.FullName -Raw | ConvertFrom-Json).email -ne 'sent' }).Count
  if ($cf.smtp_host) { Out-Check 'Issue reports' $(if ($waiting) { 'CHECK' } else { 'OK' }) ("Email through $($cf.smtp_host)" + $(if ($waiting) { ", $waiting waiting to send (no internet, or the email account said no)" } else { '' })) }
  else { Out-Check 'Issue reports' 'CHECK' ("Email not set up (Install-C3Life.ps1 -SmtpHost ... -SmtpUser ...)" + $(if ($waiting) { ", $waiting waiting" } else { '' })) }
  # Remote support (whole app for Admins through Tailscale) and the remote desktop
  if ($cf.remote_support_roles -and @($cf.remote_support_roles).Count) {
    $ts = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '100.*' } | Select-Object -First 1
    if ($ts) { Out-Check 'Remote support' 'OK' "On for $(@($cf.remote_support_roles) -join ', '): http://$($ts.IPAddress)" } else { Out-Check 'Remote support' 'PROBLEM' 'On, but Tailscale isn''t connected on this PC' }
    $tsExe = "$env:ProgramFiles\Tailscale\tailscale.exe"
    if (Test-Path $tsExe) {
      $prefs = & $tsExe debug prefs 2>$null | Out-String
      if ($prefs -match '"ForceDaemon":\s*true') { Out-Check 'Tailscale after a restart' 'OK' 'Run unattended is on' } else { Out-Check 'Tailscale after a restart' 'CHECK' 'Turn on Run unattended in the Tailscale menu, or nobody can reach the PC after a restart' }
    }
  } else { Out-Check 'Remote support' 'OK' 'Off (Install-C3Life.ps1 -RemoteSupport to turn on)' }
  $crd = Get-Service -Name chromoting -ErrorAction SilentlyContinue
  if ($crd -and $crd.Status -eq 'Running') { Out-Check 'Remote desktop' 'OK' 'Chrome Remote Desktop is running' }
  elseif ($crd) { Out-Check 'Remote desktop' 'CHECK' 'Chrome Remote Desktop is installed but not running' }
  else { Out-Check 'Remote desktop' 'OK' 'Not set up (Help > Fix things from home)' }
}
foreach ($r in 'C3 Life (booth network)', 'Companion (booth network)') {
  $fw = Get-NetFirewallRule -DisplayName $r -ErrorAction SilentlyContinue
  if ($fw -and $fw.Enabled -eq 'True') { Out-Check "Firewall: $($r -replace ' \(booth network\)','')" 'OK' 'Open to the booth network only' } else { Out-Check "Firewall: $($r -replace ' \(booth network\)','')" 'PROBLEM' 'Phones can''t reach it. Run Install-C3Life.ps1' }
}

# Storage and clock
$c = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$free = [math]::Round(100 * $c.FreeSpace / $c.Size)
if ($free -ge 15) { Out-Check 'Storage' 'OK' ("{0}% free of {1:N0} GB" -f $free, ($c.Size / 1GB)) } else { Out-Check 'Storage' 'PROBLEM' "$free% free" }
$disk = Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object -First 1
if ($disk) { if ($disk.HealthStatus -eq 'Healthy') { Out-Check 'SSD health' 'OK' 'Healthy' } else { Out-Check 'SSD health' 'PROBLEM' "$($disk.HealthStatus). Back up and replace the SSD" } }
if ((Get-Date).Year -lt 2025) { Out-Check 'Clock' 'PROBLEM' "The date is $(Get-Date -Format d). The coin battery (B1) may be flat" } else { Out-Check 'Clock' 'OK' (Get-Date -Format 'HH:mm, d MMM yyyy') }

# Summary
$n = $results | Group-Object State -AsHashTable -AsString
Write-Host ''
$ok = if ($n['OK']) { $n['OK'].Count } else { 0 }
$chk = if ($n['CHECK']) { $n['CHECK'].Count } else { 0 }
$bad = if ($n['PROBLEM']) { $n['PROBLEM'].Count } else { 0 }
$colour = if ($bad) { 'Red' } elseif ($chk) { 'Yellow' } else { 'Green' }
Write-Host ("{0} OK, {1} to check, {2} problems" -f $ok, $chk, $bad) -ForegroundColor $colour
$report = Join-Path $InstallDir ('logs\check-{0:yyyy-MM-dd-HHmm}.csv' -f (Get-Date))
try { New-Item -ItemType Directory -Force -Path (Split-Path $report) | Out-Null; $results | Export-Csv -Path $report -NoTypeInformation; Write-Host "Saved to $report" } catch { }
if ($bad) { exit 1 }
