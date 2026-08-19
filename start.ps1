# Helix start script (Windows) - runs in background.
# Python's own logging appends to ~/.helix/logs/helix.log; this script only
# captures stdout/stderr so startup crashes aren't lost.

$ErrorActionPreference = "Stop"

$HelixDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$HelixHome = Join-Path $env:USERPROFILE ".helix"
$LogDir = Join-Path $HelixHome "logs"
$PidFile = Join-Path $HelixHome "helix.pid"
$OutLog = Join-Path $LogDir "helix.console.log"
$ErrLog = Join-Path $LogDir "helix.console.err.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Don't start if already running
$MainPy = [System.IO.Path]::GetFullPath((Join-Path $HelixDir "main.py"))

function Get-HelixPids {
    $ids = @()
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        $cmd = $_.CommandLine
        if ($cmd -and $cmd.IndexOf($MainPy, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $ids += $_.ProcessId
        }
    }
    $ids | Select-Object -Unique
}

if (Test-Path $PidFile) {
    $ExistingPid = ((Get-Content $PidFile -Raw -Encoding UTF8) -replace '\uFEFF', '').Trim()
    if ($ExistingPid -match '^\d+$') {
        $proc = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Output "Helix already running (PID $ExistingPid)"
            exit 0
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

$live = @(Get-HelixPids)
if ($live) {
    Write-Output ("Helix already running (PID " + ($live -join ", ") + ") - PID file was stale")
    Write-Output "Fix: .\stop.ps1"
    exit 1
}

$port = 18791
$cfgPath = Join-Path $HelixHome "config.json"
if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.web.admin_port) { $port = [int]$cfg.web.admin_port }
    } catch {}
}
$bound = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
if ($bound) {
    Write-Output ("ERROR: port $port is already in use (PID " + (($bound.OwningProcess | Select-Object -Unique) -join ", ") + ")")
    Write-Output "Fix: .\stop.ps1"
    exit 1
}

# Preflight checks
$Python = Join-Path $HelixDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Output "ERROR: Virtual environment not found."
    Write-Output "Fix: Run the setup wizard first: python main.py setup"
    exit 1
}

# Python version check (3.10+)
$verOut = & $Python -c "import sys; print(sys.version_info.major, sys.version_info.minor)"
if (-not $?) {
    Write-Output "ERROR: Could not determine Python version."
    Write-Output "Fix: Recreate the venv: python main.py setup"
    exit 1
}
$parts = $verOut.Trim() -split '\s+'
$major = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Output "ERROR: Python $major.$minor is too old. Helix requires Python 3.10+."
    Write-Output "Fix: Install Python 3.10+ from https://python.org then re-run: python main.py setup"
    exit 1
}

# Claude Code CLI check
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    foreach ($cand in @(
        (Join-Path $env:USERPROFILE ".local\bin\claude.exe"),
        (Join-Path $env:APPDATA "npm\claude.cmd")
    )) {
        if (Test-Path $cand) { $claude = $cand; break }
    }
}
if (-not $claude) {
    Write-Output "ERROR: Claude Code CLI not found."
    Write-Output "Fix: Install Claude Code from https://docs.anthropic.com/claude-code"
    exit 1
}

# Helix uses its own isolated Claude Code config dir but shares your login.
# Copy the credential file across when yours is newer, so a token refresh in
# your normal CLI propagates here.
$HelixClaudeDir = Join-Path $HelixHome "claude"
$SrcCred = Join-Path $env:USERPROFILE ".claude\.credentials.json"
$DstCred = Join-Path $HelixClaudeDir ".credentials.json"
if (Test-Path $SrcCred) {
    $needsCopy = $true
    if (Test-Path $DstCred) {
        $needsCopy = (Get-Item $SrcCred).LastWriteTime -gt (Get-Item $DstCred).LastWriteTime
    }
    if ($needsCopy) {
        New-Item -ItemType Directory -Force -Path $HelixClaudeDir | Out-Null
        Copy-Item $SrcCred $DstCred -Force
    }
}
if (-not (Test-Path $DstCred)) {
    Write-Output "ERROR: no Claude Code credentials found to share."
    Write-Output "Fix: log in to Claude Code normally (run 'claude'), then retry."
    exit 1
}

# Dependencies check. Presence only - version-range comparison would need
# `packaging`, which isn't a declared dependency.
$depCheck = @'
import sys
from importlib.metadata import distribution, PackageNotFoundError
import re
missing = []
with open("requirements.txt") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[;]", line)[0].strip()
        try:
            distribution(name)
        except PackageNotFoundError:
            missing.append(name)
for m in missing:
    print("  Missing: " + m)
'@
Push-Location $HelixDir
$depIssues = $depCheck | & $Python -W ignore -
Pop-Location

if ($depIssues) {
    Write-Output "ERROR: Missing dependencies:"
    $depIssues | ForEach-Object { Write-Output $_ }
    Write-Output ""
    Write-Output "Fix: .venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt"
    exit 1
}

# Launch
$env:PYTHONUNBUFFERED = "1"
# Redirecting stdout to a file makes Python fall back to the ANSI codepage
# (cp1252), which raises UnicodeEncodeError on the agent's emoji. Force UTF-8.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$proc = Start-Process -FilePath $Python `
    -ArgumentList (Join-Path $HelixDir "main.py") `
    -WorkingDirectory $HelixDir `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden `
    -PassThru

[System.IO.File]::WriteAllText($PidFile, "$($proc.Id)`n")
Write-Output "Helix started (PID $($proc.Id))"
