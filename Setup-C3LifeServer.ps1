<#
.SYNOPSIS
  Sets up Windows on the C3 Life server (HP EliteDesk, i5-7500, 16 GB, 256 GB NVMe) for C3 Life.

.DESCRIPTION
  Does Steps 3 and 4 of the C3 Life server set-up guide in one go:
    - names the PC C3LIFE (so phones find it as c3life.local)
    - booth Ethernet: fixed address 192.168.10.10, no gateway, Private network, Energy Efficient Ethernet off,
      Windows not allowed to turn the adapter off
    - power: High performance, never sleep, hibernate and fast start-up off, USB selective suspend off,
      power button = shut down, UPS: shut down at 30% battery
    - Windows Update: no automatic downloads or restarts (or fully off), no driver updates, active hours 07-23,
      antivirus definitions still checked at start-up
    - no Windows tips or suggested apps
  It shows everything it will change and asks before changing anything. Run it again at any time: it only
  changes what isn't already right. Everything is written to C:\C3Life\logs.

  It does NOT set the booth user's automatic sign-in (use Microsoft Sysinternals Autologon, which stores the
  password encrypted), the BIOS, or the Wi-Fi for internet.

.PARAMETER UpdateMode
  Notify (recommended): Windows downloads and restarts nothing until someone clicks. Off: no automatic updates at all.

