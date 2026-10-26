<#
.SYNOPSIS
  Adds (or removes) the GitHub key C3 Life uses to look for and download updates. You type the key; it's never shown.

.DESCRIPTION
  The C3 Life repository is private, so the HP needs a key to read it. Make a fine-grained token on GitHub that can
  only read this one repository (github.com > Settings > Developer settings > Fine-grained tokens > Generate new token:
  Only select repositories: c3life; Repository permissions: Contents: Read-only; an expiry of up to a year).
  Then, in an Administrator PowerShell in C:\C3Life:
    .\Set-GitHubKey.ps1          paste the key when it asks (nothing shows as you paste), then Enter
    .\Set-GitHubKey.ps1 -Remove  takes it off the HP
  It checks the key with GitHub, then keeps it encrypted for this PC in C:\C3Life\secrets, readable only by SYSTEM and
  Administrators: the C3 Life app (the booth user) never sees it, and it's never in config.json, the logs or reports.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = 'C:\C3Life',
  [switch]$Remove
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in an Administrator PowerShell.' }
$dir = Join-Path $InstallDir 'secrets'
$file = Join-Path $dir 'github-key.dat'
if ($Remove) {
  if (Test-Path $file) { Remove-Item -Force $file; Write-Host 'The GitHub key is off the HP. C3 Life can no longer look for updates.' }
  else { Write-Host 'There was no GitHub key on the HP.' }
  exit 0
}
$cfgPath = Join-Path $InstallDir 'server\config.json'
$cfg = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$repo = if ($cfg.PSObject.Properties['update_repo'] -and $cfg.update_repo) { $cfg.update_repo } else { 'jmcginlay90-blip/c3life' }
$api = if ($cfg.PSObject.Properties['update_api'] -and $cfg.update_api) { $cfg.update_api.TrimEnd('/') } else { 'https://api.github.com' }

$sec = Read-Host -AsSecureString "Paste the GitHub key for $repo (nothing shows as you paste), then press Enter"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $key) { throw 'Nothing was pasted.' }

Write-Host "Checking the key with GitHub..."
try {
  $h = @{ 'Authorization' = "Bearer $key"; 'Accept' = 'application/vnd.github+json'; 'User-Agent' = 'C3-Life-updater' }
  $r = Invoke-RestMethod -Uri "$api/repos/$repo" -Headers $h -TimeoutSec 30
  Write-Host "  It can read $($r.full_name)."
  if ($r.permissions -and ($r.permissions.push -or $r.permissions.admin)) { Write-Warning 'This key can also change the repository. A read-only key (Contents: Read-only) is safer: consider making a new one.' }
} catch {
  $m = $_.Exception.Message
  if ($m -match '401') { throw 'GitHub said no to that key. Check you copied all of it, and that it has not run out.' }
  if ($m -match '404') { throw "That key can't see $repo. When you make it, choose Only select repositories: c3life, and Contents: Read-only." }
  throw "Couldn't reach GitHub to check the key: $m"
}

Add-Type -AssemblyName System.Security
New-Item -ItemType Directory -Force -Path $dir | Out-Null
# only SYSTEM and Administrators can open this folder
& icacls $dir /inheritance:r /grant '*S-1-5-18:(OI)(CI)F' /grant '*S-1-5-32-544:(OI)(CI)F' | Out-Null
$enc = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($key), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
[IO.File]::WriteAllBytes($file, $enc)
$key = $null
Write-Host 'The GitHub key is on the HP, encrypted. C3 Life looks for updates once a day; an Admin updates in the app (Saved setup > Updates).'
# look now, so the app shows what's new straight away
Start-ScheduledTask -TaskName 'C3 Life Update' -ErrorAction SilentlyContinue
