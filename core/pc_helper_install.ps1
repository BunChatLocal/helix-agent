#Requires -Version 5.1
# Allowlisted installs after the person clicked Yes in Zork. No caller-supplied
# commands. Known item ids only.
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$SessionId,
    [Parameter(Mandatory = $true)][string]$Secret,
    [string]$Items = '',
    [string]$JobFile = '',
    [string]$DoneFile = ''
)

# Trays from before October start this script in a visible console, and a pupil
# who sees it may close it mid-install. This file is fetched fresh for every job,
# so hiding the window here reaches those PCs before their tray is replaced.
try {
    Add-Type -Namespace ZorkSelf -Name Win -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
    $selfHwnd = [ZorkSelf.Win]::GetConsoleWindow()
    if ($selfHwnd -ne [IntPtr]::Zero) { [void][ZorkSelf.Win]::ShowWindow($selfHwnd, 0) }
} catch { }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Allowed = @{
    teams        = $true
    winupdate    = $true
    appsanywhere = $true
    office       = $true
    word         = $true
    excel        = $true
    powerpoint   = $true
    outlook      = $true
    onenote      = $true
    onedrive     = $true
    sketchup     = $true
    fridnett     = $true
    skoleprogrammer = $true
    printer      = $true
}

$script:SkipHelixHttp = $false
$script:EdgeDebugPort = 0
$script:ProgressFile = Join-Path $env:LOCALAPPDATA 'ZorkHelper\progress.txt'
$script:CaptiveFlag = Join-Path $env:LOCALAPPDATA 'ZorkHelper\captive.flag'

function Set-CaptiveOffline {
    param([bool]$On)
    $script:SkipHelixHttp = $On
    try {
        $dir = Split-Path -Parent $script:CaptiveFlag
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        if ($On) {
            Set-Content -LiteralPath $script:CaptiveFlag -Value '1' -Encoding ASCII
        } else {
            Remove-Item -LiteralPath $script:CaptiveFlag -Force -ErrorAction SilentlyContinue
        }
    } catch { }
}

function Write-LocalProgress {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return }
    try {
        $dir = Split-Path -Parent $script:ProgressFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        [IO.File]::WriteAllText($script:ProgressFile, $Text)
    } catch { }
}

function Get-ZorkSha256 {
    param([string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($Path)))
    } finally {
        $sha.Dispose()
    }
}

function Update-HelperTray {
    $homeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
    if (-not (Test-Path -LiteralPath $homeDir)) { return }
    $base = $BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId
    $assets = @(
        @{ key = 'tray'; path = '/tray'; dest = (Join-Path $homeDir 'zork-helper-tray.ps1'); mirror = $null; min = 2000 },
        @{ key = 'open'; path = '/open'; dest = (Join-Path $homeDir 'zork-helper-open.ps1'); mirror = $null; min = 500 },
        @{ key = 'install'; path = '/install-script'; dest = (Join-Path $homeDir 'zork-helper-install.ps1'); mirror = (Join-Path $homeDir 'install-script.ps1'); min = 2000 }
    )
    foreach ($asset in $assets) {
        $tmp = Join-Path $env:TEMP ('zork-update-' + [string]$asset.key + '-' + $SessionId + '.ps1')
        try {
            $wc = New-Object System.Net.WebClient
            $wc.Encoding = [Text.Encoding]::UTF8
            $wc.Headers.Add('X-Helper-Secret', $Secret)
            $wc.DownloadFile($base + [string]$asset.path, $tmp)
            if ((Test-Path -LiteralPath $tmp) -and ((Get-Item -LiteralPath $tmp).Length -gt [int]$asset.min)) {
                $dest = [string]$asset.dest
                # Rewriting an unchanged tray would make it restart after every job.
                $same = $false
                if (Test-Path -LiteralPath $dest) {
                    $same = ((Get-ZorkSha256 $tmp) -eq (Get-ZorkSha256 $dest))
                }
                if (-not $same) {
                    Copy-Item -LiteralPath $tmp -Destination $dest -Force
                    if ($asset.key -eq 'tray') { $script:TrayReplaced = $true }
                }
                if ($asset.mirror) {
                    Copy-Item -LiteralPath $tmp -Destination ([string]$asset.mirror) -Force
                }
            }
        } catch { }
        try { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } catch { }
    }
}

function Send-Helper {
    param([string]$Path, [hashtable]$Body)
    if ($script:SkipHelixHttp) { return }
    $uri = ($BaseUrl.TrimEnd('/') + $Path)
    $json = $Body | ConvertTo-Json -Compress -Depth 6
    $req = [Net.HttpWebRequest]::Create($uri)
    $req.Method = 'POST'
    $req.Timeout = 2500
    $req.ReadWriteTimeout = 2500
    $req.KeepAlive = $false
    $req.ContentType = 'application/json; charset=utf-8'
    $req.Headers.Add('X-Helper-Secret', $Secret)
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $req.ContentLength = $bytes.Length
    $stream = $req.GetRequestStream()
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Close() }
    $resp = $req.GetResponse()
    try { $resp.Close() } catch { }
}

function Note {
    param([string]$Text, [string]$Activity = $null)
    Write-LocalProgress $Text
    $body = @{ text = $Text }
    if ($PSBoundParameters.ContainsKey('Activity')) { $body.activity = $Activity }
    try { Send-Helper -Path "/api/helper/$SessionId/progress" -Body $body } catch { }
}

function Write-InstallDone {
    if ([string]::IsNullOrWhiteSpace($DoneFile)) { return }
    try {
        $dir = Split-Path -Parent $DoneFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        Set-Content -LiteralPath $DoneFile -Value '1' -Encoding ASCII
    } catch { }
}