.EXAMPLE
  Right-click Start > Windows PowerShell (Admin), then:
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\Setup-C3LifeServer.ps1
#>
[CmdletBinding()]
param(
  [string]$ComputerName = 'C3LIFE',
  [string]$BoothIp = '192.168.10.10',
  [int]$PrefixLength = 24,
  [string]$AdapterName = '',
  [ValidateSet('Notify', 'Off')][string]$UpdateMode = 'Notify',
  [int]$ActiveHoursStart = 7,
  [int]$ActiveHoursEnd = 23,
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'

function Test-Admin {
  $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) { throw 'Run this in an Administrator PowerShell (right-click Start > Windows PowerShell (Admin)).' }

$logDir = 'C:\C3Life\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir ('setup-c3lifeserver-{0:yyyy-MM-dd-HHmm}.log' -f (Get-Date))) | Out-Null

# ---- What needs changing -------------------------------------------------------------------------------
$plan = New-Object System.Collections.ArrayList
function Add-Step([string]$what, [scriptblock]$do) { [void]$plan.Add([pscustomobject]@{ What = $what; Do = $do }) }

# The booth Ethernet adapter: wired, physical, not Wi-Fi.
if ($AdapterName) {
  $nic = Get-NetAdapter -Name $AdapterName
} else {
  $nic = Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' -and $_.InterfaceDescription -notmatch 'Wireless|Wi-?Fi|WLAN|USB' } | Sort-Object @{ Expression = { $_.Status -eq 'Up' }; Descending = $true } | Select-Object -First 1
}
if (-not $nic) { throw 'No built-in Ethernet adapter found. Use -AdapterName "Ethernet".' }
Write-Host "Booth network adapter: $($nic.Name) ($($nic.InterfaceDescription)), $($nic.Status)"

# 1. PC name
if ($env:COMPUTERNAME -ne $ComputerName) {
  Add-Step "Rename this PC from $env:COMPUTERNAME to $ComputerName (needs a restart)" { Rename-Computer -NewName $ComputerName -Force | Out-Null; $script:needRestart = $true }
}

# 2. Fixed address on the booth Ethernet, no gateway (internet comes over Wi-Fi)
$ip4 = Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
$ipIf = Get-NetIPInterface -InterfaceIndex $nic.ifIndex -AddressFamily IPv4
if (-not ($ip4 | Where-Object { $_.IPAddress -eq $BoothIp -and $_.PrefixLength -eq $PrefixLength -and $_.PrefixOrigin -eq 'Manual' }) -or $ipIf.Dhcp -eq 'Enabled') {
  Add-Step "Set $($nic.Name) to the fixed address $BoothIp/$PrefixLength with no gateway" {
    Set-NetIPInterface -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -Dhcp Disabled
    Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetRoute -InterfaceIndex $nic.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $BoothIp -PrefixLength $PrefixLength | Out-Null
    Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ResetServerAddresses
  }
}

# 3. Private network (Dante and the phones can't find the PC on Public)
$prof = Get-NetConnectionProfile -InterfaceIndex $nic.ifIndex -ErrorAction SilentlyContinue
if ($prof -and $prof.NetworkCategory -ne 'Private') {
  Add-Step "Set the booth network to Private (now $($prof.NetworkCategory))" { Set-NetConnectionProfile -InterfaceIndex $nic.ifIndex -NetworkCategory Private }
}

# 4. Energy Efficient Ethernet off (Dante needs it off), and Windows not allowed to turn the adapter off
$eee = Get-NetAdapterAdvancedProperty -Name $nic.Name -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Energy.?Efficient|Green Ethernet|\bEEE\b' }
foreach ($p in $eee) {
  $offVal = $p.ValidDisplayValues | Where-Object { $_ -match '^(Off|Disabled)$' } | Select-Object -First 1
  if ($offVal -and $p.DisplayValue -ne $offVal) {
    $pn = $p.DisplayName; $ov = $offVal
    $nicName = $nic.Name
    Add-Step "Set '$pn' to $ov on $nicName" ({ Set-NetAdapterAdvancedProperty -Name $nicName -DisplayName $pn -DisplayValue $ov -NoRestart }.GetNewClosure())
  }
}
$pm = Get-NetAdapterPowerManagement -Name $nic.Name -ErrorAction SilentlyContinue
if ($pm -and $pm.AllowComputerToTurnOffDevice -ne 'Disabled') {
  Add-Step "Stop Windows turning off $($nic.Name) to save power" { Set-NetAdapterPowerManagement -Name $nic.Name -AllowComputerToTurnOffDevice Disabled -NoRestart }
}

# 5. Power settings (always re-applied; powercfg is quick and harmless to repeat)
Add-Step 'Power: High performance, never sleep or turn the screen off, hibernate and fast start-up off, USB selective suspend off, power button = shut down, UPS = shut down at 30%' {
  $hp = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
  & powercfg /setactive $hp
  foreach ($t in 'standby-timeout-ac', 'monitor-timeout-ac', 'disk-timeout-ac', 'hibernate-timeout-ac', 'standby-timeout-dc', 'hibernate-timeout-dc') { & powercfg /change $t 0 }
  & powercfg /hibernate off
  & powercfg /setacvalueindex $hp 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4320-8f2d-1c4b5b4a5d4b 0   # USB selective suspend: off
  & powercfg /setacvalueindex $hp 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f1c09935c3 3   # power button: shut down
  & powercfg /setdcvalueindex $hp 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f1c09935c3 3
  & powercfg /setdcvalueindex $hp e73a048d-bf27-4f12-9731-8b2076e8891f 9a66d8d7-4ff7-4ef9-b5a2-5a326ca2a469 30  # UPS: critical level 30%
  & powercfg /setdcvalueindex $hp e73a048d-bf27-4f12-9731-8b2076e8891f 637ea02f-bbcb-4015-8e2c-a1c7b9c0b546 3   # UPS: critical action = shut down
  & powercfg /setactive $hp
  New-Item -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Force | Out-Null
  Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -Type DWord -Value 0
}

# 6. Windows Update: the same settings Group Policy writes
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
$au = Join-Path $wu 'AU'
$wantAU = if ($UpdateMode -eq 'Off') { @{ NoAutoUpdate = 1 } } else { @{ NoAutoUpdate = 0; AUOptions = 2 } }
$wantAU.NoAutoRebootWithLoggedOnUsers = 1
$wantWU = @{ ExcludeWUDriversInQualityUpdate = 1; SetActiveHours = 1; ActiveHoursStart = $ActiveHoursStart; ActiveHoursEnd = $ActiveHoursEnd }
function Get-RegVal($path, $name) { try { (Get-ItemProperty -Path $path -Name $name -ErrorAction Stop).$name } catch { $null } }
$wuWrong = @($wantAU.Keys | Where-Object { (Get-RegVal $au $_) -ne $wantAU[$_] }) + @($wantWU.Keys | Where-Object { (Get-RegVal $wu $_) -ne $wantWU[$_] })
if ($UpdateMode -eq 'Off' -and $null -ne (Get-RegVal $au 'AUOptions')) { $wuWrong += 'AUOptions' }
if ($wuWrong.Count) {
  $desc = if ($UpdateMode -eq 'Off') { 'no automatic updates at all' } else { 'notify only: nothing downloads or restarts until someone clicks' }
  Add-Step "Windows Update: $desc; no restarts while signed in; no driver updates; active hours $ActiveHoursStart`:00-$ActiveHoursEnd`:00" {
    New-Item -Path $au -Force | Out-Null
    foreach ($k in $wantAU.Keys) { Set-ItemProperty -Path $au -Name $k -Type DWord -Value $wantAU[$k] }
    if ($UpdateMode -eq 'Off') { Remove-ItemProperty -Path $au -Name AUOptions -ErrorAction SilentlyContinue }
    foreach ($k in $wantWU.Keys) { Set-ItemProperty -Path $wu -Name $k -Type DWord -Value $wantWU[$k] }
  }
}
$sig = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Signature Updates'
if ((Get-RegVal $sig 'UpdateOnStartUp') -ne 1) {
  Add-Step 'Antivirus: check for new definitions at every start-up (no restart needed)' {
    New-Item -Path $sig -Force | Out-Null
    Set-ItemProperty -Path $sig -Name UpdateOnStartUp -Type DWord -Value 1
  }
}

# 7. No Windows tips, suggested apps or "finish setting up" screens
$cc = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent'
if ((Get-RegVal $cc 'DisableWindowsConsumerFeatures') -ne 1 -or (Get-RegVal $cc 'DisableSoftLanding') -ne 1) {
  Add-Step 'Turn off Windows tips and suggested apps' {
    New-Item -Path $cc -Force | Out-Null
    Set-ItemProperty -Path $cc -Name DisableWindowsConsumerFeatures -Type DWord -Value 1
    Set-ItemProperty -Path $cc -Name DisableSoftLanding -Type DWord -Value 1
  }
}

# 8. Clock: keep Windows Time running
$w32 = Get-Service w32time
if ($w32.StartType -ne 'Automatic' -or $w32.Status -ne 'Running') {
  Add-Step 'Start the Windows Time service automatically' { Set-Service w32time -StartupType Automatic; Start-Service w32time }
}

# ---- Show the plan and ask ------------------------------------------------------------------------------
Write-Host ''
if ($plan.Count -eq 0) {
  Write-Host 'Everything is already set. Nothing to change.' -ForegroundColor Green
} else {
  Write-Host 'This will change:' -ForegroundColor Cyan
  $i = 1; foreach ($s in $plan) { Write-Host ("  {0}. {1}" -f $i, $s.What); $i++ }
  Write-Host ''
  $go = $Yes
  if (-not $go) { $go = (Read-Host 'Type YES to make these changes') -ceq 'YES' }
  if (-not $go) { Write-Host 'Nothing was changed.'; Stop-Transcript | Out-Null; return }
  $script:needRestart = $false
  foreach ($s in $plan) {
    try { & $s.Do; Write-Host "  done: $($s.What)" -ForegroundColor Green }
    catch { Write-Warning "  FAILED: $($s.What): $($_.Exception.Message)" }
  }
  & gpupdate /target:computer /force | Out-Null
}

# ---- Things this script can't do -----------------------------------------------------------------------
Write-Host ''
Write-Host 'Still to do by hand:' -ForegroundColor Cyan
$auto = Get-RegVal 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' 'AutoAdminLogon'
if ($auto -ne '1') { Write-Host '  - Automatic sign-in: run Microsoft Sysinternals Autologon and enter the booth user and password.' }
else { Write-Host '  - Automatic sign-in: already on.' }
Write-Host '  - BIOS (F10 at start-up): After Power Loss = Power On, S5 Maximum Power Savings = Disable (see the README). Windows can''t change these.'
Write-Host '  - Wi-Fi for internet: join the church Wi-Fi.'
Write-Host '  - Then run Install-C3Life.ps1, and Check-C3LifeServer.ps1 to check everything.'
if ($script:needRestart) { Write-Host ''; Write-Host 'Restart the PC to finish renaming it.' -ForegroundColor Yellow }
Stop-Transcript | Out-Null
