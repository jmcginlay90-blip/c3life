<#
.SYNOPSIS
  Installs C3 Life on the C3 Life server and sets up everything it starts with.

.DESCRIPTION
  Step 5 (C3 Life) and Step 6 of the C3 Life server set-up guide:
    - copies the C3 Life server and app to C:\C3Life
    - starts C3 Life when the booth user signs in (scheduled task, restarts itself if it stops)
    - opens port 80 (C3 Life) and 8000 (Companion) in the firewall, for the booth network only
    - adds Dante Virtual Soundcard and the full-screen booth screen to the booth user's start-up
    - checks Dante Controller, Dante Virtual Soundcard, R Remote and Companion are installed, and records
      where R Remote and Dante Controller are so C3 Life can open them
  It shows what it will do and asks first. Run it again after an update: your settings in config.json are kept.

  Sound: by default C3 Life opens Dante Virtual Soundcard in ASIO mode (64 x 64) for Listen, the RTA, the MD log,
  recording and playback (-AudioEngine asio). Set DVS to ASIO and 64 x 64 in its Settings first.

.EXAMPLE
  Sign in as booth once first (so its profile exists), then sign in as admin and in an Administrator PowerShell:
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\Install-C3Life.ps1
#>
[CmdletBinding()]
param(
  [string]$BoothUser = 'booth',
  [int]$Port = 80,
  [string]$InstallDir = 'C:\C3Life',
  # The booth network. Leave out to use the networks this PC is on (whatever the church's router uses: 192.168.x,
  # 10.x...), and the firewall follows this PC's own network if it ever changes.
  [string]$BoothNetwork = '',
  [switch]$NoKiosk,
  # How C3 Life hears Dante Virtual Soundcard. asio: DVS in ASIO mode, 64 x 64; C3 Life opens it once and shares it with
  # Listen, the RTA, the MD log, recording and playback. wdm: DVS in WDM mode (16 channels), no recording.
  [ValidateSet('asio', 'wdm')][string]$AudioEngine = 'asio',
  [switch]$ListenFromHome,
  # Remote support: Admins can use the whole app from anywhere through Tailscale (everyone else: Listen only).
  [switch]$RemoteSupport,
  [string]$TailscaleNetwork = '100.64.0.0/10',
  [switch]$NoSubtitles,
  # Languages for translated subtitles (Argos Translate codes, for example es = Spanish, fa = Persian, zh = Chinese). '' for none.
  [string]$Languages = 'es',
  [string]$SpeechModel = 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip',
  # Name tags: a Planning Center Personal Access Token (api.planningcenteronline.com > Personal Access Tokens).
  # Leave out to keep what's already set.
  [string]$PlanningCenterAppId = '',
  [string]$PlanningCenterSecret = '',
  # ProPresenter's network API (ProPresenter > Settings > Network > Enable Network).
  [string]$ProPresenter = '',   # leave out: the network scan finds it (and follows it if its address changes)
  # The Behringer Wing's IP address (on the Wing: Setup > Network). Leave out to keep what's set, or set it
  # later on the Mixer screen (Find the Wing). The app starts as Watch only; an Admin switches it to Control.
  [string]$WingIp = '',
  # Music: C3 Life helpers (helper/c3life_helper.py) on the Macs and Windows PCs are found by the network scan, and an
  # Admin types each one's key on Music. Only to set one by hand: its address and key (leave out to keep what's set).
  [string]$Helper = '',
  [string]$HelperKey = '',
  # The Yamaha Tio1608-D's IP address, so C3 Life can change its gain and phantom power (like R Remote).
  [string]$TioIp = '',
  # Bible Scripture: KJV and WEBBE are built in. Keys for the others (leave out to keep what's set):
  [string]$EsvKey = '',        # api.esv.org (free, non-commercial)
  [string]$NltKey = '',        # api.nlt.to
  [string]$ApiBibleKey = '',   # api.bible, with -NivBibleId for the NIV once the church has permission
  [string]$NivBibleId = '',
  # Issue reports: emailed on their own when there's internet. -ReportTo can be changed later in the app.
  [string]$ReportTo = '',
  [string]$SmtpHost = '',      # e.g. smtp.office365.com or smtp.gmail.com
  [int]$SmtpPort = 587,
  [string]$SmtpUser = '',      # the account the reports are sent from
  [string]$SmtpFrom = '',
  # optional: add reports to a monday.com board (Profile > Developers > API token); the board is picked in the app
  [string]$MondayToken = '',
  [switch]$Yes
)