function Get-Wanted {
    if (-not [string]::IsNullOrWhiteSpace($JobFile) -and (Test-Path -LiteralPath $JobFile)) {
        try { $Items = [IO.File]::ReadAllText($JobFile).Trim() } catch { }
    }
    $out = New-Object System.Collections.ArrayList
    foreach ($raw in @($Items -split ',')) {
        $key = ([string]$raw).Trim().ToLower()
        if ($Allowed.ContainsKey($key) -and -not $out.Contains($key)) {
            [void]$out.Add($key)
        }
    }
    return , @($out.ToArray())
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Unelevate {
    if ('ZorkUnelevate' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class ZorkUnelevate {
    [DllImport("user32.dll")] public static extern IntPtr GetShellWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr proc, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] public static extern bool DuplicateTokenEx(IntPtr existing, uint access, IntPtr sa, int impersonation, int type, out IntPtr dup);
    [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern bool CreateProcessWithTokenW(IntPtr token, uint logon, string app, StringBuilder cmd, uint flags, IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb;
        public string lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess, hThread;
        public uint dwProcessId, dwThreadId;
    }
    public static string Launch(string file, string arguments) {
        IntPtr shell = GetShellWindow();
        if (shell == IntPtr.Zero) return "no-shell";
        uint pid;
        GetWindowThreadProcessId(shell, out pid);
        IntPtr proc = OpenProcess(0x1400, false, pid);
        if (proc == IntPtr.Zero) return "open-process";
        IntPtr token;
        if (!OpenProcessToken(proc, 0x0002, out token)) { CloseHandle(proc); return "open-token"; }
        IntPtr primary;
        if (!DuplicateTokenEx(token, 0xf01ff, IntPtr.Zero, 2, 1, out primary)) {
            CloseHandle(token); CloseHandle(proc); return "dup-token";
        }
        STARTUPINFO si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        si.lpDesktop = "winsta0\\default";
        si.dwFlags = 0x0001;
        si.wShowWindow = 1;
        string line = "\"" + file + "\"";
        if (!string.IsNullOrEmpty(arguments)) line += " " + arguments;
        StringBuilder cmd = new StringBuilder(line);
        PROCESS_INFORMATION pi = new PROCESS_INFORMATION();
        bool ok = CreateProcessWithTokenW(primary, 0, null, cmd, 0, IntPtr.Zero, null, ref si, out pi);
        int err = Marshal.GetLastWin32Error();
        CloseHandle(token); CloseHandle(primary); CloseHandle(proc);
        if (pi.hThread != IntPtr.Zero) CloseHandle(pi.hThread);
        if (pi.hProcess != IntPtr.Zero) CloseHandle(pi.hProcess);
        return ok ? "ok" : ("create-" + err);
    }
}
'@
}

function Set-InstallBusyFlag {
    param([bool]$On)
    $f = Join-Path $env:LOCALAPPDATA 'ZorkHelper\install-busy.flag'
    try {
        $dir = Split-Path -Parent $f
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        if ($On) { Set-Content -LiteralPath $f -Value '1' -Encoding ASCII }
        else { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Restart-HelperTray {
    $homeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
    $tray = Join-Path $homeDir 'zork-helper-tray.ps1'
    if (-not (Test-Path -LiteralPath $tray)) { return }
    Set-InstallBusyFlag $true
    $pidFile = Join-Path $homeDir 'tray.pid'
    try {
        if (Test-Path -LiteralPath $pidFile) {
            $old = 0
            [void][int]::TryParse(([IO.File]::ReadAllText($pidFile)).Trim(), [ref]$old)
            if ($old -gt 0) { Stop-Process -Id $old -Force -ErrorAction SilentlyContinue }
        }
    } catch { }
    Start-Sleep -Milliseconds 500
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arg = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '"'
    $started = $false
    if (Test-Admin) {
        try {
            Ensure-Unelevate
            $started = ([ZorkUnelevate]::Launch($ps, $arg) -eq 'ok')
        } catch { $started = $false }
    }
    if (-not $started) {
        try {
            Start-Process -FilePath $ps -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-STA', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $tray
            ) | Out-Null
        } catch { }
    }
}

function Find-Winget {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) { return [string]$cmd.Source }
    $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $alias) { return $alias }
    return ''
}

function Install-TeamsWork {
    Note 'Installerer Teams (jobb eller skole)...'
    $winget = Find-Winget
    if ([string]::IsNullOrWhiteSpace($winget)) {
        throw 'winget ble ikke funnet pa denne PC-en.'
    }
    $args = @(
        'install', '--id', 'Microsoft.Teams', '--source', 'winget',
        '--accept-package-agreements', '--accept-source-agreements',
        '--disable-interactivity'
    )
    $p = Start-Process -FilePath $winget -ArgumentList $args -Wait -PassThru -WindowStyle Hidden
    if ($p.ExitCode -and $p.ExitCode -ne 0 -and $p.ExitCode -ne -1978335189) {
        throw "Teams-installasjon avsluttet med kode $($p.ExitCode)"
    }
    Note 'Teams: ferdig.'
}

function Install-WindowsUpdates {
    Note 'Laster ned Windows-oppdateringer...'
    $session = New-Object -ComObject Microsoft.Update.Session
    $session.ClientApplicationID = 'ZorkHelper'
    $searcher = $session.CreateUpdateSearcher()
    $searcher.Online = $true
    $found = $searcher.Search('IsInstalled=0 and IsHidden=0')
    $n = 0
    try { $n = [int]$found.Updates.Count } catch { $n = 0 }
    if ($n -eq 0) {
        Note 'Ingen ventende Windows-oppdateringer.'
        return
    }
    $coll = New-Object -ComObject Microsoft.Update.UpdateColl
    for ($i = 0; $i -lt $n; $i++) {
        $u = $found.Updates.Item($i)
        try {
            if (-not $u.EulaAccepted) { $u.AcceptEula() }
        } catch { }
        [void]$coll.Add($u)
    }
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $coll
    $dl = $downloader.Download()
    $dlCode = 0
    try { $dlCode = [int]$dl.ResultCode } catch { $dlCode = 0 }
    if ($dlCode -eq 4 -or $dlCode -eq 5) {
        Note 'Klarte ikke laste ned Windows-oppdateringer.'
        return
    }
    Note 'Installerer Windows-oppdateringer...'
    $installer = $session.CreateUpdateInstaller()
    $installer.Updates = $coll
    $result = $installer.Install()
    $code = 0
    $reboot = $false
    try { $code = [int]$result.ResultCode } catch { $code = 0 }
    try { $reboot = [bool]$result.RebootRequired } catch { $reboot = $false }
    if ($code -eq 2 -or $code -eq 3) {
        if ($reboot) {
            Note 'Windows Update er installert. Start PC-en pa nytt for a fullfore.'
        } elseif ($code -eq 3) {
            Note 'Windows Update fullforte med noen feil.'
        } else {
            Note 'Windows Update er installert.'
        }
        return
    }
    if ($code -eq 4 -or $code -eq 5) {
        Note 'Windows Update feilet. Prov fra Innstillinger.'
        return
    }
    Note 'Windows Update er ferdig.'
}

function Invoke-Netsh {
    param(
        [Parameter(Mandatory = $true)][string[]]$NetshArgs,
        [int]$TimeoutMs = 8000
    )
    $netsh = Join-Path $env:SystemRoot 'System32\netsh.exe'
    $tag = [guid]::NewGuid().ToString('N')
    $outFile = Join-Path $env:TEMP ('zork-netsh-' + $tag + '.out')
    $errFile = Join-Path $env:TEMP ('zork-netsh-' + $tag + '.err')
    try {
        $p = Start-Process -FilePath $netsh -ArgumentList $NetshArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        if ($null -eq $p) {
            return @{ Ok = $false; TimedOut = $false; Text = ''; Code = -1 }
        }
        if (-not $p.WaitForExit($TimeoutMs)) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch { }
            return @{ Ok = $false; TimedOut = $true; Text = ''; Code = -1 }
        }
        $text = ''
        try { $text = [IO.File]::ReadAllText($outFile) } catch { }
        try { $text += [IO.File]::ReadAllText($errFile) } catch { }
        return @{ Ok = ($p.ExitCode -eq 0); TimedOut = $false; Text = $text; Code = $p.ExitCode }
    } catch {
        return @{ Ok = $false; TimedOut = $false; Text = [string]$_.Exception.Message; Code = -1 }
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Clip-NetshNote {
    param([string]$Text, [int]$Max = 160)
    $t = ([string]$Text -replace '\s+', ' ').Trim()
    if ($t.Length -le $Max) { return $t }
    return $t.Substring(0, $Max)
}

function Get-WlanInterface {
    try {
        $n = Get-NetAdapter -ErrorAction Stop |
            Where-Object {
                $_.Status -eq 'Up' -and (
                    $_.Name -match 'Wi-?Fi|Wireless|WLAN' -or
                    $_.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11|WLAN'
                )
            } |
            Select-Object -First 1 -ExpandProperty Name
        if ($n) { return [string]$n }
    } catch { }
    return ''
}

function Add-WlanInterfaceArg {
    param([string[]]$Parts, [string]$Iface)
    if ([string]::IsNullOrWhiteSpace($Iface)) { return $Parts }
    return @($Parts + ('interface="' + $Iface + '"'))
}

function Get-WifiSsid {
    # The real 802.11 SSID comes from netsh. The network *name* from
    # Get-NetConnectionProfile is unreliable for this: on a domain-joined FRID
    # network it reads back as "ad.viken.no", not "FRID", so the FRID/FRID-iot
    # checks would never match. Read netsh first; fall back to the profile name
    # only when netsh gives us nothing (adapter naming, timeout).
    $ssid = ''
    $state = ''
    $r = Invoke-Netsh -NetshArgs @('wlan', 'show', 'interfaces') -TimeoutMs 6000
    foreach ($line in @($r.Text -split "`r?`n")) {
        if ($line -match '^\s*BSSID\s') { continue }
        if ($line -match '^\s*SSID(?:\s+\d+)?\s*:\s*(.+)\s*$') {
            $ssid = $Matches[1].Trim()
        }
        if ($line -match '^\s*(State|Tilstand)\s*:\s*(.+)\s*$') {
            $state = $Matches[2].Trim().ToLower()
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($ssid)) {
        $up = ($state -eq 'connected' -or $state -eq 'tilkoblet')
        return @{ Ssid = $ssid; Connected = $up }
    }
    try {
        $profiles = @(Get-NetConnectionProfile -ErrorAction Stop)
        foreach ($p in $profiles) {
            $alias = ''
            try { $alias = [string]$p.InterfaceAlias } catch { $alias = '' }
            if ($alias -notmatch 'Wi-?Fi|Wireless|WLAN') { continue }
            $n = [string]$p.Name
            if (-not [string]::IsNullOrWhiteSpace($n) -and $n -ne '*') {
                return @{ Ssid = $n.Trim(); Connected = $true }
            }
        }
    } catch { }
    return @{ Ssid = ''; Connected = $false }
}

function Get-SsidMatch {
    param([string]$Got, [string]$Want)
    return (-not [string]::IsNullOrWhiteSpace($Got)) -and ($Got.Trim() -ieq $Want)
}

function Test-ConnectedSsid {
    param([string]$Name)
    $wifi = Get-WifiSsid
    return ($wifi.Connected -and (Get-SsidMatch $wifi.Ssid $Name))
}

function Test-SsidInRange {
    param([string]$Name)
    $try = 0
    $unknown = $false
    while ($try -lt 2) {
        $try++
        $r = Invoke-Netsh -NetshArgs @('wlan', 'show', 'networks') -TimeoutMs 8000
        if ($r.TimedOut) {
            $unknown = $true
        } else {
            $listed = $false
            foreach ($line in @($r.Text -split "`r?`n")) {
                if ($line -match '^\s*SSID\s+\d+\s*:\s*(.+)\s*$') {
                    $listed = $true
                    if (Get-SsidMatch $Matches[1] $Name) { return $true }
                }
            }
            if ($listed) { return $false }
            $unknown = $true
        }
        if ($try -lt 2) { Start-Sleep -Seconds 2 }
    }
    if ($unknown) { return $null }
    return $false
}

function Wait-ConnectedSsid {
    param([string]$Name, [int]$Seconds = 40)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-ConnectedSsid $Name) { return $true }
    }
    return $false
}

function Add-FridIotProfile {
    $iot = 'FRID-iot'
    $xmlPath = Join-Path $env:TEMP ('zork-frid-iot-' + $SessionId + '.xml')
    $xml = @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>$iot</name>
  <SSIDConfig>
    <SSID>
      <hex>465249442d696f74</hex>
      <name>$iot</name>
    </SSID>
    <nonBroadcast>false</nonBroadcast>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>friddeploy</keyMaterial>
      </sharedKey>
    </security>
  </MSM>
  <MacRandomization xmlns="http://www.microsoft.com/networking/WLAN/profile/v3">
    <enableRandomization>false</enableRandomization>
  </MacRandomization>
</WLANProfile>
"@
    [System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::ASCII)
    try {
        $quoted = 'filename="' + $xmlPath + '"'
        $added = Invoke-Netsh -NetshArgs @('wlan', 'add', 'profile', $quoted, 'user=all') -TimeoutMs 12000
        $ok = (-not $added.TimedOut) -and $added.Ok -and ($added.Text -notmatch '(?i)error|failed|feil')
        if (-not $ok) {
            $added = Invoke-Netsh -NetshArgs @('wlan', 'add', 'profile', $quoted, 'user=current') -TimeoutMs 12000
            $ok = (-not $added.TimedOut) -and $added.Ok -and ($added.Text -notmatch '(?i)error|failed|feil')
        }
        if (-not $ok) {
            $why = Clip-NetshNote $added.Text
            if ($added.TimedOut) { $why = 'netsh svarte ikke' }
            if ([string]::IsNullOrWhiteSpace($why)) { $why = 'ukjent feil' }
            throw ('netsh avviste wifi-profilen: ' + $why)
        }
    } finally {
        Remove-Item -LiteralPath $xmlPath -Force -ErrorAction SilentlyContinue
    }
}

function Connect-FridIot {
    $iot = 'FRID-iot'
    Note 'Sjekker wifi...'
    $wifi = Get-WifiSsid
    if ($wifi.Connected -and (Get-SsidMatch $wifi.Ssid 'FRID')) {
        Note 'Done! Du er koblet til riktig nettverk (FRID).'
        return 'frid'
    }

    $from = $wifi.Ssid
    if ([string]::IsNullOrWhiteSpace($from)) { $from = 'dette nettverket' }
    Note ('Pa ' + $from + ' na. Ser etter FRID-iot...')

    $seen = Test-SsidInRange $iot
    if ($seen -eq $false) {
        Note 'Ser ikke FRID-iot i lista. Prover a koble til likevel.'
    } elseif ($null -eq $seen) {
        Note 'Wifi-sok svarte ikke. Prover a koble til FRID-iot likevel.'
    }

    $iface = Get-WlanInterface
    $attempt = 0
    while ($attempt -lt 3) {
        $attempt++
        Note ('Kobler fra ' + $from + ' og over til FRID-iot (' + $attempt + '/3)...')
        $disc = Add-WlanInterfaceArg -Parts @('wlan', 'disconnect') -Iface $iface
        $dr = Invoke-Netsh -NetshArgs $disc -TimeoutMs 8000
        if ($dr.TimedOut) { Note 'netsh svarte ikke ved frakobling. Prover videre.' }
        Start-Sleep -Seconds 1
        [void](Invoke-Netsh -NetshArgs @('wlan', 'delete', 'profile', ('name=' + $iot)) -TimeoutMs 8000)
        Add-FridIotProfile
        $connArgs = Add-WlanInterfaceArg -Parts @('wlan', 'connect', ('name=' + $iot), ('ssid=' + $iot)) -Iface $iface
        $r = Invoke-Netsh -NetshArgs $connArgs -TimeoutMs 15000
        if ($r.TimedOut) {
            Note 'netsh svarte ikke. Prover igjen...'
            continue
        }
        if (-not [string]::IsNullOrWhiteSpace($r.Text)) {
            $clip = Clip-NetshNote $r.Text
            if ($clip -and $clip -match '(?i)error|failed|feil|not found|ikke|denied') {
                Note ('Windows: ' + $clip)
            }
        }
        if (Wait-ConnectedSsid $iot 20) { return 'iot' }
        $wifi = Get-WifiSsid
        if ($wifi.Connected -and -not [string]::IsNullOrWhiteSpace($wifi.Ssid)) { $from = $wifi.Ssid }
    }
    throw 'Klarte ikke a koble til FRID-iot. Prov igjen pa skolen, med wifi pa.'
}

function Connect-FridAndForgetIot {
    $iot = 'FRID-iot'
    $candidates = New-Object System.Collections.ArrayList
    [void]$candidates.Add('FRID')
    $listed = Invoke-Netsh -NetshArgs @('wlan', 'show', 'profiles') -TimeoutMs 8000
    foreach ($line in @($listed.Text -split "`r?`n")) {
        if ($line -notmatch ':\s*(.+?)\s*$') { continue }
        $cand = $Matches[1].Trim()
        if (-not $cand) { continue }
        if ($cand -ieq $iot) { continue }
        if ($cand -notmatch '(?i)^FRID') { continue }
        if (-not $candidates.Contains($cand)) { [void]$candidates.Add($cand) }
    }
    $iface = Get-WlanInterface
    foreach ($name in @($candidates.ToArray())) {
        [void](Invoke-Netsh -NetshArgs (Add-WlanInterfaceArg -Parts @('wlan', 'disconnect') -Iface $iface) -TimeoutMs 8000)
        Start-Sleep -Seconds 1
        $connArgs = Add-WlanInterfaceArg -Parts @('wlan', 'connect', ('name=' + $name), 'ssid=FRID') -Iface $iface
        [void](Invoke-Netsh -NetshArgs $connArgs -TimeoutMs 15000)
        if (Wait-ConnectedSsid 'FRID' 20) {
            [void](Invoke-Netsh -NetshArgs @('wlan', 'delete', 'profile', ('name=' + $iot)) -TimeoutMs 8000)
            Note 'Koblet til FRID. FRID-iot er fjernet.'
            return
        }
    }
    Note 'Sertifikatet er installert. Koble til FRID i wifi-listen hvis du ikke er der enna. Hvis FRID ber om brukernavn og passord, ga til IT Helpdesk i 1. etasje.'
}

function Ensure-FridUi {
    if ('ZorkFridUi' -as [type]) { return }
    Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class ZorkFridUi {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr hWnd, EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    public const uint BM_CLICK = 0x00F5;
    static string Title(IntPtr hWnd) {
        StringBuilder sb = new StringBuilder(512);
        GetWindowText(hWnd, sb, sb.Capacity);
        return sb.ToString().Trim();
    }
    static string Norm(string text) {
        if (string.IsNullOrEmpty(text)) return "";
        return text.Replace("&", "").Trim().TrimEnd('>').Trim();
    }
    static List<IntPtr> Nodes(IntPtr root) {
        List<IntPtr> nodes = new List<IntPtr>();
        nodes.Add(root);
        EnumChildWindows(root, (h, l) => { nodes.Add(h); return true; }, IntPtr.Zero);
        return nodes;
    }
    static string BodyText(IntPtr root) {
        StringBuilder all = new StringBuilder();
        foreach (IntPtr h in Nodes(root)) { all.Append(Title(h)); all.Append(' '); }
        return all.ToString().ToLowerInvariant();
    }
    static bool IsWizard(string title) {
        if (string.IsNullOrEmpty(title)) return false;
        string t = title.ToLowerInvariant();
        return t.Contains("quickconnect") || t.Contains("aruba") || t.Contains("clearpass")
            || t.Contains("frid onboard");
    }
    static bool IsCertWarning(string title) {
        if (string.IsNullOrEmpty(title)) return false;
        string t = title.ToLowerInvariant();
        return t.Contains("security warning") || t.Contains("sikkerhetsadvarsel")
            || t.Contains("root certificate") || t.Contains("root ca") || t.Contains("certificate");
    }
    // Only ever confirm a certificate prompt that is the expected FRID/Viken CA.
    // This keeps the helper from ever clicking Ja/Yes on some unrelated dialog
    // (for example the Windows location-access prompt, which the pupil decides).
    static bool ConfirmedFridCert(IntPtr root) {
        string body = BodyText(root);
        return body.Contains("viken root ca") || body.Contains("viken issuing")
            || body.Contains("frid");
    }
    // Hide (nCmdShow=0) or show (nCmdShow=5) the top-level windows owned by the
    // given process ids. Used to tuck the helper's Edge window away after login.
    public static int ShowWindowsForPids(string csvPids, int nCmdShow) {
        HashSet<uint> pids = new HashSet<uint>();
        foreach (string s in csvPids.Split(',')) { uint v; if (uint.TryParse(s.Trim(), out v)) pids.Add(v); }
        int n = 0;
        List<IntPtr> roots = new List<IntPtr>();
        EnumWindows((h, l) => { roots.Add(h); return true; }, IntPtr.Zero);
        foreach (IntPtr root in roots) {
            uint pid; GetWindowThreadProcessId(root, out pid);
            if (pids.Contains(pid) && Title(root).Length > 0) { ShowWindow(root, nCmdShow); n++; }
        }
        return n;
    }
    public static int ClickButtons(int processId, bool allowClose) {
        int flags = 0;
        List<IntPtr> roots = new List<IntPtr>();
        EnumWindows((h, l) => {
            if (IsWindowVisible(h)) roots.Add(h);
            return true;
        }, IntPtr.Zero);
        foreach (IntPtr root in roots) {
            uint pid;
            GetWindowThreadProcessId(root, out pid);
            string title = Title(root);
            bool ownByQc = (pid == (uint)processId);
            bool wizard = IsWizard(title);
            bool cert = IsCertWarning(title);
            if (!ownByQc && !wizard && !cert) continue;

            HashSet<string> go = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (wizard || ownByQc) {
                go.Add("Next"); go.Add("Neste"); go.Add("Connect"); go.Add("Finish");
                if (allowClose) { go.Add("Close"); go.Add("Lukk"); }
            }
            if (cert && ConfirmedFridCert(root)) {
                go.Add("Ja"); go.Add("Yes");
            }
            if (go.Count == 0) continue;

            foreach (IntPtr h in Nodes(root)) {
                string text = Norm(Title(h));
                if (text.Length == 0 || !go.Contains(text)) continue;
                SendMessage(h, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
                flags |= 1;
                if (text.Equals("Connect", StringComparison.OrdinalIgnoreCase)
                    || text.Equals("Finish", StringComparison.OrdinalIgnoreCase)) flags |= 2;
            }
        }
        return flags;
    }
}
"@
}

function Start-FridWizardClicks {
    param($Process)
    $qcPid = 0
    try { $qcPid = [int]$Process.Id } catch { $qcPid = 0 }
    try { Ensure-FridUi } catch {
        Note 'Klikk Next og Ja i vinduet hvis det blir staaende.'
        try { if (-not $Process.HasExited) { $Process.WaitForExit(480000) | Out-Null } } catch { }
        return
    }
    $allowClose = $false
    $deadline = (Get-Date).AddMinutes(8)
    $goneStreak = 0
    # ArubaQuickConnect.exe is a self-extractor: it unpacks and launches the real
    # QuickConnect.exe wizard, then can exit on its own. So don't stop when the
    # process we started exits — keep clicking Next/Ja/Connect/Finish until every
    # QuickConnect process is gone (a short streak guards against a momentary gap).
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        try {
            $flags = [ZorkFridUi]::ClickButtons($qcPid, $allowClose)
            if (($flags -band 2) -ne 0) { $allowClose = $true }
        } catch { }
        $alive = @(Get-FridQuickConnectProcesses)
        if ($alive.Count -eq 0) {
            $goneStreak++
            if ($goneStreak -ge 4) { break }
        } else {
            $goneStreak = 0
        }
    }
}

function Get-FridQuickConnectProcesses {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.ProcessName -match '(?i)ArubaQuickConnect|QuickConnect' }
}

function Get-EdgeExe {
    foreach ($c in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )) {
        if ($c -and (Test-Path -LiteralPath $c)) { return [string]$c }
    }
    try {
        $cmd = Get-Command msedge.exe -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { return [string]$cmd.Source }
    } catch { }
    return ''
}

