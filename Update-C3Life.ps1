<#
.SYNOPSIS
  Updates C3 Life from GitHub: looks for a new version, and installs it when an Admin asks in the app.

.DESCRIPTION
  Run by the scheduled task "C3 Life Update" (as SYSTEM, made by Install-C3Life.ps1), never by hand:
    - once a day, and when an Admin taps "Look for updates" in the app: asks GitHub what's new and writes it to
      data\update\status.json for the app to show. It changes nothing.
    - when an Admin taps "Update now" and says yes (the app writes data\update\request.json): downloads that version,
      backs up the app, the server and data\, runs its Install-C3Life.ps1 again with the same options as last time,
      checks C3 Life starts, and if it doesn't, puts the backup back.
  It never updates on a Sunday. The app also refuses while recording or playing back.

  The repository is private, so GitHub needs a key: a fine-grained token that can only read this repository
  (Contents: Read). The person adds it on the HP with Set-GitHubKey.ps1; it's kept encrypted for this PC, readable
  only by SYSTEM and Administrators. C3 Life itself (the booth user) never sees it.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = 'C:\C3Life',
  [string]$KeyFile = '',
  # tests: run the steps here without Windows (no scheduled tasks, no DPAPI, a pretend installer)
  [switch]$Test
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$cfgPath = Join-Path $InstallDir 'server\config.json'
$cfg = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
function Opt($name, $default) { if ($cfg.PSObject.Properties[$name] -and $cfg.$name) { return $cfg.$name } return $default }
$repo = Opt 'update_repo' 'jmcginlay90-blip/c3life'
$branch = Opt 'update_branch' 'main'
$api = (Opt 'update_api' 'https://api.github.com').TrimEnd('/')
$dataDir = Opt 'data_dir' (Join-Path $InstallDir 'data')
$port = [int](Opt 'port' 80)
$upDir = Join-Path $dataDir 'update'
$logDir = Join-Path $InstallDir 'logs'
New-Item -ItemType Directory -Force -Path $upDir, $logDir | Out-Null
if (-not $KeyFile) { $KeyFile = Join-Path $InstallDir 'secrets\github-key.dat' }
$logFile = Join-Path $logDir 'update.log'

function Log($t) { $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $t; Add-Content -Path $logFile -Value $line; Write-Host $line }
function Read-Json($p) { if (Test-Path $p) { try { return Get-Content $p -Raw | ConvertFrom-Json } catch { return $null } } return $null }
function Write-Json($p, $o) { $tmp = "$p.tmp"; ($o | ConvertTo-Json -Depth 6) | Set-Content -Path $tmp -Encoding UTF8; Move-Item -Force $tmp $p }