# The booth network: given, or the private networks this PC is on now (each connected adapter's own network).
function Get-OwnNetworks {
  $out = @()
  foreach ($a in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' })) {
    $ip = [System.Net.IPAddress]::Parse($a.IPAddress).GetAddressBytes()
    $b = $ip[0]; $c = $ip[1]
    if (-not ($b -eq 10 -or ($b -eq 172 -and $c -ge 16 -and $c -le 31) -or ($b -eq 192 -and $c -eq 168))) { continue }
    $len = [int]$a.PrefixLength
    $mask = [uint32]0; if ($len -gt 0) { $mask = [uint32]([math]::Pow(2, 32) - [math]::Pow(2, 32 - $len)) }
    $n = ([uint32]$ip[0] -shl 24) -bor ([uint32]$ip[1] -shl 16) -bor ([uint32]$ip[2] -shl 8) -bor [uint32]$ip[3]
    $n = $n -band $mask
    $net = '{0}.{1}.{2}.{3}/{4}' -f (($n -shr 24) -band 255), (($n -shr 16) -band 255), (($n -shr 8) -band 255), ($n -band 255), $len
    if ($out -notcontains $net) { $out += $net }
  }
  return $out
}
if ($BoothNetwork) { $boothNets = @($BoothNetwork); $fwRemote = $BoothNetwork }
else {
  $boothNets = @(Get-OwnNetworks)
  if (-not $boothNets) { $boothNets = @('192.168.10.0/24'); Write-Warning 'No network found on this PC yet: using 192.168.10.0/24. Run this again once it is on the booth network, or give -BoothNetwork.' }
  $fwRemote = 'LocalSubnet'   # Windows' own "this PC's network", which follows it if the network changes
  $BoothNetwork = $boothNets -join ', '
}
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in an Administrator PowerShell.' }
foreach ($need in 'server\c3life_server.py', 'server\listen.py', 'server\access.py', 'server\dante.py', 'server\nametags.py', 'server\scripture.py', 'server\cues.py', 'server\reports.py', 'server\pco.py', 'server\wing.py', 'server\tio.py', 'server\monitors.py', 'server\scan.py', 'server\hwcheck.py', 'server\foyer.py', 'server\rta_inputs.py', 'server\rta_patch.py', 'server\dante_names.py', 'server\lightkey.py', 'server\rta_live.py', 'server\midi_in.py', 'server\desk.py', 'server\music.py', 'server\helpers.py', 'server\health.py', 'server\ringout.py', 'server\macros.py', 'server\audio_engine.py', 'server\recorder.py', 'server\bibles\KJV.txt.gz', 'server\bibles\WEBBE.txt.gz', 'app\player.html') {
  if (-not (Test-Path (Join-Path $here $need))) { throw "Missing $need next to this script. Copy the whole c3life-server folder." }
}

$logDir = Join-Path $InstallDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir ('install-c3life-{0:yyyy-MM-dd-HHmm}.log' -f (Get-Date))) | Out-Null

# ---- Python (runs the C3 Life server) --------------------------------------------------------------------
$py = $null
foreach ($cand in @(@('py', '-3'), @('python'))) {
  $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
  if ($cmd) {
    $extra = @($cand | Select-Object -Skip 1)
    try {
      $exe = & $cmd.Source @extra -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null
      if ($exe -and (Test-Path $exe.Trim())) { $py = $exe.Trim(); break }
    } catch { }
  }
}
if (-not $py) { throw 'Python 3.9 or newer is needed. In an Administrator PowerShell run:  winget install Python.Python.3.12 --scope machine   then run this again.' }
if ($py -like "$env:SystemDrive\Users\*") { throw "Python is installed only for one user ($py), so $BoothUser can't run it. Install it for everyone:  winget install Python.Python.3.12 --scope machine" }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "Python: $py"