function Get-EdgeProfileDir {
    return (Join-Path $env:TEMP 'ZorkHelperEdge')
}

function Get-EdgeDebugPort {
    if ($script:EdgeDebugPort -gt 0) { return [int]$script:EdgeDebugPort }
    $script:EdgeDebugPort = 18793
    return [int]$script:EdgeDebugPort
}

function Stop-ZorkHelperEdge {
    $mark = 'ZorkHelperEdge'
    try {
        Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.CommandLine -and $_.CommandLine.IndexOf($mark, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch { }
}

function Get-ZorkHelperEdgePids {
    $mark = 'ZorkHelperEdge'
    $ids = New-Object System.Collections.Generic.List[string]
    try {
        Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.CommandLine -and $_.CommandLine.IndexOf($mark, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                [void]$ids.Add([string]$_.ProcessId)
            }
        }
    } catch { }
    return @($ids.ToArray())
}

function Set-ZorkHelperEdgeVisible {
    param([bool]$Visible)
    try { Ensure-FridUi } catch { return }
    $pids = @(Get-ZorkHelperEdgePids)
    if ($pids.Count -eq 0) { return }
    $show = if ($Visible) { 5 } else { 0 }   # SW_SHOW / SW_HIDE
    try { [void][ZorkFridUi]::ShowWindowsForPids([string]::Join(',', $pids), $show) } catch { }
}

# Only ever run an Aruba QuickConnect the pupil just downloaded from the portal,
# and only when it carries a valid Hewlett Packard Enterprise signature. There is
# no bundled copy any more: each portal download is personal to the signed-in
# account, so the exe must come from this pupil's own login.
function Test-IsArubaQuickConnect {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $sig = Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
        if ([string]$sig.Status -ne 'Valid') { return $false }
        $subject = ''
        try { $subject = [string]$sig.SignerCertificate.Subject } catch { $subject = '' }
        return ($subject -match '(?i)Hewlett\s*Packard\s*Enterprise')
    } catch {
        return $false
    }
}