$statusPath = Join-Path $upDir 'status.json'
$status = Read-Json $statusPath
if (-not $status) { $status = [pscustomobject]@{} }
function Set-Status([hashtable]$h) {
  foreach ($k in $h.Keys) { $status | Add-Member -Force -NotePropertyName $k -NotePropertyValue $h[$k] }
  $status | Add-Member -Force -NotePropertyName 'at' -NotePropertyValue (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  Write-Json $statusPath $status
}

# ---- the key: encrypted for this PC (DPAPI, LocalMachine), readable only by SYSTEM and Administrators ----
function Get-Key {
  if (-not (Test-Path $KeyFile)) { return $null }
  if ($Test -and -not ($PSVersionTable.PSEdition -eq 'Desktop' -or $IsWindows)) { return (Get-Content $KeyFile -Raw).Trim() }
  Add-Type -AssemblyName System.Security
  $b = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($KeyFile), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
  return [Text.Encoding]::UTF8.GetString($b)
}

function GH($path, $key) {
  $h = @{ 'Authorization' = "Bearer $key"; 'Accept' = 'application/vnd.github+json'; 'User-Agent' = 'C3-Life-updater'; 'X-GitHub-Api-Version' = '2022-11-28' }
  return Invoke-RestMethod -Uri ($api + $path) -Headers $h -TimeoutSec 30
}
# A commit's title in plain words: for a merged pull request, the pull request's title (its second paragraph)
function Title($msg) {
  $lines = @(($msg -split "`r?`n") | Where-Object { $_.Trim() })
  if ($lines.Count -gt 1 -and $lines[0] -match '^Merge pull request #\d+') { return $lines[1].Trim() }
  if ($lines.Count) { return $lines[0].Trim() } return ''
}
function Short($c) { return [ordered]@{ sha = $c.sha; date = $c.commit.committer.date; title = (Title $c.commit.message) } }

function Check($key) {
  $head = GH "/repos/$repo/commits/$branch" $key
  $latest = Short $head
  $inst = Read-Json (Join-Path $upDir 'installed.json')
  $new = @()
  if ($inst -and $inst.sha -and $inst.sha -ne $head.sha) {
    try {
      $cmp = GH "/repos/$repo/compare/$($inst.sha)...$($head.sha)" $key
      # merged pull requests only, newest first: what's new, in their own words
      $new = @($cmp.commits | Where-Object { $_.parents.Count -gt 1 -or -not ($cmp.commits | Where-Object { $_.parents.Count -gt 1 }) } | ForEach-Object { Short $_ })
      [array]::Reverse($new)
    } catch { $new = @() }
  }
  Set-Status @{ checked = (Get-Date -Format 'yyyy-MM-dd HH:mm'); ok = $true; error = ''; key = $true; latest = $latest; new = @($new | Select-Object -First 30); behind = [bool]((-not $inst) -or ($inst.sha -ne $head.sha)) }
  Log "checked: latest $($head.sha.Substring(0, 7)) $($latest.title)"
  return $head
}

function Health {
  try { $h = Invoke-RestMethod "http://127.0.0.1:$port/api/health" -TimeoutSec 3; return [bool]($h.ok -and $h.service -eq 'c3life') } catch { return $false }
}
function Copy-Tree($from, $to, [string[]]$skip = @()) {
  if ($Test -and -not ($PSVersionTable.PSEdition -eq 'Desktop' -or $IsWindows)) {
    New-Item -ItemType Directory -Force -Path $to | Out-Null
    Get-ChildItem $to -Force | Where-Object { $skip -notcontains $_.Name } | Remove-Item -Recurse -Force
    Get-ChildItem $from -Force | Where-Object { $skip -notcontains $_.Name } | Copy-Item -Destination $to -Recurse -Force
    return
  }
  $xd = @(); foreach ($s in $skip) { $xd += @('/XD', (Join-Path $from $s), (Join-Path $to $s)) }
  & robocopy $from $to /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP @xd | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "Copying $from to $to failed (robocopy $LASTEXITCODE)" }
}
function Stop-C3Life {
  if ($Test) { return }
  Stop-ScheduledTask -TaskName 'C3 Life' -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*c3life_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Start-C3Life { if (-not $Test) { Start-ScheduledTask -TaskName 'C3 Life' -ErrorAction SilentlyContinue } }

function Apply($key, $req) {
  if ((Get-Date).DayOfWeek -eq 'Sunday' -and -not $env:C3LIFE_UPDATE_ANY_DAY) { throw 'Not on a Sunday: update on a weekday.' }
  Set-Status @{ state = 'checking'; error = '' }
  $head = Check $key
  if ($req.sha -ne $head.sha) { throw 'A newer version came out since you looked. Look for updates again, then update.' }
  $sha = $head.sha
  $title = Title $head.commit.message
  Log "updating to $($sha.Substring(0, 7)) for $($req.by): $title"

  # 1. download that version
  Set-Status @{ state = 'downloading' }
  $updates = Join-Path $InstallDir 'updates'
  New-Item -ItemType Directory -Force -Path $updates | Out-Null
  $zip = Join-Path $updates "$sha.zip"
  $h = @{ 'Authorization' = "Bearer $key"; 'Accept' = 'application/vnd.github+json'; 'User-Agent' = 'C3-Life-updater' }
  $url = "$api/repos/$repo/zipball/$sha"
  # GitHub answers with a short-lived download link; follow it without sending the key on
  $loc = $null
  try { $r = Invoke-WebRequest -Uri $url -Headers $h -MaximumRedirection 0 -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop; if ($r.StatusCode -ge 300) { $loc = $r.Headers.Location } else { [IO.File]::WriteAllBytes($zip, $r.Content) } }
  catch { $resp = $_.Exception.Response; if ($resp -and $resp.Headers -and $resp.Headers.Location) { $loc = [string]$resp.Headers.Location } else { throw } }
  if ($loc) { Invoke-WebRequest -Uri ([string]$loc) -UseBasicParsing -OutFile $zip -TimeoutSec 600 -Headers @{ 'User-Agent' = 'C3-Life-updater' } }
  $out = Join-Path $updates $sha
  if (Test-Path $out) { Remove-Item -Recurse -Force $out }
  Expand-Archive -Path $zip -DestinationPath $out -Force
  $src = Get-ChildItem $out -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'Install-C3Life.ps1') } | Select-Object -First 1
  if (-not $src) { throw 'That download isn''t a C3 Life version (no Install-C3Life.ps1 in it).' }
  foreach ($need in 'server\c3life_server.py', 'app\player.html') { if (-not (Test-Path (Join-Path $src.FullName $need))) { throw "That download is missing $need." } }

  # 2. back up the app, the server (its config.json too) and data\ (the last 3 backups are kept)
  Set-Status @{ state = 'backing up' }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $bk = Join-Path $InstallDir "backups\before-update-$stamp"
  foreach ($d in 'app', 'server') { if (Test-Path (Join-Path $InstallDir $d)) { Copy-Tree (Join-Path $InstallDir $d) (Join-Path $bk $d) } }
  if (Test-Path $dataDir) { Copy-Tree $dataDir (Join-Path $bk 'data') @('update') }
  Get-ChildItem (Join-Path $InstallDir 'backups') -Directory -Filter 'before-update-*' | Sort-Object Name -Descending | Select-Object -Skip 3 | Remove-Item -Recurse -Force
  Log "backed up to $bk"

  # 3. install it, with the same options as last time
  Set-Status @{ state = 'installing' }
  $wasRunning = (Health)
  $opts = Read-Json (Join-Path $InstallDir 'install-options.json')
  $ih = [ordered]@{ Yes = $true }
  if ($opts) {
    foreach ($p in $opts.PSObject.Properties) {
      if ($p.Value -is [bool]) { if ($p.Value) { $ih[$p.Name] = $true } }
      elseif ("$($p.Value)" -ne '') { $ih[$p.Name] = "$($p.Value)" }
    }
  }
  $iargs = @(); foreach ($k in $ih.Keys) { if ($ih[$k] -is [bool]) { $iargs += "-$k" } else { $iargs += @("-$k", $ih[$k]) } }
  $installer = Join-Path $src.FullName 'Install-C3Life.ps1'
  Log "running $installer $($iargs -join ' ')"
  $ilog = Join-Path $logDir "update-install-$stamp.txt"
  $failed = ''
  $env:C3LIFE_UPDATING = '1'   # the installer then leaves this task alone while it runs
  try {
    if ($Test) { & (Join-Path $src.FullName 'Install-C3Life.ps1') @ih *> $ilog }
    else { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer @iargs *> $ilog }
    if ($LASTEXITCODE) { $failed = "the installer stopped (exit $LASTEXITCODE)" }
  } catch { $failed = "the installer stopped: $($_.Exception.Message)" }
  Remove-Item Env:\C3LIFE_UPDATING -ErrorAction SilentlyContinue

  # 4. does C3 Life start again? (only if it was running before: with nobody signed in as booth it isn't)
  if (-not $failed) {
    Set-Status @{ state = 'starting' }
    if ($wasRunning) {
      $ok = $false
      foreach ($i in 1..60) { Start-Sleep 2; if (Health) { $ok = $true; break } }
      if (-not $ok) { $failed = 'C3 Life didn''t start again within 2 minutes' }
    } else { Log 'C3 Life wasn''t running before (booth not signed in), so starting it isn''t checked' }
  }

  if ($failed) {
    # 5. put the backup back
    Log "update failed: $failed. Putting the backup back."
    Set-Status @{ state = 'putting back' }
    Stop-C3Life
    foreach ($d in 'app', 'server') { if (Test-Path (Join-Path $bk $d)) { Copy-Tree (Join-Path $bk $d) (Join-Path $InstallDir $d) } }
    if (Test-Path (Join-Path $bk 'data')) { Copy-Tree (Join-Path $bk 'data') $dataDir @('update') }
    Start-C3Life
    Set-Status @{ state = 'idle'; last = [ordered]@{ result = 'put back'; to = $sha; title = $title; by = $req.by; when = (Get-Date -Format 'yyyy-MM-dd HH:mm'); why = "$failed. The version before was put back, so nothing changed." } }
    return
  }
  Write-Json (Join-Path $upDir 'installed.json') ([ordered]@{ sha = $sha; date = $head.commit.committer.date; title = $title; when = (Get-Date -Format 'yyyy-MM-dd HH:mm'); by = $req.by })
  Set-Status @{ state = 'idle'; behind = $false; new = @(); last = [ordered]@{ result = 'updated'; to = $sha; title = $title; by = $req.by; when = (Get-Date -Format 'yyyy-MM-dd HH:mm'); why = '' } }
  Get-ChildItem $updates | Where-Object { $_.Name -notlike "$sha*" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Log "updated to $($sha.Substring(0, 7))"
}

