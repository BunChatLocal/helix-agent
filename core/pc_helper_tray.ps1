#Requires -Version 5.1
# Resident Zork Helper. Lives in the notification area. Polls for scan/install
# jobs. Does not run caller-supplied commands.

    if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne 'STA') {
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Start-Process -FilePath $ps -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-STA', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath
    )
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class ZorkNative {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
} catch { }

$mutex = New-Object System.Threading.Mutex($false, 'Local\ZorkHelperTray')
$taken = $false
try {
    $taken = $mutex.WaitOne(0, $false)
} catch [Threading.AbandonedMutexException] {
    $taken = $true
}
if (-not $taken) {
    exit 0
}

$script:HomeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
$script:CfgPath = Join-Path $script:HomeDir 'config.json'
$script:SkipScan = $false
$script:DeviceId = ''
$script:DeviceSecret = ''
$script:BaseUrl = ''
$script:SessionId = ''
$script:SessionSecret = ''
$script:BusyProc = $null
$script:BusyKind = ''
$script:BusySid = ''
$script:BusySecret = ''
$script:BusyTmp = ''
$script:BusyDone = ''
$script:BusyStartedTick = 0
$script:Tick = 0
$script:AfterInstallScan = $false
$script:WifiSsid = ''
$script:WifiConnected = $false
$script:WifiCheckedAt = 0