# ---- ffmpeg (turns Dante audio into the Listen streams) -------------------------------------------------
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) {
  $ffmpeg = Get-ChildItem -Path "$env:ProgramFiles\ffmpeg", "$env:ProgramFiles\WinGet\Links", "$env:ProgramData\chocolatey\bin" -Recurse -Depth 3 -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
$dvsDevice = ''
if ($ffmpeg) {
  # The Windows sound device Dante Virtual Soundcard makes (its exact name depends on the DVS settings).
  $devs = & $ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1 | Out-String
  $m = [regex]::Matches($devs, '"([^"]*Dante[^"]*)"\s*\(audio\)')
  if ($m.Count -eq 0) { $m = [regex]::Matches($devs, '"([^"]*Dante[^"]*)"') }
  if ($m.Count) { $dvsDevice = $m[0].Groups[1].Value }
  Write-Host "ffmpeg: $ffmpeg"
  if ($AudioEngine -eq 'asio') { Write-Host 'Sound: Dante Virtual Soundcard in ASIO mode (64 x 64), opened by C3 Life itself' }
  elseif ($dvsDevice) { Write-Host "Listen will use the sound device: $dvsDevice" } else { Write-Warning 'No Dante sound device found yet. Start Dante Virtual Soundcard (WDM, 16 x 16), then run this again.' }
} else {
  Write-Warning 'ffmpeg is not installed, so Listen will have no sound. Install it for everyone:  winget install Gyan.FFmpeg --scope machine   then run this again.'
}

# ---- The booth user and its start-up folder --------------------------------------------------------------
$user = Get-LocalUser -Name $BoothUser -ErrorAction SilentlyContinue
if (-not $user) { throw "There's no local user called '$BoothUser'. Make it first (guide, Step 2)." }
$prof = Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $user.SID.Value } | Select-Object -First 1
$startup = $null
if ($prof) { $startup = Join-Path $prof.LocalPath 'AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup' }
else { Write-Warning "$BoothUser hasn't signed in yet, so its start-up folder doesn't exist. Sign in as $BoothUser once, then run this again for the start-up items." }

# ---- Find the other booth software ---------------------------------------------------------------------
$pf = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
function Find-Exe([string[]]$subdirs, [string]$pattern) {
  foreach ($r in $pf) { foreach ($d in $subdirs) {
    $dir = Join-Path $r $d
    if (Test-Path $dir) {
      $hit = Get-ChildItem -Path $dir -Recurse -Depth 3 -Filter $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($hit) { return $hit.FullName }
    }
  } }
  return ''
}
$found = [ordered]@{
  'Dante Controller'        = Find-Exe @('Audinate') 'DanteController.exe'
  'Dante Virtual Soundcard' = Find-Exe @('Audinate') 'Dante Virtual Soundcard*.exe'
  'R Remote'                = Find-Exe @('Yamaha') '*Remote*.exe'
  'Companion'               = Find-Exe @('Companion', 'Bitfocus') '*Companion*.exe'
}
if (-not $found['Dante Virtual Soundcard']) { $found['Dante Virtual Soundcard'] = Find-Exe @('Audinate') 'DVS*.exe' }
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1