function Ensure-EdgeCdp {
    if ('ZorkEdgeCdp' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
public static class ZorkEdgeCdp {
    public static string HttpGet(string url) {
        try {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.Timeout = 1500;
            req.ReadWriteTimeout = 1500;
            using (WebResponse resp = req.GetResponse())
            using (StreamReader sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                return sr.ReadToEnd();
        } catch {
            return "";
        }
    }
    public static string Extract(string json, string key) {
        string needle = "\"" + key + "\":\"";
        int i = json.IndexOf(needle, StringComparison.Ordinal);
        if (i < 0) {
            needle = "\"" + key + "\": \"";
            i = json.IndexOf(needle, StringComparison.Ordinal);
        }
        if (i < 0) return "";
        i += needle.Length;
        int j = json.IndexOf('"', i);
        if (j < 0) return "";
        return json.Substring(i, j - i).Replace("\\/", "/");
    }
    public static bool IsLoginUrl(string url) {
        if (string.IsNullOrEmpty(url)) return false;
        string u = url.ToLowerInvariant();
        if (u.IndexOf("microsoftonline") >= 0) return true;
        if (u.IndexOf("login.microsoft.com") >= 0) return true;
        if (u.IndexOf("login.windows.net") >= 0) return true;
        if (u.IndexOf("frid_provisioning.php") >= 0) return true;
        return false;
    }
    public static bool IsReadyUrl(string url) {
        if (string.IsNullOrEmpty(url)) return false;
        string u = url.ToLowerInvariant();
        return u.IndexOf("gjesteportal.frid-iks.no") >= 0 && u.IndexOf("mdps_status.php") >= 0;
    }
    public static bool IsPortalUrl(string url) {
        if (string.IsNullOrEmpty(url)) return false;
        return url.IndexOf("gjesteportal.frid-iks.no", StringComparison.OrdinalIgnoreCase) >= 0;
    }
    public static string PageJson(int port) {
        return HttpGet("http://127.0.0.1:" + port + "/json");
    }
    public static bool HasReadyPage(int port) {
        string json = PageJson(port);
        if (string.IsNullOrEmpty(json)) return false;
        string[] parts = json.Replace("},{", "}\n{").Split('\n');
        for (int i = 0; i < parts.Length; i++) {
            if (IsReadyUrl(Extract(parts[i], "url"))) return true;
        }
        return false;
    }
    static string JsonString(string s) {
        if (s == null) s = "";
        StringBuilder sb = new StringBuilder();
        sb.Append('"');
        for (int i = 0; i < s.Length; i++) {
            char c = s[i];
            if (c == '"' || c == '\\') { sb.Append('\\'); sb.Append(c); }
            else if (c == '\n') sb.Append("\\n");
            else if (c == '\r') sb.Append("\\r");
            else sb.Append(c);
        }
        sb.Append('"');
        return sb.ToString();
    }
    public static string Eval(string wsUrl, string expression) {
        try {
            using (ClientWebSocket ws = new ClientWebSocket()) {
                ws.ConnectAsync(new Uri(wsUrl), CancellationToken.None).GetAwaiter().GetResult();
                string payload = "{\"id\":1,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":" + JsonString(expression) + ",\"returnByValue\":true}}";
                byte[] send = Encoding.UTF8.GetBytes(payload);
                ws.SendAsync(new ArraySegment<byte>(send), WebSocketMessageType.Text, true, CancellationToken.None).GetAwaiter().GetResult();
                byte[] buf = new byte[65536];
                DateTime until = DateTime.UtcNow.AddSeconds(5);
                StringBuilder sb = new StringBuilder();
                while (DateTime.UtcNow < until) {
                    WebSocketReceiveResult r = ws.ReceiveAsync(new ArraySegment<byte>(buf), CancellationToken.None).GetAwaiter().GetResult();
                    sb.Append(Encoding.UTF8.GetString(buf, 0, r.Count));
                    if (r.EndOfMessage) {
                        string msg = sb.ToString();
                        sb.Length = 0;
                        if (msg.IndexOf("\"id\":1") >= 0 || msg.IndexOf("\"id\": 1") >= 0) return msg;
                    }
                }
                return "timeout";
            }
        } catch (Exception ex) {
            return "ws:" + ex.GetType().Name;
        }
    }
    public static string ClickStartQuickConnect(int port) {
        string json = PageJson(port);
        if (string.IsNullOrEmpty(json)) return "no-json";
        string js = "(function(){function txt(el){var a=el.getAttribute&&el.getAttribute('aria-label');return ((el.innerText||el.textContent||el.value||a||el.title||'')+'').replace(/\\s+/g,' ').trim();}var all=document.querySelectorAll('a,button,input,[role=\"button\"],span,div');for(var i=0;i<all.length;i++){if(/start\\s*quick\\s*connect/i.test(txt(all[i]))){all[i].click();return 'clicked';}}return 'missing';})()";
        string[] parts = json.Replace("},{", "}\n{").Split('\n');
        string last = "no-page";
        for (int i = 0; i < parts.Length; i++) {
            string url = Extract(parts[i], "url");
            if (IsLoginUrl(url)) { last = "login"; continue; }
            if (!IsPortalUrl(url)) continue;
            string ws = Extract(parts[i], "webSocketDebuggerUrl");
            if (string.IsNullOrEmpty(ws)) continue;
            string r = Eval(ws, js);
            if (r.IndexOf("clicked") >= 0) return "clicked";
            last = r;
        }
        return last;
    }
}
'@
}

function Invoke-UiaNamedClick {
    param([string[]]$Names)
    try {
        Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
        Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop
    } catch { return $false }
    try {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $classCond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Chrome_WidgetWin_1')
        $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $classCond)
        foreach ($w in $wins) {
            foreach ($n in $Names) {
                $nameCond = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::NameProperty, $n)
                $el = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $nameCond)
                if ($el) {
                    $pat = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                    if ($pat) { $pat.Invoke(); return $true }
                }
            }
        }
    } catch { }
    return $false
}