function Get-Cfg {
    if (-not (Test-Path -LiteralPath $script:CfgPath)) { return $null }
    try {
        return (Get-Content -LiteralPath $script:CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Save-Cfg {
    param($Cfg)
    New-Item -ItemType Directory -Force -Path $script:HomeDir | Out-Null
    ($Cfg | ConvertTo-Json -Compress) | Set-Content -LiteralPath $script:CfgPath -Encoding UTF8
}

function Read-State {
    $cfg = Get-Cfg
    if ($null -eq $cfg) { return $false }
    $script:BaseUrl = [string]$cfg.base_url
    $script:SessionId = [string]$cfg.session_id
    $script:SessionSecret = [string]$cfg.session_secret
    if ($cfg.PSObject.Properties['device_id']) { $script:DeviceId = [string]$cfg.device_id }
    if ($cfg.PSObject.Properties['device_secret']) { $script:DeviceSecret = [string]$cfg.device_secret }
    $script:SkipScan = $false
    if ($cfg.PSObject.Properties['skip_scan']) {
        try { $script:SkipScan = [bool]$cfg.skip_scan } catch { $script:SkipScan = $false }
    }
    if ([string]::IsNullOrWhiteSpace($script:BaseUrl)) { return $false }
    return $true
}

function Persist-Device {
    $cfg = Get-Cfg
    if ($null -eq $cfg) {
        $cfg = [pscustomobject]@{
            base_url       = $script:BaseUrl
            session_id     = $script:SessionId
            session_secret = $script:SessionSecret
        }
    }
    $cfg | Add-Member -NotePropertyName device_id -NotePropertyValue $script:DeviceId -Force
    $cfg | Add-Member -NotePropertyName device_secret -NotePropertyValue $script:DeviceSecret -Force
    $cfg | Add-Member -NotePropertyName session_id -NotePropertyValue $script:SessionId -Force
    $cfg | Add-Member -NotePropertyName session_secret -NotePropertyValue $script:SessionSecret -Force
    $cfg | Add-Member -NotePropertyName base_url -NotePropertyValue $script:BaseUrl -Force
    $cfg | Add-Member -NotePropertyName skip_scan -NotePropertyValue $script:SkipScan -Force
    Save-Cfg $cfg
}

function Register-ZorkProtocol {
    $open = Join-Path $script:HomeDir 'zork-helper-open.ps1'
    if (-not (Test-Path -LiteralPath $open)) { return }
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $openCmd = '"' + $ps + '" -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $open + '" "%1"'
    $rk = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\zorkhelper')
    $rk.SetValue('', 'URL:Zork Helper')
    $rk.SetValue('URL Protocol', '')
    $rk.CreateSubKey('shell\open\command').SetValue('', $openCmd)
    $rk.Close()
    $run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (-not (Test-Path -LiteralPath $run)) { New-Item -Path $run -Force | Out-Null }
    $tray = Join-Path $script:HomeDir 'zork-helper-tray.ps1'
    $startCmd = '"' + $ps + '" -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '"'
    Set-ItemProperty -LiteralPath $run -Name 'ZorkHelper' -Value $startCmd
}

function Clear-SessionFromCfg {
    $script:SessionId = ''
    $script:SessionSecret = ''
    Persist-Device
}

function Send-Json {
    param([string]$Url, [string]$Secret, [string]$Body = '{}', [string]$Method = 'POST', [int]$TimeoutMs = 2500)
    $req = [Net.HttpWebRequest]::Create($Url)
    $req.Method = $Method
    $req.Timeout = $TimeoutMs
    $req.ReadWriteTimeout = $TimeoutMs
    $req.KeepAlive = $false
    $req.ContentType = 'application/json; charset=utf-8'
    if ($Secret) { $req.Headers.Add('X-Helper-Secret', $Secret) }
    if ($Method -ne 'GET') {
        $payload = [Text.Encoding]::UTF8.GetBytes($(if ($null -eq $Body) { '{}' } else { $Body }))
        $req.ContentLength = $payload.Length
        $stream = $req.GetRequestStream()
        try { $stream.Write($payload, 0, $payload.Length) } finally { $stream.Close() }
    }
    $resp = $req.GetResponse()
    try {
        $reader = New-Object IO.StreamReader($resp.GetResponseStream())
        try { return $reader.ReadToEnd() } finally { $reader.Close() }
    } finally {
        $resp.Close()
    }
}

function Hide-ZorkConsole {
    try {
        $hwnd = [ZorkNative]::GetConsoleWindow()
        if ($hwnd -ne [IntPtr]::Zero) {
            [void][ZorkNative]::ShowWindow($hwnd, 0)
        }
    } catch { }
}

function New-ZorkIcon {
    try {
        $size = 32
        $fmt = [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        $bmp = New-Object System.Drawing.Bitmap $size, $size, $fmt
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.Clear([System.Drawing.Color]::Transparent)

        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $pts = [System.Drawing.PointF[]]@(
            [System.Drawing.PointF]::new(16.0, 2.0),
            [System.Drawing.PointF]::new(27.0, 6.5),
            [System.Drawing.PointF]::new(26.5, 16.0),
            [System.Drawing.PointF]::new(16.0, 30.0),
            [System.Drawing.PointF]::new(5.5, 16.0),
            [System.Drawing.PointF]::new(5.0, 6.5)
        )
        $path.AddClosedCurve($pts, 0.12)

        $fill = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 37, 99, 235))
        $edge = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(255, 29, 78, 216), 1.4)
        $g.FillPath($fill, $path)
        $g.DrawPath($edge, $path)

        $fill.Dispose()
        $edge.Dispose()
        $path.Dispose()
        $g.Dispose()
        $script:NotifyBitmap = $bmp
        return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
    } catch {
        return [System.Drawing.SystemIcons]::Application
    }
}

function Show-Tip {
    param([string]$Text)
    try {
        $script:Notify.BalloonTipTitle = 'Zork Helper'
        $script:Notify.BalloonTipText = $Text
        $script:Notify.ShowBalloonTip(4000)
    } catch { }
}

function Start-HiddenPs {
    param([string]$File, [Alias('Args')][string[]]$Extra)
    $list = New-Object System.Collections.ArrayList
    [void]$list.Add('-NoProfile')
    [void]$list.Add('-ExecutionPolicy')
    [void]$list.Add('Bypass')
    [void]$list.Add('-File')
    [void]$list.Add($File)
    foreach ($a in @($Extra)) { [void]$list.Add($a) }
    return Start-Process -FilePath powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @($list.ToArray())
}

function Start-ScanJob {
    param([string]$Sid, [string]$Secret)
    if ([string]::IsNullOrWhiteSpace($Sid) -or [string]::IsNullOrWhiteSpace($Secret)) { return }
    $tmp = Join-Path $env:TEMP ('zork-scan-' + $Sid + '.ps1')
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [Text.Encoding]::UTF8
    $wc.Headers.Add('X-Helper-Secret', $Secret)
    $wc.DownloadFile(($script:BaseUrl.TrimEnd('/') + '/api/helper/' + $Sid + '/script'), $tmp)
    try {
        [void]$wc.UploadString(
            ($script:BaseUrl.TrimEnd('/') + '/api/helper/' + $Sid + '/connect'),
            'POST',
            '{}'
        )
    } catch { }
    $script:BusyProc = Start-HiddenPs -File $tmp -Args @('-BaseUrl', $script:BaseUrl, '-SessionId', $Sid, '-Secret', $Secret)
    $script:BusyKind = 'scan'
    $script:BusySid = $Sid
    $script:BusySecret = $Secret
    $script:BusyTmp = $tmp
    $script:BusyDone = ''
    Tell-Session -Sid $Sid -Secret $Secret -Text 'Sjekker denne PC-en...' -Activity 'scanning'
    Show-Tip 'Sjekker denne PC-en...'
}

function Tell-Session {
    param([string]$Sid, [string]$Secret, [string]$Text, [string]$Activity = $null)
    if ([string]::IsNullOrWhiteSpace($Sid) -or [string]::IsNullOrWhiteSpace($Secret)) { return }
    $payload = @{ text = $Text }
    if ($null -ne $Activity) { $payload.activity = $Activity }
    try {
        [void](Send-Json -Url ($script:BaseUrl.TrimEnd('/') + '/api/helper/' + $Sid + '/progress') -Secret $Secret -Body ($payload | ConvertTo-Json -Compress))
    } catch { }
}

function Get-InstallIds {
    param($Items)
    $ids = New-Object System.Collections.Generic.List[string]
    foreach ($raw in @($Items)) {
        foreach ($part in ([string]$raw).ToLower() -split '[,;]+') {
            $id = $part.Trim()
            if ($id -and -not $ids.Contains($id)) { $ids.Add($id) }
        }
    }
    return @($ids)
}

function Test-FridNettOnly {
    param($Items)
    $ids = @(Get-InstallIds $Items)
    if ($ids.Count -eq 0) { return $false }
    foreach ($id in $ids) {
        if ($id -ne 'fridnett') { return $false }
    }
    return $true
}

function Test-PrinterOnly {
    param($Items)
    $ids = @(Get-InstallIds $Items)
    if ($ids.Count -eq 0) { return $false }
    foreach ($id in $ids) {
        if ($id -ne 'printer') { return $false }
    }
    return $true
}

function Save-HttpToFile {
    param([string]$Url, [string]$Secret, [string]$Dest, [int]$TimeoutMs = 8000)
    $req = [Net.HttpWebRequest]::Create($Url)
    $req.Method = 'GET'
    $req.Timeout = $TimeoutMs
    $req.ReadWriteTimeout = $TimeoutMs
    $req.KeepAlive = $false
    if ($Secret) { $req.Headers.Add('X-Helper-Secret', $Secret) }
    $resp = $req.GetResponse()
    try {
        $src = $resp.GetResponseStream()
        $fs = [IO.File]::Create($Dest)
        try { $src.CopyTo($fs) } finally { $fs.Close() }
    } finally {
        $resp.Close()
    }
}

function Test-SessionTokenUsable {
    param([string]$Sid, [string]$Secret)
    if ([string]::IsNullOrWhiteSpace($Sid) -or $Sid -notmatch '^[a-f0-9]{32}$') { return $false }
    if ([string]::IsNullOrWhiteSpace($Secret) -or $Secret.Length -lt 16) { return $false }
    if ($Sid -eq '00000000000000000000000000000000') { return $false }
    if ($Secret -eq 'offline-local-session-token') { return $false }
    return $true
}

function Start-InstallJob {
    param([string]$Sid, [string]$Secret, [string[]]$Items)
    if (Test-FridNettOnly $Items) {
        try { Refresh-WifiStatus } catch { }
        if (-not (Test-CaptiveOffline)) {
            Tell-Session -Sid $Sid -Secret $Secret -Text 'Sjekker wifi...' -Activity 'installing'
        }
        if (Test-SchoolFridSsid $script:WifiSsid) {
            Tell-Session -Sid $Sid -Secret $Secret -Text 'Done! Du er koblet til riktig nettverk (FRID).' -Activity ''
            Show-Tip 'Done! Du er pa riktig nettverk.'
            return
        }
    }
    $tmp = Join-Path $env:TEMP ('zork-install-' + $Sid + '.ps1')
    $jobFile = Join-Path $env:TEMP ('zork-install-items-' + $Sid + '.txt')
    $cache = Join-Path $script:HomeDir 'install-script.ps1'
    $seed = Join-Path $script:HomeDir 'zork-helper-install.ps1'
    $got = $false
    $canFetch = (Test-SessionTokenUsable -Sid $Sid -Secret $Secret) -and (-not (Test-CaptiveOffline))
    if ($canFetch) {
        try {
            Save-HttpToFile -Url ($script:BaseUrl.TrimEnd('/') + '/api/helper/' + $Sid + '/install-script') -Secret $Secret -Dest $tmp -TimeoutMs 8000
            if ((Test-Path -LiteralPath $tmp) -and ((Get-Item -LiteralPath $tmp).Length -gt 2000)) {
                Copy-Item -LiteralPath $tmp -Destination $cache -Force
                Copy-Item -LiteralPath $tmp -Destination $seed -Force
                $got = $true
            }
        } catch { }
    }
    if (-not $got) {
        if (Test-Path -LiteralPath $cache) {
            Copy-Item -LiteralPath $cache -Destination $tmp -Force
            $got = $true
        } elseif (Test-Path -LiteralPath $seed) {
            Copy-Item -LiteralPath $seed -Destination $tmp -Force
            try { Copy-Item -LiteralPath $seed -Destination $cache -Force } catch { }
            $got = $true
        } else {
            $old = Get-ChildItem -LiteralPath $env:TEMP -Filter 'zork-install-*.ps1' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First 1
            if ($old) {
                Copy-Item -LiteralPath $old.FullName -Destination $tmp -Force
                $got = $true
            }
        }
    }
    if (-not $got) {
        Tell-Session -Sid $Sid -Secret $Secret -Text 'Klarte ikke a hente installasjonsfilen.' -Activity ''
        Show-Tip 'Klarte ikke a starte installasjon.'
        return
    }
    $joined = [string]::Join(',', @($Items))
    [IO.File]::WriteAllText($jobFile, $joined)
    $done = Join-Path $script:HomeDir ('install-' + $Sid + '.done')
    try { Remove-Item -LiteralPath $done -Force -ErrorAction SilentlyContinue } catch { }
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arg = '-NoProfile -ExecutionPolicy Bypass -File "' + $tmp + '" -BaseUrl "' + $script:BaseUrl + '" -SessionId "' + $Sid + '" -Secret "' + $Secret + '" -JobFile "' + $jobFile + '" -DoneFile "' + $done + '"'
    if (Test-FridNettOnly $Items) {
        Show-Tip 'Kobler til FRID-iot...'
        try {
            $script:BusyProc = Start-Process -FilePath $ps -PassThru -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $tmp,
                '-BaseUrl', $script:BaseUrl, '-SessionId', $Sid, '-Secret', $Secret,
                '-JobFile', $jobFile, '-DoneFile', $done
            )
        } catch {
            $script:BusyProc = $null
            Tell-Session -Sid $Sid -Secret $Secret -Text 'Klarte ikke a starte FRID-nett.' -Activity ''
            Show-Tip 'Klarte ikke a starte FRID-nett.'
            return
        }
        if ($null -eq $script:BusyProc) {
            Tell-Session -Sid $Sid -Secret $Secret -Text 'Klarte ikke a starte FRID-nett.' -Activity ''
            return
        }
        $script:BusyKind = 'install'
        $script:BusySid = $Sid
        $script:BusySecret = $Secret
        $script:BusyTmp = $tmp
        $script:BusyDone = $done
        $script:BusyStartedTick = $script:Tick
        $script:AfterInstallScan = $false
        return
    }
    if (-not (Test-CaptiveOffline)) {
        Tell-Session -Sid $Sid -Secret $Secret -Text 'Windows spor om tillatelse til a installere.' -Activity 'installing'
        Show-Tip 'Windows spor om a tillate Zork Helper...'
    }
    $wrap = Join-Path $env:TEMP ('zork-elevate-' + $Sid + '.ps1')
    $exeLit = $ps.Replace("'", "''")
    $argLit = $arg.Replace("'", "''")
    $wrapText = @(
        '$info = New-Object System.Diagnostics.ProcessStartInfo',
        ('$info.FileName = ''' + $exeLit + ''''),
        ('$info.Arguments = ''' + $argLit + ''''),
        '$info.UseShellExecute = $true',
        '$info.Verb = ''runas''',
        '$info.CreateNoWindow = $false',
        '$info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Normal',
        'try {',
        '    $p = [Diagnostics.Process]::Start($info)',
        '    if ($null -eq $p) { exit 2 }',
        '    $p.WaitForExit()',
        '    exit [int]$p.ExitCode',
        '} catch {',
        '    exit 2',
        '}'
    ) -join "`r`n"
    [IO.File]::WriteAllText($wrap, $wrapText)
    try {
        $script:BusyProc = Start-Process -FilePath $ps -PassThru -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $wrap
        )
    } catch {
        $script:BusyProc = $null
        Tell-Session -Sid $Sid -Secret $Secret -Text 'Installasjon avbrutt.' -Activity ''
        Show-Tip 'Installasjon avbrutt.'
        return
    }
    if ($null -eq $script:BusyProc) {
        Tell-Session -Sid $Sid -Secret $Secret -Text 'Installasjon avbrutt.' -Activity ''
        return
    }
    $script:BusyKind = 'install'
    $script:BusySid = $Sid
    $script:BusySecret = $Secret
    $script:BusyTmp = $tmp
    $script:BusyDone = $done
    $script:BusyStartedTick = $script:Tick
    $ids = @(Get-InstallIds $Items)
    $script:AfterInstallScan = ($ids.Count -gt 0) -and -not (Test-FridNettOnly $ids) -and -not (Test-PrinterOnly $ids)
    if (Test-FridNettOnly $ids) {
        Show-Tip 'Kobler til FRID-iot...'
    } else {
        Show-Tip 'Installerer det som mangler...'
    }
}