# ---- What will happen ----------------------------------------------------------------------------------
$taskName = 'C3 Life'
$serverDir = Join-Path $InstallDir 'server'
$appDir = Join-Path $InstallDir 'app'
$cfgPath = Join-Path $serverDir 'config.json'
$dataDir = Join-Path $InstallDir 'data'
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listen) {
  $owner = (Get-Process -Id $listen.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  if ($owner -notmatch '^pythonw?$') { throw "Port $Port is already used by '$owner'. Stop that program (for example IIS) first, or use -Port." }
}

Write-Host ''
Write-Host 'This will:' -ForegroundColor Cyan
Write-Host "  1. Copy the C3 Life server and app to $InstallDir (your config.json is kept)"
Write-Host "  2. Start C3 Life on port $Port whenever $BoothUser signs in, and restart it if it stops"
Write-Host "  3. Allow ports $Port (C3 Life) and 8000 (Companion) through the firewall, for $BoothNetwork only"
if ($ListenFromHome) { Write-Host "     and let Tailscale devices ($TailscaleNetwork) reach Listen only, to listen from home" } else { Write-Host '     Listening from home stays off (use -ListenFromHome to turn it on)' }
if ($RemoteSupport) { Write-Host '     and let Admins signed in through Tailscale use the whole app (remote support)' } else { Write-Host '     Remote support stays off (use -RemoteSupport to turn it on)' }
if ($AudioEngine -eq 'asio') { Write-Host '     and hear Dante Virtual Soundcard in ASIO mode, 64 x 64 (install sounddevice for Python), for Listen, the RTA and recording' }
else { Write-Host '     and hear Dante Virtual Soundcard in WDM mode, 16 channels (-AudioEngine wdm: no recording)' }
if ($NoSubtitles) { Write-Host '     Subtitles on Listen stay off (-NoSubtitles)' } else { Write-Host "     and set up subtitles: install Vosk for Python and download the English speech model (about 40 MB, once) to $InstallDir\speech-model" }
if (-not $NoSubtitles -and $Languages) { Write-Host "     and translated subtitles in: $Languages (about 100 MB each, once) to $InstallDir\translate" }
if ($startup) {
  if ($found['Dante Virtual Soundcard']) { Write-Host "  4. Start Dante Virtual Soundcard when $BoothUser signs in" }
  if (-not $NoKiosk -and $edge) { Write-Host "  5. Open C3 Life full screen on the booth screen when $BoothUser signs in" }
}
Write-Host ''
Write-Host 'Booth software found:' -ForegroundColor Cyan
foreach ($k in $found.Keys) {
  if ($found[$k]) { Write-Host ("  OK       {0}" -f $k) -ForegroundColor Green } else { Write-Host ("  MISSING  {0}  (install it, guide Step 5)" -f $k) -ForegroundColor Yellow }
}
Write-Host ''
$go = $Yes
if (-not $go) { $go = (Read-Host 'Type YES to install') -ceq 'YES' }
if (-not $go) { Write-Host 'Nothing was changed.'; Stop-Transcript | Out-Null; return }

# 1. Files
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*c3life_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $serverDir, $appDir | Out-Null
Copy-Item -Force (Join-Path $here 'server\c3life_server.py'), (Join-Path $here 'server\listen.py'), (Join-Path $here 'server\access.py'), (Join-Path $here 'server\dante.py'), (Join-Path $here 'server\nametags.py'), (Join-Path $here 'server\scripture.py'), (Join-Path $here 'server\cues.py'), (Join-Path $here 'server\reports.py'), (Join-Path $here 'server\pco.py'), (Join-Path $here 'server\wing.py'), (Join-Path $here 'server\tio.py'), (Join-Path $here 'server\monitors.py'), (Join-Path $here 'server\scan.py'), (Join-Path $here 'server\hwcheck.py'), (Join-Path $here 'server\foyer.py'), (Join-Path $here 'server\rta_inputs.py'), (Join-Path $here 'server\rta_patch.py'), (Join-Path $here 'server\dante_names.py'), (Join-Path $here 'server\lightkey.py'), (Join-Path $here 'server\rta_live.py'), (Join-Path $here 'server\midi_in.py'), (Join-Path $here 'server\desk.py'), (Join-Path $here 'server\music.py'), (Join-Path $here 'server\helpers.py'), (Join-Path $here 'server\health.py'), (Join-Path $here 'server\ringout.py'), (Join-Path $here 'server\macros.py'), (Join-Path $here 'server\audio_engine.py'), (Join-Path $here 'server\recorder.py') $serverDir
New-Item -ItemType Directory -Force -Path (Join-Path $serverDir 'bibles') | Out-Null
Copy-Item -Force (Join-Path $here 'server\bibles\*') (Join-Path $serverDir 'bibles')
# netaudio reads the Dante routing (read only), so Listen sources can be named from Dante.
& $py -m pip install --quiet --disable-pip-version-check netaudio
if ($LASTEXITCODE -ne 0) { Write-Warning 'netaudio did not install: Listen settings will use the routes saved on Mixer > Dante for names.' }
# sounddevice: PortAudio with ASIO, so C3 Life can open Dante Virtual Soundcard's 64 channels itself.
if ($AudioEngine -eq 'asio') {
  & $py -m pip install --quiet --disable-pip-version-check sounddevice
  if ($LASTEXITCODE -ne 0) { Write-Warning 'sounddevice did not install: C3 Life will hear nothing until it does. Run this again with internet.' }
}
if (Test-Path $appDir) { Get-ChildItem $appDir | Remove-Item -Recurse -Force }
Copy-Item -Recurse -Force (Join-Path $here 'app\*') $appDir
if (Test-Path $cfgPath) { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json }
else { $cfg = [pscustomobject][ordered]@{ port = $Port; bind = '0.0.0.0'; app_dir = $appDir; data_dir = $dataDir; allowed_networks = @($boothNets + '127.0.0.0/8'); start_page = '/player.html?p=Listen.dc.html&start=1' } }
# Listen settings are refreshed every time: where ffmpeg is, the DVS sound device, and listening from home.
if ($ffmpeg) { $cfg | Add-Member -Force -NotePropertyName ffmpeg -NotePropertyValue $ffmpeg }
if ($dvsDevice) { $cfg | Add-Member -Force -NotePropertyName listen_device -NotePropertyValue $dvsDevice }
$cfg | Add-Member -Force -NotePropertyName audio_engine -NotePropertyValue $(if ($AudioEngine -eq 'asio') { 'asio' } else { '' })
$cfg | Add-Member -Force -NotePropertyName data_dir -NotePropertyValue $dataDir
# Name tags (Running order and Show). The token stays in config.json on this PC; phones never see it.
if ($PlanningCenterAppId -and $PlanningCenterSecret) {
  $cfg | Add-Member -Force -NotePropertyName pco_app_id -NotePropertyValue $PlanningCenterAppId
  $cfg | Add-Member -Force -NotePropertyName pco_secret -NotePropertyValue $PlanningCenterSecret
  Write-Host 'Name tags: Planning Center token saved'
} elseif (-not $cfg.pco_app_id) {
  Write-Host 'Name tags: no Planning Center token yet. Run again with -PlanningCenterAppId and -PlanningCenterSecret to turn them on.'
}
if ($ProPresenter) { $cfg | Add-Member -Force -NotePropertyName propresenter -NotePropertyValue $ProPresenter }
if ($Helper) { $cfg | Add-Member -Force -NotePropertyName helper -NotePropertyValue $Helper }
if ($HelperKey) {
  $cfg | Add-Member -Force -NotePropertyName helper_key -NotePropertyValue $HelperKey
  Write-Host "Music: the helper on $Helper, with its key"
}
if ($WingIp) {
  if ($WingIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "-WingIp should look like 192.168.10.20" }
  $cfg | Add-Member -Force -NotePropertyName wing_ip -NotePropertyValue $WingIp
  Write-Host "Wing: $WingIp (starts as Watch only; an Admin switches it to Control on the Mixer screen)"
}
if ($TioIp) {
  if ($TioIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "-TioIp should look like 192.168.10.41" }
  $cfg | Add-Member -Force -NotePropertyName tio_ip -NotePropertyValue $TioIp
  Write-Host "Tio1608-D: $TioIp (starts as Watch only)"
}
# Bible Scripture keys. They stay in config.json on this PC; phones never see them.
if ($EsvKey) { $cfg | Add-Member -Force -NotePropertyName esv_key -NotePropertyValue $EsvKey; Write-Host 'Bible Scripture: ESV key saved' }
if ($NltKey) { $cfg | Add-Member -Force -NotePropertyName nlt_key -NotePropertyValue $NltKey; Write-Host 'Bible Scripture: NLT key saved' }
if ($ApiBibleKey) { $cfg | Add-Member -Force -NotePropertyName apibible_key -NotePropertyValue $ApiBibleKey; Write-Host 'Bible Scripture: API.Bible key saved' }
if ($NivBibleId) {
  $ids = @{}; if ($cfg.apibible_ids) { $cfg.apibible_ids.PSObject.Properties | ForEach-Object { $ids[$_.Name] = $_.Value } }
  $ids['NIV'] = $NivBibleId
  $cfg | Add-Member -Force -NotePropertyName apibible_ids -NotePropertyValue ([pscustomobject]$ids)
  Write-Host 'Bible Scripture: NIV Bible id saved'
}
# Issue reports. The email password is asked for here and stays in config.json on this PC.
if ($ReportTo) { $cfg | Add-Member -Force -NotePropertyName report_to -NotePropertyValue $ReportTo }
if ($SmtpHost) {
  $cfg | Add-Member -Force -NotePropertyName smtp_host -NotePropertyValue $SmtpHost
  $cfg | Add-Member -Force -NotePropertyName smtp_port -NotePropertyValue $SmtpPort
  if ($SmtpUser) { $cfg | Add-Member -Force -NotePropertyName smtp_user -NotePropertyValue $SmtpUser }
  $cfg | Add-Member -Force -NotePropertyName smtp_from -NotePropertyValue $(if ($SmtpFrom) { $SmtpFrom } else { $SmtpUser })
  if ($SmtpUser) {
    $sec = Read-Host "Password (or app password) for $SmtpUser" -AsSecureString
    $cfg | Add-Member -Force -NotePropertyName smtp_password -NotePropertyValue ([Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)))
  }
  Write-Host "Issue reports: email through $SmtpHost saved"
} elseif (-not $cfg.smtp_host) {
  Write-Host 'Issue reports: email not set up yet (run again with -SmtpHost and -SmtpUser). Reports wait on the C3 Life server until it is.'
}
if ($MondayToken) { $cfg | Add-Member -Force -NotePropertyName monday_token -NotePropertyValue $MondayToken; Write-Host 'Issue reports: monday.com token saved (pick the board in the app)' }
# Subtitles: Vosk turns speech into text on this PC, with no internet once it's installed.
$modelDir = Join-Path $InstallDir 'speech-model'
if (-not $NoSubtitles) {
  try {
    & $py -m pip install --quiet --disable-pip-version-check vosk
    if ($LASTEXITCODE -ne 0) { throw 'pip failed' }
    if (-not (Test-Path (Join-Path $modelDir 'am'))) {
      $zip = Join-Path $env:TEMP 'c3life-speech-model.zip'
      Write-Host 'Downloading the speech model for subtitles...'
      Invoke-WebRequest -Uri $SpeechModel -OutFile $zip -UseBasicParsing
      $tmp = Join-Path $env:TEMP 'c3life-speech-model'
      Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
      Expand-Archive -Path $zip -DestinationPath $tmp -Force
      $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
      Remove-Item -Recurse -Force $modelDir -ErrorAction SilentlyContinue
      Move-Item $inner.FullName $modelDir
      Remove-Item -Force $zip
    }
    $cfg | Add-Member -Force -NotePropertyName captions_model -NotePropertyValue $modelDir
    Write-Host "Subtitles: ready ($modelDir)"
    # Translated subtitles: Argos Translate language packs, run through CTranslate2. No internet once installed.
    $codes = @($Languages -split '[,; ]+' | Where-Object { $_ -match '^[a-z]{2,3}$' })
    if ($codes.Count) {
      & $py -m pip install --quiet --disable-pip-version-check ctranslate2 sentencepiece
      if ($LASTEXITCODE -ne 0) { throw 'pip failed (ctranslate2)' }
      $trDir = Join-Path $InstallDir 'translate'
      New-Item -ItemType Directory -Force -Path $trDir | Out-Null
      $index = Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json' -UseBasicParsing
      foreach ($code in $codes) {
        if (Test-Path (Join-Path $trDir "$code\metadata.json")) { Write-Host "  Translation: $code already installed"; continue }
        $pkg = $index | Where-Object { $_.from_code -eq 'en' -and $_.to_code -eq $code } | Select-Object -First 1
        if (-not $pkg) { Write-Warning "No English to '$code' language pack. See the list at https://www.argosopentech.com/argospm/index/"; continue }
        Write-Host "  Downloading English to $($pkg.to_name)..."
        $zip = Join-Path $env:TEMP "c3life-$code.zip"
        Invoke-WebRequest -Uri $pkg.links[0] -OutFile $zip -UseBasicParsing
        $tmp = Join-Path $env:TEMP "c3life-$code"
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
        Move-Item $inner.FullName (Join-Path $trDir $code)
        Remove-Item -Force $zip
      }
      Write-Host "Translated subtitles: $((Get-ChildItem $trDir -Directory).Name -join ', ')"
    }
  } catch {
    Write-Warning "Subtitles weren't set up ($_). Listen still works without them. Check the internet and run this again."
  }
}
# Lists stay lists in config.json: $(if ...) would turn an empty list into null and a list of one into a plain string
[string[]]$remoteNets = @()
if ($ListenFromHome -or $RemoteSupport) { $remoteNets = @($TailscaleNetwork) }
[string[]]$supportRoles = @()
if ($RemoteSupport) { $supportRoles = @('Admin') }
$cfg | Add-Member -Force -NotePropertyName remote_networks -NotePropertyValue $remoteNets
$cfg | Add-Member -Force -NotePropertyName remote_support_roles -NotePropertyValue $supportRoles
$cfg | ConvertTo-Json -Depth 4 | Set-Content -Path $cfgPath -Encoding UTF8
# Where the apps C3 Life opens on this PC are (Dante > Open R Remote, and Dante Controller).
[ordered]@{ rremote = $found['R Remote']; dante = $found['Dante Controller']; dvs = $found['Dante Virtual Soundcard']; companion = $found['Companion'] } |
  ConvertTo-Json | Set-Content -Path (Join-Path $InstallDir 'local-apps.json') -Encoding UTF8
