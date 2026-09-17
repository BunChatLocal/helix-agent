#Requires -Version 5.1
# Protocol handler: zorkhelper://wake
# Starts the tray if it is not already listening. Never puts secrets in the URL.
param([string]$Uri = '')

$ErrorActionPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$homeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
$tray = Join-Path $homeDir 'zork-helper-tray.ps1'
$inbox = Join-Path $homeDir 'inbox.json'
$pingUrl = 'http://127.0.0.1:18792/v1/wake'

function Test-LocalHelper {
    try {
        $req = [Net.HttpWebRequest]::Create($pingUrl)
        $req.Method = 'GET'
        $req.Timeout = 800
        $resp = $req.GetResponse()
        $ok = [int]$resp.StatusCode -ge 200 -and [int]$resp.StatusCode -lt 300
        $resp.Close()
        return $ok
    } catch {
        return $false
    }
}

New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
try {
    Set-Content -LiteralPath $inbox -Value '{"op":"wake"}' -Encoding ASCII
} catch { }

if (Test-LocalHelper) { exit 0 }

if (-not (Test-Path -LiteralPath $tray)) { exit 1 }

Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-STA', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $tray
) | Out-Null

$n = 0
while ($n -lt 40) {
    Start-Sleep -Milliseconds 250
    if (Test-LocalHelper) { exit 0 }
    $n++
}
exit 0