function Resolve-InboxSession {
    $sid = ([string]$script:SessionId).Trim().ToLowerInvariant()
    if ($sid -notmatch '^[a-f0-9]{32}$') {
        $sid = '00000000000000000000000000000000'
    }
    $secret = ([string]$script:SessionSecret).Trim()
    if ($secret.Length -lt 16) {
        $secret = 'offline-local-session-token'
    }
    return @{ sid = $sid; secret = $secret }
}

function Start-InboxFridNett {
    if ($script:BusyKind -eq 'install') { return }
    if ([string]::IsNullOrWhiteSpace($script:BaseUrl)) {
        $script:BaseUrl = 'https://zorken.chat'
    }
    $sess = Resolve-InboxSession
    Show-Tip 'Starter FRID-nett...'
    Start-InstallJob -Sid ([string]$sess.sid) -Secret ([string]$sess.secret) -Items @('fridnett')
}

function Register-Device {
    if ([string]::IsNullOrWhiteSpace($script:SessionId) -or [string]::IsNullOrWhiteSpace($script:SessionSecret)) {
        return $false
    }
    $hostName = [string]$env:COMPUTERNAME
    $body = (@{ hostname = $hostName } | ConvertTo-Json -Compress)
    try {
        $raw = Send-Json -Url ($script:BaseUrl.TrimEnd('/') + '/api/helper/' + $script:SessionId + '/device') -Secret $script:SessionSecret -Body $body
        $got = $raw | ConvertFrom-Json
        if ($got.id -and $got.secret) {
            $script:DeviceId = [string]$got.id
            $script:DeviceSecret = [string]$got.secret
            Persist-Device
            return $true
        }
    } catch { }
    return $false
}

