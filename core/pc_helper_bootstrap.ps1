#Requires -Version 5.1
# First-run window: progress bar, then the tray stays by the clock.
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$SessionId,
    [Parameter(Mandatory = $true)][string]$Secret,
    [string]$JobKind = '__DEFAULT_JOB_KIND__',
    [AllowEmptyString()][string]$JobItems = '__DEFAULT_JOB_ITEMS__'
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { $Host.UI.RawUI.WindowTitle = 'Zork Helper' } catch { }

function Send-BootProgress {
    param([string]$Text)
    try {
        $pwc = New-Object System.Net.WebClient
        $pwc.Encoding = [Text.Encoding]::UTF8
        $pwc.Headers.Add('Content-Type', 'application/json; charset=utf-8')
        $pwc.Headers.Add('X-Helper-Secret', $Secret)
        $clip = $Text
        if ($clip.Length -gt 180) { $clip = $clip.Substring(0, 180) }
        $body = (@{ text = $clip } | ConvertTo-Json -Compress)
        [void]$pwc.UploadString(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/progress'), 'POST', $body)
    } catch { }
}

function Show-ZorkProgress {
    param([int]$Percent, [string]$Text)
    $pct = [Math]::Max(0, [Math]::Min(100, $Percent))
    $fill = [int][Math]::Floor($pct / 5)
    if ($fill -gt 20) { $fill = 20 }
    $bar = ('#' * $fill) + ('-' * (20 - $fill))
    $line = '  [{0}] {1,3}%  Loading...  {2}' -f $bar, $pct, $Text
    if ($line.Length -gt 79) { $line = $line.Substring(0, 79) }
    Write-Progress -Activity 'Zork Helper' -Status ('Loading... {0}%' -f $pct) -PercentComplete $pct -CurrentOperation $Text
    Write-Host ("`r" + $line.PadRight(79)) -NoNewline
    if ($Text -ne 'Ferdig') { Send-BootProgress $Text }
}

function Show-ZorkError {
    param([string]$Message)
    try { Write-Progress -Activity 'Zork Helper' -Completed } catch { }
    Write-Host ''
    Write-Host '  Feil' -ForegroundColor Red
    Write-Host ('  ' + $Message) -ForegroundColor Red
}

function Send-BootError {
    param([string]$Message)
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Encoding = [Text.Encoding]::UTF8
        $wc.Headers.Add('Content-Type', 'application/json; charset=utf-8')
        $wc.Headers.Add('X-Helper-Secret', $Secret)
        $clip = $Message
        if ($clip.Length -gt 180) { $clip = $clip.Substring(0, 180) }
        $body = (@{ message = $clip } | ConvertTo-Json -Compress)
        [void]$wc.UploadString(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/error'), 'POST', $body)
    } catch { }
}

function Send-LocalJob {
    param([string]$Kind, [string]$Items)
    $payload = @{
        base_url        = $BaseUrl.TrimEnd('/')
        session_id      = $SessionId
        session_secret  = $Secret
        kind            = $Kind
        items           = ([string]$Items)
    } | ConvertTo-Json -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $req = [Net.HttpWebRequest]::Create('http://127.0.0.1:18792/v1/job')
    $req.Method = 'POST'
    $req.ContentType = 'application/json; charset=utf-8'
    $req.Timeout = 8000
    $req.ContentLength = $bytes.Length
    $st = $req.GetRequestStream()
    $st.Write($bytes, 0, $bytes.Length)
    $st.Close()
    $resp = $req.GetResponse()
    $code = [int]$resp.StatusCode
    $resp.Close()
    if ($code -lt 200 -or $code -ge 300) {
        throw 'Zork Helper tok ikke imot jobben.'
    }
}

Write-Host ''
Write-Host '  Zork Helper' -ForegroundColor Cyan
$jobKind = ([string]$JobKind).Trim().ToLower()
if ($jobKind -ne 'install') { $jobKind = 'scan' }
if ($jobKind -eq 'install') {
    Write-Host '  Starter Zork Helper...' -ForegroundColor Gray
} else {
    Write-Host '  Sjekker denne PC-en...' -ForegroundColor Gray
}
Write-Host ''

$homeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
$tray = Join-Path $homeDir 'zork-helper-tray.ps1'
$installSeed = Join-Path $homeDir 'zork-helper-install.ps1'
$installCache = Join-Path $homeDir 'install-script.ps1'
$scan = Join-Path $env:TEMP ('zork-scan-' + $SessionId + '.ps1')
$cfgPath = Join-Path $homeDir 'config.json'
$wc = New-Object System.Net.WebClient
$wc.Encoding = [Text.Encoding]::UTF8
$wc.Headers.Add('X-Helper-Secret', $Secret)

try {
    Show-ZorkProgress 8 'Starter...'
    New-Item -ItemType Directory -Force -Path $homeDir | Out-Null

    Show-ZorkProgress 18 'Laster ned hjelperen...'
    $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/tray'), $tray)
    $open = Join-Path $homeDir 'zork-helper-open.ps1'
    $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/open'), $open)
    Show-ZorkProgress 24 'Forbereder FRID-nett...'
    try {
        $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/install-script'), $installSeed)
        if ((Test-Path -LiteralPath $installSeed) -and ((Get-Item -LiteralPath $installSeed).Length -gt 2000)) {
            Copy-Item -LiteralPath $installSeed -Destination $installCache -Force
        }
    } catch { }

    if ($jobKind -eq 'scan') {
        Show-ZorkProgress 28 'Laster ned sjekken...'
        $wc.DownloadFile(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/script'), $scan)
    }

    Show-ZorkProgress 36 'Lagrer innstillinger...'
    $cfg = '{"base_url":"' + $BaseUrl.TrimEnd('/') + '","session_id":"' + $SessionId + '","session_secret":"' + $Secret + '","skip_scan":true}'
    Set-Content -LiteralPath $cfgPath -Value $cfg -Encoding UTF8

    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (-not (Test-Path -LiteralPath $run)) { New-Item -Path $run -Force | Out-Null }
    $startCmd = '"' + $ps + '" -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '"'
    Set-ItemProperty -LiteralPath $run -Name 'ZorkHelper' -Value $startCmd

    $openCmd = '"' + $ps + '" -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $open + '" "%1"'
    $rk = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\zorkhelper')
    $rk.SetValue('', 'URL:Zork Helper')
    $rk.SetValue('URL Protocol', '')
    $rk.CreateSubKey('shell\open\command').SetValue('', $openCmd)
    $rk.Close()

    Show-ZorkProgress 42 'Oppdaterer hjelperen...'
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*zork-helper-tray.ps1*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 300

    Show-ZorkProgress 50 'Starter Zork Helper ved klokken...'
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $trayProc = Start-Process -FilePath $ps -PassThru -ArgumentList @(
        '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', $tray
    )
    if (-not $trayProc) {
        throw 'Klarte ikke starte Zork Helper ved klokken.'
    }
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        try {
            $req = [Net.HttpWebRequest]::Create('http://127.0.0.1:18792/v1/ping')
            $req.Method = 'GET'
            $req.Timeout = 400
            $resp = $req.GetResponse()
            $resp.Close()
            $up = $true
            break
        } catch { }
    }
    if (-not $up) {
        throw 'Zork Helper startet, men ikonet ved klokken svarte ikke.'
    }
    try {
        $wc.Headers['Content-Type'] = 'application/json; charset=utf-8'
        [void]$wc.UploadString(($BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId + '/attach'), 'POST', '{}')
    } catch { }

    if ($jobKind -eq 'install') {
        Show-ZorkProgress 70 'Starter jobben...'
        Send-LocalJob -Kind 'install' -Items $JobItems
    } else {
        Show-ZorkProgress 58 'Sjekker programmer...'
        $p = Start-Process -FilePath powershell.exe -Wait -PassThru -NoNewWindow -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scan,
            '-BaseUrl', $BaseUrl, '-SessionId', $SessionId, '-Secret', $Secret
        )
        Remove-Item -LiteralPath $scan -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -and $p.ExitCode -ne 0) {
            throw "Sjekken avsluttet med kode $($p.ExitCode)"
        }
    }

    Show-ZorkProgress 100 'Ferdig'
    try { Write-Progress -Activity 'Zork Helper' -Completed } catch { }
    Write-Host ''
    Write-Host ''
    Write-Host '  Ferdig. Zork Helper ligger ved klokken.' -ForegroundColor Green
    Write-Host '  Du kan lukke dette vinduet.' -ForegroundColor Gray
    Start-Sleep -Seconds 4
    exit 0
} catch {
    $msg = [string]$_.Exception.Message
    Send-BootError $msg
    Show-ZorkError $msg
    Write-Host ''
    Write-Host '  Trykk en tast for aa lukke.' -ForegroundColor Gray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep -Seconds 20 }
    exit 1
}