function Find-NewQuickConnectExe {
    param([datetime]$After)
    $dirs = @(
        (Join-Path $env:USERPROFILE 'Downloads'),
        (Join-Path $env:USERPROFILE 'Nedlastinger')
    )
    foreach ($d in $dirs) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        $hit = Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '(?i)ArubaQuickConnect|QuickConnect' -and
                $_.Extension -eq '.exe' -and
                $_.LastWriteTime -ge $After.AddSeconds(-5)
            } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($hit) { return [string]$hit.FullName }
    }
    return ''
}

function Get-EdgeOnboardArgLine {
    param([string]$Url, [switch]$NoDebug)
    $profile = Get-EdgeProfileDir
    if ($NoDebug) {
        return ('--user-data-dir="' + $profile + '" --no-first-run --no-default-browser-check --new-window ' + $Url)
    }
    $port = Get-EdgeDebugPort
    return ('--user-data-dir="' + $profile + '" --remote-debugging-port=' + $port + ' --remote-allow-origins=* --no-first-run --no-default-browser-check --new-window ' + $Url)
}

function Test-ZorkHelperEdgeRunning {
    $mark = 'ZorkHelperEdge'
    $running = $false
    try {
        foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" -ErrorAction SilentlyContinue)) {
            if ($p.CommandLine -and $p.CommandLine.IndexOf($mark, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $running = $true
                break
            }
        }
    } catch { }
    return $running
}