function Invoke-Heartbeat {
    if ([string]::IsNullOrWhiteSpace($script:DeviceId) -or [string]::IsNullOrWhiteSpace($script:DeviceSecret)) {
        return
    }
    try {
        [void](Send-Json -Url ($script:BaseUrl.TrimEnd('/') + '/api/helper/device/' + $script:DeviceId + '/heartbeat') -Secret $script:DeviceSecret)
    } catch { }
}

function Get-ServerJob {
    if ([string]::IsNullOrWhiteSpace($script:DeviceId) -or [string]::IsNullOrWhiteSpace($script:DeviceSecret)) {
        return $null
    }
    try {
        $raw = Send-Json -Url ($script:BaseUrl.TrimEnd('/') + '/api/helper/device/' + $script:DeviceId + '/job') -Secret $script:DeviceSecret -Method 'GET' -Body ''
        $got = $raw | ConvertFrom-Json
        if ($null -eq $got.job) { return $null }
        return $got.job
    } catch {
        return $null
    }
}

function Start-JobFromServer {
    param($Job)
    if ($null -eq $Job) { return }
    if ($script:BusyKind -eq 'install') { return }
    $kind = [string]$Job.kind
    if ($script:BusyKind -and $kind -ne 'install') { return }
    $sid = [string]$Job.session_id
    $secret = [string]$Job.session_secret
    if ($kind -eq 'install') {
        $items = @()
        if ($Job.items) { $items = @($Job.items) }
        Start-InstallJob -Sid $sid -Secret $secret -Items $items
        return
    }
    if ($kind -eq 'scan') {
        Start-ScanJob -Sid $sid -Secret $secret
    }
}