# The data folder holds everyone's PINs (hashed) and mixes: only the booth user (which runs C3 Life) and admins can read it.
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
& icacls $dataDir /inheritance:r /grant "$($env:COMPUTERNAME)\$($BoothUser):(OI)(CI)M" /grant "*S-1-5-32-544:(OI)(CI)F" /grant "*S-1-5-18:(OI)(CI)F" | Out-Null
& icacls $logDir /grant "$($env:COMPUTERNAME)\$($BoothUser):(OI)(CI)M" | Out-Null

# 2. Start at sign-in
$argLine = '"{0}" --config "{1}" --log "{2}"' -f (Join-Path $serverDir 'c3life_server.py'), $cfgPath, (Join-Path $logDir 'c3life.log')
$action = New-ScheduledTaskAction -Execute $pyw -Argument $argLine -WorkingDirectory $serverDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$($env:COMPUTERNAME)\$BoothUser"
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$($env:COMPUTERNAME)\$BoothUser" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# 3. Firewall, booth network only
foreach ($r in @(@{ n = 'C3 Life (booth network)'; p = $Port }, @{ n = 'Companion (booth network)'; p = 8000 })) {
  Get-NetFirewallRule -DisplayName $r.n -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName $r.n -Direction Inbound -Protocol TCP -LocalPort $r.p -RemoteAddress $fwRemote -Profile Private, Domain -Action Allow | Out-Null
}
# The Wing's meters, and the network scan's answers (Wing search, Bonjour), arrive by UDP, from the booth network only
Get-NetFirewallRule -DisplayName 'C3 Life Wing meters' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
# network MIDI from MultiTracks Playback (Lights › Show setup), booth network only
Get-NetFirewallRule -DisplayName 'C3 Life network MIDI (Playback)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'C3 Life network MIDI (Playback)' -Direction Inbound -Protocol UDP -LocalPort 5004, 5005 -RemoteAddress $fwRemote -Profile Private, Domain -Action Allow | Out-Null
New-NetFirewallRule -DisplayName 'C3 Life Wing meters' -Direction Inbound -Protocol UDP -Program $pyw -RemoteAddress $fwRemote -Profile Private, Domain -Action Allow | Out-Null
# Listening from home: Tailscale devices may reach C3 Life, which only lets them use Listen.
Get-NetFirewallRule -DisplayName 'C3 Life Listen (from home)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($ListenFromHome -or $RemoteSupport) {
  New-NetFirewallRule -DisplayName 'C3 Life Listen (from home)' -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $TailscaleNetwork -Profile Any -Action Allow | Out-Null
  $tsExe = if (Get-Command tailscale -ErrorAction SilentlyContinue) { (Get-Command tailscale).Source } elseif (Test-Path "$env:ProgramFiles\Tailscale\tailscale.exe") { "$env:ProgramFiles\Tailscale\tailscale.exe" } else { $null }
  if (-not $tsExe) { Write-Warning 'Tailscale is not installed on this PC yet. Install it and sign in with the church account (tailscale.com/download).' }
  else {
    # Keep Tailscale connected when nobody is signed in and after a restart, so help can always reach the PC.
    & $tsExe set --unattended=true 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host 'Tailscale: stays connected after a restart (Run unattended)' } else { Write-Warning 'Tailscale: turn on Run unattended in its menu (sign in first).' }
  }
}
if ($RemoteSupport) {
  $crd = Get-Service -Name chromoting -ErrorAction SilentlyContinue
  if ($crd) { Write-Host 'Remote desktop: Chrome Remote Desktop is installed' }
  else { Write-Host 'Remote desktop: set up Chrome Remote Desktop next (remotedesktop.google.com/access, signed in as the church Google account). Help > Fix things from home.' }
}

