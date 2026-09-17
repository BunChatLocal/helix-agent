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

function Update-HelperTray {
    $homeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
    $dest = Join-Path $homeDir 'zork-helper-tray.ps1'
    if (-not (Test-Path -LiteralPath $homeDir)) { return }
    $tmp = Join-Path $env:TEMP ('zork-tray-' + $SessionId + '.ps1')
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Encoding = [Text.Encoding]::UTF8
        $wc.Headers.Add('X-Helper-Secret', $Secret)
        $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/tray'), $tmp)
        if ((Test-Path -LiteralPath $tmp) -and ((Get-Item -LiteralPath $tmp).Length -gt 2000)) {
            Copy-Item -LiteralPath $tmp -Destination $dest -Force
        }
    } catch { }
}

function Send-Helper {
    param([string]$Path, [hashtable]$Body)
    $uri = ($BaseUrl.TrimEnd('/') + $Path)
    $json = $Body | ConvertTo-Json -Compress -Depth 6
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [Text.Encoding]::UTF8
    $wc.Headers.Add('Content-Type', 'application/json; charset=utf-8')
    $wc.Headers.Add('X-Helper-Secret', $Secret)
    [void]$wc.UploadString($uri, 'POST', $json)
}

function Note {
    param([string]$Text, [string]$Activity = $null)
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

function Get-WifiSsid {
    $ssid = ''
    $state = ''
    try {
        $raw = & netsh wlan show interfaces 2>$null
        foreach ($line in @($raw)) {
            if ($line -match '^\s*SSID\s*:\s*(.+)\s*$' -and $line -notmatch 'BSSID') {
                $ssid = $Matches[1].Trim()
            }
            if ($line -match '^\s*(State|Tilstand)\s*:\s*(.+)\s*$') {
                $state = $Matches[2].Trim().ToLower()
            }
        }
    } catch { }
    $up = ($state -eq 'connected' -or $state -eq 'tilkoblet')
    return @{ Ssid = $ssid; Connected = $up }
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
    while ($try -lt 2) {
        $try++
        try {
            $raw = & netsh wlan show networks 2>$null
            foreach ($line in @($raw)) {
                if ($line -match '^\s*SSID\s+\d+\s*:\s*(.+)\s*$') {
                    if (Get-SsidMatch $Matches[1] $Name) { return $true }
                }
            }
        } catch { }
        if ($try -lt 2) { Start-Sleep -Seconds 2 }
    }
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
      <name>$iot</name>
    </SSID>
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
</WLANProfile>
"@
    [System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::ASCII)
    try {
        $added = & netsh wlan add profile filename="$xmlPath" user=all
        if ("$added" -match '(?i)error|failed|feil') {
            $added = & netsh wlan add profile filename="$xmlPath" user=current
            if ("$added" -match '(?i)error|failed|feil') {
                throw 'netsh avviste wifi-profilen'
            }
        }
    } finally {
        Remove-Item -LiteralPath $xmlPath -Force -ErrorAction SilentlyContinue
    }
}

function Connect-FridIot {
    $iot = 'FRID-iot'
    Note 'Sjekker wifi...'
    if (Test-ConnectedSsid 'FRID') {
        Note 'Done! Du er koblet til riktig nettverk (FRID).'
        return 'frid'
    }

    if (Test-ConnectedSsid $iot) {
        Note 'Kobler fra FRID-iot og glemmer nettverket...'
        try { [void](& netsh wlan disconnect) } catch { }
        Start-Sleep -Seconds 2
        try { [void](& netsh wlan delete profile name="$iot") } catch { }
        Add-FridIotProfile
        Note 'Kobler til FRID-iot...'
        [void](& netsh wlan connect name="$iot" ssid="$iot")
        if (Wait-ConnectedSsid $iot 40) { return 'iot' }
        throw 'Klarte ikke a koble til FRID-iot. Prov igjen pa skolen, med wifi pa.'
    }

    Note 'Ser etter FRID-iot...'
    if (-not (Test-SsidInRange $iot)) {
        throw 'Fant ikke FRID-iot. Du ma vaere pa skolen, med wifi pa.'
    }
    try { [void](& netsh wlan delete profile name="$iot") } catch { }
    Add-FridIotProfile
    Note 'Kobler til FRID-iot...'
    [void](& netsh wlan connect name="$iot" ssid="$iot")
    if (Wait-ConnectedSsid $iot 40) { return 'iot' }
    throw 'Klarte ikke a koble til FRID-iot. Prov igjen pa skolen, med wifi pa.'
}

function Connect-FridAndForgetIot {
    $iot = 'FRID-iot'
    $name = 'FRID'
    try {
        foreach ($line in @(& netsh wlan show profiles 2>$null)) {
            if ($line -notmatch ':\s*(FRID\S*)\s*$') { continue }
            $cand = $Matches[1].Trim()
            if ($cand -and $cand -ne $iot) { $name = $cand; break }
        }
    } catch { }
    try { [void](& netsh wlan connect name="$name" ssid="FRID") } catch { }
    Start-Sleep -Seconds 4
    $wifi = Get-WifiSsid
    if ($wifi.Connected -and (Get-SsidMatch $wifi.Ssid 'FRID')) {
        try { [void](& netsh wlan delete profile name="$iot") } catch { }
        Note 'Koblet til FRID. FRID-iot er fjernet.'
        return
    }
    Note 'Sertifikatet er installert. Koble til FRID i wifi-listen hvis du ikke er der ennå. Fjern FRID-iot etterpå.'
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
    static bool TitleLooksLikeOnboard(string title) {
        if (string.IsNullOrEmpty(title)) return false;
        string t = title.ToLowerInvariant();
        if (t.Contains("user account control") || t.Contains("brukerkontokontroll")) return false;
        if (t.Contains("microsoft edge") || t.Contains("google chrome") || t.Contains("firefox")) return false;
        if (t.Contains("sign in") || t.Contains("logg p") || t.Contains("microsoftonline")) return false;
        return t.Contains("quickconnect") || t.Contains("aruba") || t.Contains("clearpass")
            || t.Contains("security warning") || t.Contains("sikkerhetsadvarsel")
            || t.Contains("root ca");
    }
    public static int ClickButtons(int processId, bool allowClose) {
        HashSet<string> go = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        string[] names = { "Next", "Neste", "Ja", "Yes", "Finish", "Connect" };
        foreach (string n in names) go.Add(n);
        if (allowClose) { go.Add("Close"); go.Add("Lukk"); }
        int flags = 0;
        List<IntPtr> roots = new List<IntPtr>();
        EnumWindows((h, l) => {
            if (IsWindowVisible(h)) roots.Add(h);
            return true;
        }, IntPtr.Zero);
        foreach (IntPtr root in roots) {
            uint pid;
            GetWindowThreadProcessId(root, out pid);
            if (pid != (uint)processId && !TitleLooksLikeOnboard(Title(root))) continue;
            List<IntPtr> nodes = new List<IntPtr>();
            nodes.Add(root);
            EnumChildWindows(root, (h, l) => { nodes.Add(h); return true; }, IntPtr.Zero);
            foreach (IntPtr h in nodes) {
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
    while (-not $Process.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        try { $Process.Refresh() } catch { break }
        try {
            $flags = [ZorkFridUi]::ClickButtons($qcPid, $allowClose)
            if (($flags -band 2) -ne 0) { $allowClose = $true }
        } catch { }
    }
}

function Get-FridQuickConnectProcesses {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.ProcessName -match '(?i)ArubaQuickConnect|QuickConnect' }
}

function Open-FridOnboardLogin {
    $url = 'https://gjesteportal.frid-iks.no/onboard/frid_provisioning.php'
    $edge = $null
    foreach ($c in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )) {
        if ($c -and (Test-Path -LiteralPath $c)) { $edge = $c; break }
    }
    if ($edge) {
        Start-Process -FilePath $edge -ArgumentList @(
            ('--app=' + $url),
            '--window-size=700,700',
            '--new-window'
        ) | Out-Null
        return
    }
    Start-Process $url | Out-Null
}

function Wait-FridQuickConnectProcess {
    param($IgnoreIds, [int]$Seconds = 480)
    $ignore = @()
    if ($null -ne $IgnoreIds) { $ignore = @($IgnoreIds) }
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $p = Get-FridQuickConnectProcesses |
            Where-Object { $ignore -notcontains $_.Id } |
            Select-Object -First 1
        if ($p) { return $p }
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

    Note 'Laster ned FRID-nett...'
    $exe = Join-Path $env:TEMP ('zork-fridnett-' + $SessionId + '.exe')
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add('X-Helper-Secret', $Secret)
    try {
        $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/tool/fridnett'), $exe)
    } catch {
        throw 'Klarte ikke a hente FRID-nett. Prov igjen pa skolen.'
    }
    if (-not (Test-Path -LiteralPath $exe) -or ((Get-Item -LiteralPath $exe).Length -lt 1000)) {
        throw 'FRID-nett-filen mangler pa serveren.'
    }
    $bytes = Get-Content -LiteralPath $exe -Encoding Byte -TotalCount 2
    if ($bytes.Length -lt 2 -or $bytes[0] -ne 77 -or $bytes[1] -ne 90) {
        throw 'FRID-nett-filen ser ikke ut som et Windows-program.'
    }
    try { Unblock-File -LiteralPath $exe -ErrorAction SilentlyContinue } catch { }

    $join = Connect-FridIot
    if ($join -eq 'frid') {
        $script:FridAlreadyOk = $true
        return
    }
    Start-Sleep -Seconds 2

    $ignore = @()
    try { $ignore = @(Get-FridQuickConnectProcesses | ForEach-Object { $_.Id }) } catch { $ignore = @() }

    Note 'Pa FRID-iot. Aapner innlogging. Logg inn i vinduet. Klikk Start QuickConnect.'
    Open-FridOnboardLogin

    $p = Wait-FridQuickConnectProcess -IgnoreIds $ignore -Seconds 480
    if ($null -eq $p) {
        Note 'Fant ikke QuickConnect. Starter programmet selv. Godkjenn i Windows hvis du blir spurt.'
        $p = Start-Process -FilePath $exe -PassThru
        if ($null -eq $p) { throw 'Klarte ikke a starte FRID-nett.' }
    } else {
        Note 'Fant QuickConnect. Fortsetter...'
    }
    Start-FridWizardClicks -Process $p
    try {
        if (-not $p.HasExited) { $p.WaitForExit(120000) | Out-Null }
    } catch { }
    $code = 0
    try { $code = [int]$p.ExitCode } catch { $code = 0 }
    Connect-FridAndForgetIot
    if ($code -and $code -ne 0) {
        Note ('FRID-nett ble lukket (kode ' + $code + ').')
        return
    }
    Note 'FRID-nett er ferdig.'
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

function Show-PrinterWindow {
    try { $Host.UI.RawUI.WindowTitle = 'Zork Helper - Skrivernedlasting' } catch { }
    try {
        if (-not ('ZorkPrinterWin' -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class ZorkPrinterWin {
    [DllImport("kernel32.dll")] public static extern bool AllocConsole();
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
        }
        $hwnd = [ZorkPrinterWin]::GetConsoleWindow()
        if ($hwnd -eq [IntPtr]::Zero) {
            [void][ZorkPrinterWin]::AllocConsole()
            $hwnd = [ZorkPrinterWin]::GetConsoleWindow()
        }
        if ($hwnd -ne [IntPtr]::Zero) {
            [void][ZorkPrinterWin]::ShowWindow($hwnd, 9)
            [void][ZorkPrinterWin]::ShowWindow($hwnd, 5)
            [void][ZorkPrinterWin]::SetForegroundWindow($hwnd)
        }
    } catch { }
    Write-Host ''
    Write-Host '  Zork Helper'
    Write-Host '  Laster ned skoleskriveren. Bli i dette vinduet til det er ferdig.'
    Write-Host ''
}

function Show-PrinterBar {
    param([int]$Percent, [string]$Text)
    $pct = [Math]::Max(0, [Math]::Min(100, $Percent))
    $fill = [int][Math]::Floor($pct / 5)
    if ($fill -gt 20) { $fill = 20 }
    $bar = ('#' * $fill) + ('-' * (20 - $fill))
    $line = '  [{0}] {1,3}%  {2}' -f $bar, $pct, $Text
    if ($line.Length -gt 79) { $line = $line.Substring(0, 79) }
    try { $Host.UI.RawUI.WindowTitle = ('Zork Helper - Skriver {0}%' -f $pct) } catch { }
    try { Write-Progress -Activity 'Zork Helper' -Status $Text -PercentComplete $pct } catch { }
    Write-Host ("`r" + $line.PadRight(79)) -NoNewline
}

function Install-Printer {
    Stop-ZorkScanJobs
    Show-PrinterWindow
    Note 'Laster ned printer...'
    Show-PrinterBar 0 'Laster ned printer...'
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
        $lastUi = -1
        while (($n = $src.Read($buf, 0, $buf.Length)) -gt 0) {
            $fs.Write($buf, 0, $n)
            $got += $n
            if ($total -gt 0) {
                $pct = [int](($got * 100) / $total)
                if ($pct -gt 100) { $pct = 100 }
                if ($pct -ne $lastUi) {
                    Show-PrinterBar $pct 'Laster ned printer...'
                    $lastUi = $pct
                }
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
                    Show-PrinterBar 0 ('Laster ned printer... ' + $lastNote + ' MB')
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
    Show-PrinterBar 100 'Laster ferdig.'
    try { Write-Progress -Activity 'Zork Helper' -Completed } catch { }
    Write-Host ''
    Write-Host '  Laster ferdig. Installerer...'
    try { Unblock-File -LiteralPath $msi -ErrorAction SilentlyContinue } catch { }
    Note 'Installerer printer. Godkjenn i Windows hvis du blir spurt.'
    try { $Host.UI.RawUI.WindowTitle = 'Zork Helper - Installerer skriver' } catch { }
    $p = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', $msi, '/qb', '/norestart') -Wait -PassThru
    if ($null -eq $p) { throw 'Klarte ikke a starte printer-installasjonen.' }
    $code = 0
    try { $code = [int]$p.ExitCode } catch { $code = 0 }
    if (-not (Test-MsiOk $code)) {
        Note ('Printer-installasjonen ble lukket (kode ' + $code + ').')
        return
    }
    Note 'Printer er ferdig.'
    try { $Host.UI.RawUI.WindowTitle = 'Zork Helper - Skriver ferdig' } catch { }
    Write-Host '  Ferdig.'
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
Update-HelperTray
$script:FridAlreadyOk = $false
$script:PrinterOk = $false
$wanted = Get-Wanted
if ($wanted.Count -eq 0) {
    Note 'Ingenting a installere.' -Activity ''
    exit 0
}

if (-not (Test-Admin)) {
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
    Write-InstallDone
}