function Test-InstallStillRunning {
    $tmp = [string]$script:BusyTmp
    try {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'msiexec.exe'" -ErrorAction Stop)
        foreach ($p in $procs) {
            $cmd = [string]$p.CommandLine
            if ([string]::IsNullOrWhiteSpace($cmd)) { continue }
            if ($tmp -and $cmd.IndexOf($tmp, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
            if ($cmd.IndexOf('zork-elevate-', [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
            if ($cmd.IndexOf('zork-install-', [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
            if ($cmd.IndexOf('zork-printer-', [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
        }
        return $false
    } catch {
        return $true
    }
}

function Finish-BusyIfNeeded {
    if ($script:BusyKind -eq 'install') {
        $marked = $false
        try { $marked = $script:BusyDone -and (Test-Path -LiteralPath $script:BusyDone) } catch { $marked = $false }
        if (-not $marked) {
            if (($script:Tick % 10) -ne 0) { return }
            if (Test-InstallStillRunning) { return }
            $alive = $false
            try {
                if ($script:BusyProc) {
                    $script:BusyProc.Refresh()
                    $alive = -not [bool]$script:BusyProc.HasExited
                }
            } catch { $alive = $false }
            if ($alive) { return }
            $age = $script:Tick - [int]$script:BusyStartedTick
            if ($age -lt 40) { return }
            try {
                Tell-Session -Sid $script:BusySid -Secret $script:BusySecret -Text 'Installasjon avbrutt.' -Activity ''
            } catch { }
            $script:AfterInstallScan = $false
        }
    } else {
        if ($null -eq $script:BusyProc) { return }
        if (-not $script:BusyProc.HasExited) { return }
    }
    $tmp = $script:BusyTmp
    if ($tmp -and (Test-Path -LiteralPath $tmp)) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    $done = $script:BusyDone
    if ($done -and (Test-Path -LiteralPath $done)) {
        Remove-Item -LiteralPath $done -Force -ErrorAction SilentlyContinue
    }
    $wasInstall = ($script:BusyKind -eq 'install')
    $wasScan = ($script:BusyKind -eq 'scan')
    $sid = $script:BusySid
    $secret = $script:BusySecret
    $script:BusyProc = $null
    $script:BusyKind = ''
    $script:BusyTmp = ''
    $script:BusyDone = ''
    $script:BusyStartedTick = 0
    if ($wasInstall -and $script:AfterInstallScan -and $sid -and $secret) {
        $script:AfterInstallScan = $false
        Start-ScanJob -Sid $sid -Secret $secret
        return
    }
    if ($wasScan) {
        Clear-SessionFromCfg
    }
}

$script:Incoming = New-Object System.Collections.Queue
$script:IncomingLock = New-Object object
$script:StopListen = $false
$script:LocalPort = 18792
$script:TcpListener = $null

function Get-OriginKey {
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return '' }
    try {
        $u = [Uri]$Url
        if ($u.Scheme -ne 'http' -and $u.Scheme -ne 'https') { return '' }
        $h = $u.Host
        if ($h -eq 'localhost' -or $h -eq '::1') { $h = '127.0.0.1' }
        return ($u.Scheme + '://' + $h + ':' + $u.Port)
    } catch {
        return ''
    }
}

function Test-HelixSession {
    param([string]$Base, [string]$Sid, [string]$Secret)
    try {
        $u = $Base.TrimEnd('/') + '/api/helper/' + $Sid + '/connect'
        $req = [Net.HttpWebRequest]::Create($u)
        $req.Method = 'POST'
        $req.ContentType = 'application/json; charset=utf-8'
        $req.Headers.Add('X-Helper-Secret', $Secret)
        $req.Timeout = 4000
        $bytes = [Text.Encoding]::UTF8.GetBytes('{}')
        $req.ContentLength = $bytes.Length
        $st = $req.GetRequestStream()
        $st.Write($bytes, 0, $bytes.Length)
        $st.Close()
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return ($code -ge 200 -and $code -lt 300)
    } catch {
        return $false
    }
}

function Test-JobAllowed {
    param([string]$JobUrl, [string]$HeaderOrigin, [string]$Sid, [string]$Secret)
    $jobKey = Get-OriginKey $JobUrl
    if (-not $jobKey) { return $false }
    $hdrKey = Get-OriginKey $HeaderOrigin
    if ($hdrKey -and $hdrKey -ne $jobKey) { return $false }
    if (Test-HelixSession -Base $JobUrl -Sid $Sid -Secret $Secret) { return $true }
    $cfgKey = Get-OriginKey $script:BaseUrl
    return ($jobKey -eq $cfgKey)
}

function Find-HeaderEnd {
    param([byte[]]$Bytes)
    for ($i = 0; $i -le ($Bytes.Length - 4); $i++) {
        if ($Bytes[$i] -eq 13 -and $Bytes[$i+1] -eq 10 -and $Bytes[$i+2] -eq 13 -and $Bytes[$i+3] -eq 10) {
            return $i
        }
    }
    return -1
}

function Refresh-WifiStatus {
    $now = [Environment]::TickCount
    if ($script:WifiCheckedAt) {
        $delta = $now - $script:WifiCheckedAt
        if ($delta -ge 0 -and $delta -lt 8000) { return }
    }
    $script:WifiCheckedAt = $now
    $ssid = ''
    try {
        $profiles = @(Get-NetConnectionProfile -ErrorAction Stop)
        foreach ($p in $profiles) {
            $n = [string]$p.Name
            if ([string]::IsNullOrWhiteSpace($n) -or $n -eq '*' ) { continue }
            $alias = ''
            try { $alias = [string]$p.InterfaceAlias } catch { $alias = '' }
            if ($alias -match 'Wi-?Fi|Wireless|WLAN') {
                $ssid = $n
                break
            }
            if ([string]::IsNullOrWhiteSpace($ssid)) { $ssid = $n }
        }
    } catch { }
    if (-not [string]::IsNullOrWhiteSpace($ssid)) {
        $script:WifiSsid = $ssid
        $script:WifiConnected = $true
    }
}

function Test-SchoolFridSsid {
    param([string]$Name)
    $s = ([string]$Name).Trim()
    if ([string]::IsNullOrWhiteSpace($s)) { return $false }
    if ($s -imatch '^FRID[-_ ]?iot$' -or $s -imatch '^FRID[-_ ]?gie?st$') { return $false }
    return ($s -imatch '^FRID(\b|[-_ ]|$)')
}

function Test-FridIotSsid {
    param([string]$Name)
    return ([string]$Name).Trim() -imatch '^FRID[-_ ]?iot$'
}

function Test-CaptiveOffline {
    if (Test-FridIotSsid $script:WifiSsid) { return $true }
    try {
        return (Test-Path -LiteralPath (Join-Path $script:HomeDir 'captive.flag'))
    } catch {
        return $false
    }
}

function Get-LocalProgress {
    $pf = Join-Path $script:HomeDir 'progress.txt'
    try {
        if (Test-Path -LiteralPath $pf) {
            return ([IO.File]::ReadAllText($pf)).Trim()
        }
    } catch { }
    return ''
}

function Open-UserUrl {
    param([string]$Url)
    $u = ([string]$Url).Trim()
    if ($u -notmatch '^https://(onboard|gjesteportal)\.frid-iks\.no(/|$)') { return }
    Show-Tip 'Aapner innlogging i Edge...'
    $edge = ''
    foreach ($c in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )) {
        if ($c -and (Test-Path -LiteralPath $c)) { $edge = [string]$c; break }
    }
    if (-not $edge) {
        try {
            $cmd = Get-Command msedge.exe -ErrorAction SilentlyContinue
            if ($cmd -and $cmd.Source) { $edge = [string]$cmd.Source }
        } catch { }
    }
    if (-not $edge) { return }
    $profile = Join-Path $env:TEMP 'ZorkHelperEdge'
    $argLine = '--user-data-dir="' + $profile + '" --remote-debugging-port=18793 --remote-allow-origins=* --no-first-run --no-default-browser-check --new-window ' + $u
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $edge
        $psi.Arguments = $argLine
        $psi.UseShellExecute = $false
        $psi.WorkingDirectory = (Split-Path -Parent $edge)
        [void][Diagnostics.Process]::Start($psi)
    } catch { }
}

function Drain-OpenUrl {
    $f = Join-Path $script:HomeDir 'open-url.txt'
    if (-not (Test-Path -LiteralPath $f)) { return }
    $url = ''
    try { $url = ([IO.File]::ReadAllText($f)).Trim() } catch { $url = '' }
    try { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue } catch { }
    Open-UserUrl $url
}

function Get-PingBody {
    $obj = [ordered]@{
        ok        = $true
        name      = 'Zork Helper'
        ssid      = [string]$script:WifiSsid
        connected = [bool]$script:WifiConnected
        progress  = Get-LocalProgress
    }
    return ($obj | ConvertTo-Json -Compress)
}

function Write-LocalHttp {
    param($Stream, [int]$Code, [string]$Body = '', [string]$AllowOrigin = '*')
    $status = 'OK'
    if ($Code -eq 204) { $status = 'No Content' }
    elseif ($Code -eq 400) { $status = 'Bad Request' }
    elseif ($Code -eq 403) { $status = 'Forbidden' }
    elseif ($Code -eq 404) { $status = 'Not Found' }
    if ([string]::IsNullOrWhiteSpace($AllowOrigin)) { $AllowOrigin = '*' }
    $payload = [Text.Encoding]::UTF8.GetBytes($Body)
    $head = "HTTP/1.1 $Code $status`r`nAccess-Control-Allow-Origin: $AllowOrigin`r`nAccess-Control-Allow-Methods: GET, POST, OPTIONS`r`nAccess-Control-Allow-Headers: Content-Type`r`nAccess-Control-Allow-Private-Network: true`r`nAccess-Control-Max-Age: 86400`r`nContent-Type: application/json; charset=utf-8`r`nContent-Length: $($payload.Length)`r`nConnection: close`r`n`r`n"
    $hb = [Text.Encoding]::ASCII.GetBytes($head)
    $Stream.Write($hb, 0, $hb.Length)
    if ($payload.Length -gt 0) { $Stream.Write($payload, 0, $payload.Length) }
}

function Enqueue-LocalJob {
    param($Job)
    [Threading.Monitor]::Enter($script:IncomingLock)
    try { $script:Incoming.Enqueue($Job) } finally { [Threading.Monitor]::Exit($script:IncomingLock) }
}

function Drain-Incoming {
    $jobs = New-Object System.Collections.ArrayList
    [Threading.Monitor]::Enter($script:IncomingLock)
    try {
        while ($script:Incoming.Count -gt 0) { [void]$jobs.Add($script:Incoming.Dequeue()) }
    } finally { [Threading.Monitor]::Exit($script:IncomingLock) }
    foreach ($j in $jobs) {
        $kind = [string]$j.kind
        $sid = [string]$j.session_id
        $secret = [string]$j.session_secret
        if ($script:BusyKind -eq 'install') { continue }
        if ($kind -eq 'install') {
            $items = @()
            if ($j.items) { $items = @($j.items) }
            Start-InstallJob -Sid $sid -Secret $secret -Items $items
        } elseif ([string]::IsNullOrWhiteSpace($script:BusyKind)) {
            Start-ScanJob -Sid $sid -Secret $secret
        }
    }
}

function Handle-LocalClient {
    param($Client)
    $stream = $null
    try {
        $Client.ReceiveTimeout = 800
        $stream = $Client.GetStream()
        $raw = New-Object IO.MemoryStream
        $buf = New-Object byte[] 2048
        $headerEnd = -1
        while ($raw.Length -lt 65536) {
            $n = $stream.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $raw.Write($buf, 0, $n)
            $headerEnd = Find-HeaderEnd -Bytes $raw.ToArray()
            if ($headerEnd -ge 0) { break }
        }
        $bytes = $raw.ToArray()
        if ($headerEnd -lt 0) { Write-LocalHttp $stream 400 '{"ok":false}'; return }
        $headerText = [Text.Encoding]::ASCII.GetString($bytes, 0, $headerEnd)
        $lines = $headerText -split "`r`n"
        $req = $lines[0]
        $contentLength = 0
        $allowOrigin = '*'
        $headerOrigin = ''
        foreach ($line in $lines) {
            if ($line -match '^Content-Length:\s*(\d+)') { $contentLength = [int]$Matches[1] }
            if ($line -match '^Origin:\s*(.+)$') {
                $headerOrigin = $Matches[1].Trim()
                $originKey = Get-OriginKey $headerOrigin
                if ($originKey) { $allowOrigin = $headerOrigin }
            }
        }
        $bodyStart = $headerEnd + 4
        $have = $bytes.Length - $bodyStart
        while ($have -lt $contentLength -and $raw.Length -lt 65536) {
            $n = $stream.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $raw.Write($buf, 0, $n)
            $bytes = $raw.ToArray()
            $have = $bytes.Length - $bodyStart
        }
        $body = ''
        if ($contentLength -gt 0 -and $bytes.Length -ge ($bodyStart + $contentLength)) {
            $body = [Text.Encoding]::UTF8.GetString($bytes, $bodyStart, $contentLength)
        }
        if ($req -match '^OPTIONS\s') {
            Write-LocalHttp $stream 204 '' $allowOrigin
            return
        }
        if ($req -match '^GET\s+/v1/ping' -or $req -match '^GET\s+/v1/wake' -or $req -match '^POST\s+/v1/wake') {
            Write-LocalHttp $stream 200 (Get-PingBody) $allowOrigin
            return
        }
        if ($req -match '^POST\s+/v1/job') {
            $job = $null
            try { $job = $body | ConvertFrom-Json } catch { }
            if ($null -eq $job) { Write-LocalHttp $stream 400 '{"ok":false}' $allowOrigin; return }
            $sid = [string]$job.session_id
            $secret = [string]$job.session_secret
            $kind = [string]$job.kind
            $origin = [string]$job.base_url
            if ($sid -notmatch '^[a-f0-9]{32}$') { Write-LocalHttp $stream 400 '{"ok":false}' $allowOrigin; return }
            if ($secret.Length -lt 16) { Write-LocalHttp $stream 400 '{"ok":false}' $allowOrigin; return }
            if ($kind -ne 'scan' -and $kind -ne 'install') { $kind = 'scan' }
            if (-not (Test-JobAllowed -JobUrl $origin -HeaderOrigin $headerOrigin -Sid $sid -Secret $secret)) {
                Write-LocalHttp $stream 403 '{"ok":false}' $allowOrigin
                return
            }
            $script:BaseUrl = $origin.TrimEnd('/')
            $script:SessionId = $sid
            $script:SessionSecret = $secret
            Persist-Device
            [void](Register-Device)
            $items = @()
            if ($job.items) { $items = @($job.items) }
            Enqueue-LocalJob @{ kind = $kind; session_id = $sid; session_secret = $secret; items = $items }
            Write-LocalHttp $stream 200 '{"ok":true}' $allowOrigin
            return
        }
        Write-LocalHttp $stream 404 '{"ok":false}' $allowOrigin
    } catch {
    } finally {
        try { if ($stream) { $stream.Close() } } catch { }
        try { $Client.Close() } catch { }
    }
}

function Start-LocalApi {
    try {
        $script:TcpListener = New-Object Net.Sockets.TcpListener ([Net.IPAddress]::Loopback), $script:LocalPort
        $script:TcpListener.Start()
    } catch { }
}

function Pump-LocalApi {
    if ($null -eq $script:TcpListener) { return }
    $n = 0
    while ($n -lt 16) {
        $n++
        $pending = $false
        try { $pending = $script:TcpListener.Pending() } catch { return }
        if (-not $pending) { return }
        try {
            $client = $script:TcpListener.AcceptTcpClient()
            Handle-LocalClient $client
        } catch { return }
    }
}

function Drain-Inbox {
    $inbox = Join-Path $script:HomeDir 'inbox.json'
    if (-not (Test-Path -LiteralPath $inbox)) { return }
    $raw = ''
    try { $raw = [IO.File]::ReadAllText($inbox) } catch { $raw = '' }
    try { Remove-Item -LiteralPath $inbox -Force -ErrorAction SilentlyContinue } catch { }
    if ([string]::IsNullOrWhiteSpace($raw)) { return }
    $msg = $null
    try { $msg = $raw | ConvertFrom-Json } catch { $msg = $null }
    if ($null -eq $msg) { return }
    $op = ''
    try { $op = ([string]$msg.op).Trim().ToLowerInvariant() } catch { $op = '' }
    if ($op -eq 'fridnett') {
        Start-InboxFridNett
    }
}

if (-not (Read-State)) {
    New-Item -ItemType Directory -Force -Path $script:HomeDir | Out-Null
}

Hide-ZorkConsole
$script:AppContext = New-Object System.Windows.Forms.ApplicationContext
$script:Notify = New-Object System.Windows.Forms.NotifyIcon
try {
    $script:Notify.Icon = New-ZorkIcon
} catch {
    $script:Notify.Icon = [System.Drawing.SystemIcons]::Application
}
$script:Notify.Text = 'Zork Helper'
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$quit = $menu.Items.Add('Avslutt Zork Helper')
$quit.add_Click({
    $script:Notify.Visible = $false
    $script:AppContext.ExitThread()
})
$script:Notify.ContextMenuStrip = $menu
$script:Notify.add_MouseClick({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        Show-Tip 'Zork Helper kjorer. Hoyreklikk for a avslutte.'
    }
})
$script:Notify.Visible = $true
try { Refresh-WifiStatus } catch { }
Start-LocalApi
try { Register-ZorkProtocol } catch { }
Show-Tip 'Zork Helper kjorer ved klokken.'
try {
    Set-Content -LiteralPath (Join-Path $script:HomeDir 'tray.pid') -Value $PID -Encoding ASCII
} catch { }

$script:BootJobsPending = $true
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 50
$script:Tick = 0
$timer.add_Tick({
    try {
        if ($script:BootJobsPending) {
            $script:BootJobsPending = $false
            $busyFlag = $false
            try { $busyFlag = Test-Path -LiteralPath (Join-Path $script:HomeDir 'install-busy.flag') } catch { $busyFlag = $false }
            if ((-not $busyFlag) -and (-not (Test-CaptiveOffline))) {
                if ([string]::IsNullOrWhiteSpace($script:DeviceId) -or [string]::IsNullOrWhiteSpace($script:DeviceSecret)) {
                    [void](Register-Device)
                }
                $job = Get-ServerJob
                if ($null -ne $job) {
                    Start-JobFromServer $job
                }
            }
        }
        Pump-LocalApi
        Drain-OpenUrl
        Drain-Incoming
        Drain-Inbox
        Finish-BusyIfNeeded
        $script:Tick++
        $wifiDue = (($script:Tick % 80) -eq 0) -or ($script:BusyKind -eq 'install' -and ($script:Tick % 20) -eq 0)
        if ($wifiDue) {
            try { Refresh-WifiStatus } catch { }
        }
        if (($script:Tick % 80) -ne 0) { return }
        if (Test-CaptiveOffline) { return }
        if ([string]::IsNullOrWhiteSpace($script:DeviceId)) {
            [void](Register-Device)
        }
        Invoke-Heartbeat
        if ($null -eq $script:BusyProc -and [string]::IsNullOrWhiteSpace($script:BusyKind)) {
            $next = Get-ServerJob
            if ($null -ne $next) {
                Start-JobFromServer $next
            }
        }
    } catch { }
})
$timer.Start()

[System.Windows.Forms.Application]::Run($script:AppContext)
$script:StopListen = $true
try { if ($script:TcpListener) { $script:TcpListener.Stop() } } catch { }
$timer.Stop()
$script:Notify.Visible = $false
try { Remove-Item -LiteralPath (Join-Path $script:HomeDir 'tray.pid') -Force -ErrorAction SilentlyContinue } catch { }
try { $mutex.ReleaseMutex() } catch { }