# 4 and 5. Start-up items for the booth user
if ($startup) {
  New-Item -ItemType Directory -Force -Path $startup | Out-Null
  $sh = New-Object -ComObject WScript.Shell
  if ($found['Dante Virtual Soundcard']) {
    $lnk = $sh.CreateShortcut((Join-Path $startup 'Dante Virtual Soundcard.lnk'))
    $lnk.TargetPath = $found['Dante Virtual Soundcard']; $lnk.WorkingDirectory = Split-Path $found['Dante Virtual Soundcard']; $lnk.Save()
  }
  $kiosk = Join-Path $startup 'booth-screen.cmd'
  if (-not $NoKiosk -and $edge) {
    # Waits until C3 Life answers (up to 2 minutes), then opens it full screen.
    @"
@echo off
set /a n=0
:wait
curl.exe -s -o nul -m 2 http://localhost:$Port/api/health && goto open
set /a n+=1
if %n% geq 60 goto open
timeout /t 2 /nobreak >nul
goto wait
:open
start "" "$edge" --kiosk http://localhost:$Port/ --edge-kiosk-type=fullscreen --no-first-run
"@ | Set-Content -Path $kiosk -Encoding ASCII
  } elseif (Test-Path $kiosk) { Remove-Item $kiosk -Force }
}