function Wait-ZorkEdgeReady {
    param([int]$Seconds = 6, [switch]$RequireCdp)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-ZorkHelperEdgeRunning) {
            if (-not $RequireCdp) { return $true }
            try {
                $json = [ZorkEdgeCdp]::PageJson((Get-EdgeDebugPort))
                if (-not [string]::IsNullOrWhiteSpace($json)) { return $true }
            } catch { }
        }
        Start-Sleep -Milliseconds 350
    }
    return $false
}

function Start-EdgeWithArgs {
    param([string]$Edge, [string]$ArgLine)
    if (-not (Test-Admin)) {
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $Edge
            $psi.Arguments = $ArgLine
            $psi.UseShellExecute = $false
            $psi.WorkingDirectory = (Split-Path -Parent $Edge)
            [void][Diagnostics.Process]::Start($psi)
            return 'ok'
        } catch {
            return ('start:' + [string]$_.Exception.GetType().Name)
        }
    }
    try {
        Ensure-Unelevate
        $res = [string]( [ZorkUnelevate]::Launch($Edge, $ArgLine) )
        if ($res -eq 'ok') { return 'ok' }
        return ('unelevate:' + $res)
    } catch {
        return ('throw:' + [string]$_.Exception.GetType().Name)
    }
}

function Start-EdgeOnboard {
    $url = 'https://gjesteportal.frid-iks.no/onboard/frid_provisioning.php'
    $edge = Get-EdgeExe
    if (-not $edge) { return 'no-edge' }
    Stop-ZorkHelperEdge
    Start-Sleep -Milliseconds 400
    try { Ensure-EdgeCdp } catch { }

    $withCdp = Start-EdgeWithArgs -Edge $edge -ArgLine (Get-EdgeOnboardArgLine $url)
    if ($withCdp -eq 'ok') {
        if (Wait-ZorkEdgeReady -Seconds 8 -RequireCdp) { return 'ok' }
        if (Wait-ZorkEdgeReady -Seconds 2) { return 'ok-nocdp' }
    }

    $withoutCdp = Start-EdgeWithArgs -Edge $edge -ArgLine (Get-EdgeOnboardArgLine -Url $url -NoDebug)
    if ($withoutCdp -eq 'ok') {
        if (Wait-ZorkEdgeReady -Seconds 8) { return 'ok-nocdp' }
        return 'no-window'
    }
    return ($withCdp + '/' + $withoutCdp)
}

function Open-FridOnboardLogin {
    $url = 'https://gjesteportal.frid-iks.no/onboard/frid_provisioning.php'
    $how = ''
    foreach ($i in 1..3) {
        $how = Start-EdgeOnboard
        if ($how -like 'ok*') { break }
        Start-Sleep -Seconds 1
    }
    if ($how -notlike 'ok*') {
        $flag = Join-Path $env:LOCALAPPDATA 'ZorkHelper\open-url.txt'
        try {
            $dir = Split-Path -Parent $flag
            if ($dir -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Force -Path $dir | Out-Null
            }
            [IO.File]::WriteAllText($flag, $url)
        } catch { }
    }
    if ($how -eq 'ok') {
        Note 'Aapner innlogging i Edge. Logg inn der. Zork Helper klikker Start QuickConnect etterpaa.'
        return
    }
    if ($how -eq 'ok-nocdp') {
        try { Start-Process $url | Out-Null } catch { }
        Note 'Aapner innlogging i Edge. Logg inn der. Hvis Start QuickConnect ikke klikkes automatisk, klikk den selv.'
        return
    }
    if (-not [string]::IsNullOrWhiteSpace($how)) {
        Note ('Edge-start: ' + $how)
    }
    Note 'Aapner innlogging i Edge. Hvis vinduet ikke vises, aapne Edge og ga til gjesteportal.frid-iks.no/onboard/frid_provisioning.php.'
}