# ---- what to do: an Admin's request from the app, or (with none) just look ----
$lock = Join-Path $upDir 'running.lock'
if ((Test-Path $lock) -and ((Get-Item $lock).LastWriteTime -gt (Get-Date).AddHours(-2))) { Log 'already running'; exit 0 }
Set-Content -Path $lock -Value $PID
try {
  $reqPath = Join-Path $upDir 'request.json'
  $req = Read-Json $reqPath
  if (Test-Path $reqPath) { Remove-Item -Force $reqPath }
  $key = Get-Key
  if (-not $key) {
    Set-Status @{ ok = $false; key = $false; state = 'idle'; error = 'No GitHub key on the HP yet. An Admin adds it once with Set-GitHubKey.ps1 (HP Set-up guide).' }
    Log 'no GitHub key'
    exit 0
  }
  try {
    if ($req -and $req.action -eq 'apply' -and $req.sha) { Apply $key $req }
    else { Set-Status @{ state = 'checking' }; Check $key | Out-Null; Set-Status @{ state = 'idle' } }
  } catch {
    $msg = $_.Exception.Message
    if ($msg -match '401|Bad credentials') { $msg = 'GitHub said no to the key: it may have run out. Make a new one and add it with Set-GitHubKey.ps1.' }
    elseif ($msg -match '404') { $msg = "GitHub can't find $repo with that key: check the key can read this repository." }
    elseif ($msg -match 'resolve|connect|timed out|No such host') { $msg = "Couldn't reach GitHub: is there internet? ($msg)" }
    Set-Status @{ ok = $false; state = 'idle'; error = $msg }
    Log "stopped: $msg"
  }
} finally { Remove-Item -Force $lock -ErrorAction SilentlyContinue }