# Start it now (if the booth user is signed in) and check it answers.
Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$ok = $false
for ($i = 0; $i -lt 10 -and -not $ok; $i++) {
  Start-Sleep -Seconds 1
  try { $ok = (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2).ok } catch { }
}
Write-Host ''
if ($ok) {
  $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' } | ForEach-Object { "http://$($_.IPAddress)" })
  Write-Host "C3 Life is running: http://$($env:COMPUTERNAME.ToLower()).local$(if ($ips) { ' (or ' + ($ips -join ', ') + ')' })" -ForegroundColor Green
}
else { Write-Host "Installed. C3 Life starts the next time $BoothUser signs in (restart the PC to check)." -ForegroundColor Yellow }
Write-Host ''
Write-Host 'Everyone starts with PIN 1234, and Admin with the password c3life-admin. Change each one before real use, for example:' -ForegroundColor Yellow
Write-Host "  & '$py' '$(Join-Path $serverDir 'c3life_server.py')' --config '$cfgPath' --set-pin 'Keys (musician)'"
Write-Host "  & '$py' '$(Join-Path $serverDir 'c3life_server.py')' --config '$cfgPath' --set-pin 'Admin'     (asks for a password of 8 or more characters)"
Write-Host 'Next: restart the PC, then run Check-C3LifeServer.ps1.'
Stop-Transcript | Out-Null