function Wait-FridQuickConnectProcess {
    param($IgnoreIds, [int]$Seconds = 480)
    $ignore = @()
    if ($null -ne $IgnoreIds) { $ignore = @($IgnoreIds) }
    try { Ensure-EdgeCdp } catch { }
    try { Ensure-FridUi } catch { }
    $port = Get-EdgeDebugPort
    $clickedQc = $false
    $startedDl = $false
    $hidEdge = $false
    $clickAt = Get-Date
    $watchAt = Get-Date
    $notedClick = $false
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $p = Get-FridQuickConnectProcesses |
            Where-Object { $ignore -notcontains $_.Id } |
            Select-Object -First 1
        if ($p) { return $p }
        $ready = $false
        try { $ready = [ZorkEdgeCdp]::HasReadyPage($port) } catch { $ready = $false }

        # Once the pupil has signed in and the portal shows the QuickConnect
        # page, tuck the helper's Edge window out of sight and take over.
        if ($ready -and -not $hidEdge) {
            Set-ZorkHelperEdgeVisible $false
            $hidEdge = $true
        }

        if (-not $clickedQc -and $ready) {
            if (-not $notedClick) {
                Note 'Innlogging ferdig. Zork Helper laster ned og starter QuickConnect...'
                $notedClick = $true
            }
            $clickedNow = $false
            $r = ''
            try { $r = [string][ZorkEdgeCdp]::ClickStartQuickConnect($port) } catch { $r = '' }
            if ($r -match 'clicked') { $clickedNow = $true }
            if (-not $clickedNow) {
                if (Invoke-UiaNamedClick @('Start QuickConnect')) { $clickedNow = $true }
            }
            if ($clickedNow) {
                $clickedQc = $true
                $clickAt = Get-Date
            }
        }

        # If Edge questions a low-reputation download, keep the file.
        if ($clickedQc -and -not $startedDl) {
            [void](Invoke-UiaNamedClick @('Behold', 'Behold likevel', 'Behold fil', 'Keep', 'Keep anyway'))
        }

        # Launch the exe the pupil just downloaded, but only once it has fully
        # landed and carries a valid HPE signature. Launching it here (from the
        # already-elevated job) also means no second UAC prompt.
        if (-not $startedDl) {
            $dl = Find-NewQuickConnectExe -After $watchAt
            if ($dl -and (Test-IsArubaQuickConnect $dl)) {
                try {
                    Start-Process -FilePath $dl | Out-Null
                    $startedDl = $true
                    if (-not $clickedQc) { $clickedQc = $true; $clickAt = Get-Date }
                    Note 'Starter QuickConnect...'
                } catch { }
            }
        }

        # If the download is stuck behind a prompt we cannot reach while Edge is
        # hidden, show it again so the pupil can finish it. The helper keeps
        # watching and takes over as soon as QuickConnect appears.
        if ($clickedQc -and -not $startedDl -and $hidEdge -and ((Get-Date) - $clickAt).TotalSeconds -ge 45) {
            Set-ZorkHelperEdgeVisible $true
            $hidEdge = $false
            Note 'Fullfor nedlastingen i nettleseren (klikk Behold hvis du blir spurt). Zork Helper fortsetter automatisk.'
        }
    }
    return $null
}

function Install-FridNett {
    $probe = Get-WifiSsid
    if ($probe.Connected -and (Get-SsidMatch $probe.Ssid 'FRID')) {
        Note 'Done! Du er koblet til riktig nettverk (FRID).'
        $script:FridAlreadyOk = $true
        return
    }

    $onIot = $probe.Connected -and (Get-SsidMatch $probe.Ssid 'FRID-iot')

    Set-CaptiveOffline $true
    if ($onIot) {
        Note 'Kobler til FRID-iot pa nytt med riktig passord...'
    } else {
        Note 'Bytter wifi til FRID-iot...'
    }
    $join = Connect-FridIot
    if ($join -eq 'frid') {
        Set-CaptiveOffline $false
        $script:FridAlreadyOk = $true
        Note 'Done! Du er koblet til riktig nettverk (FRID).'
        return
    }

    $ignore = @()
    try { $ignore = @(Get-FridQuickConnectProcesses | ForEach-Object { $_.Id }) } catch { $ignore = @() }

    Note 'Pa FRID-iot. Aapner innlogging. Logg inn i vinduet med skolekontoen. Zork Helper tar over etterpaa.'
    Start-Sleep -Seconds 2
    Open-FridOnboardLogin

    # No bundled exe: the QuickConnect must be the one this pupil downloads after
    # their own login. If it never turns up, stop and tell them what to do.
    $p = Wait-FridQuickConnectProcess -IgnoreIds $ignore -Seconds 480
    if ($null -eq $p) {
        Set-ZorkHelperEdgeVisible $true
        throw 'Fant ikke QuickConnect. Logg inn i vinduet og klikk Start QuickConnect. Zork Helper fortsetter automatisk etterpaa.'
    }
    Note 'QuickConnect kjorer. Zork Helper klikker Next og Ja. Hvis Windows spor om posisjon, svar selv Ja eller Nei.'
    Start-FridWizardClicks -Process $p
    try {
        if (-not $p.HasExited) { $p.WaitForExit(120000) | Out-Null }
    } catch { }
    $code = 0
    try { $code = [int]$p.ExitCode } catch { $code = 0 }
    Stop-ZorkHelperEdge
    Connect-FridAndForgetIot
    $back = Get-WifiSsid
    if ($back.Connected -and (Get-SsidMatch $back.Ssid 'FRID')) {
        Set-CaptiveOffline $false
        $script:FridAlreadyOk = $true
        Note 'Done! Du er koblet til riktig nettverk (FRID).'
        return
    }
    if ($code -and $code -ne 0) {
        Note ('FRID-nett ble lukket (kode ' + $code + ').')
        return
    }
    Note 'FRID-nett er ferdig. Koble til FRID i wifi-listen hvis du ikke er der enna.'
}

function Test-MsiOk {
    param([int]$Code)
    return ($Code -eq 0 -or $Code -eq 1641 -or $Code -eq 3010)
}

function Stop-ZorkScanJobs {
    try {
        Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like '*zork-scan-*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Test-PrinterInstalled {
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    foreach ($row in @(Get-ItemProperty -Path $roots -ErrorAction SilentlyContinue)) {
        $prop = $row.PSObject.Properties['DisplayName']
        if ($null -eq $prop) { continue }
        $name = [string]$prop.Value
        foreach ($want in @('YSoft SafeQ', 'SafeQ FlexiSpooler', 'Viken Print', 'FlexiSpooler')) {
            if ($name -like ('*' + $want + '*')) { return $true }
        }
    }
    return (Test-Path -LiteralPath 'C:\SafeQ6')
}

function Install-Printer {
    Stop-ZorkScanJobs
    # Running the MSI over an existing install makes Windows repair it, and that
    # repair fails with 1603 — so a PC that already has it would report failure.
    if (Test-PrinterInstalled) {
        Note 'Printer er allerede installert pa denne PC-en.'
        $script:PrinterOk = $true
        return
    }
    Note 'Laster ned printer...'
    $msi = Join-Path $env:TEMP ('zork-printer-' + $SessionId + '.msi')
    $url = ($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/tool/printer')
    $req = [Net.HttpWebRequest]::Create($url)
    $req.Method = 'GET'
    $req.Timeout = 600000
    $req.ReadWriteTimeout = 600000
    $req.Headers.Add('X-Helper-Secret', $Secret)
    $resp = $null
    try {
        $resp = $req.GetResponse()
    } catch {
        throw 'Klarte ikke a hente printer. Last opp VikenPrint.msi under Apps.'
    }
    $fs = $null
    $src = $null
    try {
        $total = [int64]$resp.ContentLength
        $src = $resp.GetResponseStream()
        $fs = [IO.File]::Create($msi)
        $buf = New-Object byte[] 65536
        $got = [int64]0
        $lastNote = 0
        while (($n = $src.Read($buf, 0, $buf.Length)) -gt 0) {
            $fs.Write($buf, 0, $n)
            $got += $n
            if ($total -gt 0) {
                $pct = [int](($got * 100) / $total)
                if ($pct -gt 100) { $pct = 100 }
                if ($pct -ge 1 -and $lastNote -eq 0) {
                    Note 'Laster ned printer... 1%'
                    $lastNote = 1
                }
                $step = [int]([Math]::Floor($pct / 5.0) * 5)
                if ($step -ge 5 -and $step -gt $lastNote) {
                    $lastNote = $step
                    Note ('Laster ned printer... ' + $step + '%')
                }
            } else {
                $mb = [int]($got / 1048576)
                if ($mb -ge ($lastNote + 5)) {
                    $lastNote = $mb - ($mb % 5)
                    Note ('Laster ned printer... ' + $lastNote + ' MB')
                }
            }
        }
    } finally {
        try { if ($null -ne $fs) { $fs.Close() } } catch { }
        try { if ($null -ne $src) { $src.Close() } } catch { }
        try { if ($null -ne $resp) { $resp.Close() } } catch { }
    }
    if (-not (Test-Path -LiteralPath $msi) -or ((Get-Item -LiteralPath $msi).Length -lt 1000)) {
        throw 'Printer-filen mangler pa serveren.'
    }
    Note 'Laster ned printer... 100%'
    try { Unblock-File -LiteralPath $msi -ErrorAction SilentlyContinue } catch { }
    Note 'Installerer printer...'
    # Silent: this process is already elevated, so the MSI needs no prompt.
    $p = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', $msi, '/qn', '/norestart') -Wait -PassThru
    if ($null -eq $p) { throw 'Klarte ikke a starte printer-installasjonen.' }
    $code = 0
    try { $code = [int]$p.ExitCode } catch { $code = 0 }
    if (-not (Test-MsiOk $code)) {
        Note ('Printer-installasjonen ble lukket (kode ' + $code + ').')
        return
    }
    Note 'Printer er ferdig.'
    $script:PrinterOk = $true
}

function Start-Skoleprogrammer {
    $zip = Join-Path $env:TEMP ('zork-sp-' + $SessionId + '.bin')
    $dest = Join-Path $env:TEMP ('zork-sp-' + $SessionId)
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add('X-Helper-Secret', $Secret)
    try {
        $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/tool/skoleprogrammer'), $zip)
    } catch {
        Note 'Fant ikke Skoleprogrammer pa hylla. Last opp zip-filen under Apps.'
        return $false
    }
    if (-not (Test-Path -LiteralPath $zip) -or ((Get-Item -LiteralPath $zip).Length -lt 1000)) {
        Note 'Fant ikke Skoleprogrammer pa hylla. Last opp zip-filen under Apps.'
        return $false
    }
    Note 'Starter Skoleprogrammer...'
    if (Test-Path -LiteralPath $dest) {
        Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $bytes = Get-Content -LiteralPath $zip -Encoding Byte -TotalCount 4
    $isZip = ($bytes.Length -ge 2 -and $bytes[0] -eq 80 -and $bytes[1] -eq 75)
    if ($isZip) {
        Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
    } else {
        Copy-Item -LiteralPath $zip -Destination (Join-Path $dest 'Skoleprogrammer.ps1')
    }
    $bat = Get-ChildItem -LiteralPath $dest -Recurse -Filter 'windows_run.bat' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bat) {
        Start-Process -FilePath $bat.FullName -WorkingDirectory $bat.DirectoryName -Wait
        return $true
    }
    $ps1 = Get-ChildItem -LiteralPath $dest -Recurse -Filter 'Skoleprogrammer.ps1' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ps1) {
        Start-Process -FilePath powershell.exe -Wait -ArgumentList @(
            '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', $ps1.FullName
        )
        return $true
    }
    return $false
}

try {
Stop-ZorkScanJobs
$wanted = Get-Wanted
$wifiNow = Get-WifiSsid
$onIot = $wifiNow.Connected -and (Get-SsidMatch $wifiNow.Ssid 'FRID-iot')
$fridOnly = (@($wanted).Count -eq 1 -and $wanted[0] -eq 'fridnett')
if ($onIot) {
    Set-CaptiveOffline $true
} else {
    Update-HelperTray
}
if ((@($wanted) -contains 'fridnett') -and (-not $onIot)) {
    Restart-HelperTray
}
$script:FridAlreadyOk = $false
$script:PrinterOk = $false
if ($wanted.Count -eq 0) {
    Note 'Ingenting a installere.' -Activity ''
    exit 0
}

if ((-not (Test-Admin)) -and (-not $fridOnly)) {
    Note 'Trenger administrator for a installere. Godkjenn i Windows og prov igjen.' -Activity ''
    exit 1
}

$direct = @($wanted | Where-Object { $_ -eq 'teams' -or $_ -eq 'winupdate' -or $_ -eq 'fridnett' -or $_ -eq 'printer' })
foreach ($id in $direct) {
    try {
        if ($id -eq 'teams') { Install-TeamsWork }
        elseif ($id -eq 'winupdate') { Install-WindowsUpdates }
        elseif ($id -eq 'fridnett') { Install-FridNett }
        elseif ($id -eq 'printer') { Install-Printer }
    } catch {
        Note ("$id : " + [string]$_.Exception.Message)
    }
}

$needTool = @($wanted | Where-Object { $_ -ne 'teams' -and $_ -ne 'winupdate' -and $_ -ne 'fridnett' -and $_ -ne 'printer' })
if ($needTool.Count -gt 0) {
    try {
        [void](Start-Skoleprogrammer)
    } catch {
        Note ('Skoleprogrammer: ' + [string]$_.Exception.Message)
    }
}

if ($script:FridAlreadyOk) {
    Note 'Done! Du er koblet til riktig nettverk (FRID).' -Activity ''
} elseif ($script:PrinterOk) {
    Note 'Done! Printer er installert.' -Activity ''
} elseif (@($wanted) -contains 'printer') {
    Note 'Klarte ikke a installere printer.' -Activity ''
} else {
    Note 'Installasjon ferdig.' -Activity ''
}
exit 0
} finally {
    Set-InstallBusyFlag $false
    Write-InstallDone
}
