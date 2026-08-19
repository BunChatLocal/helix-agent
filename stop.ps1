# Helix stop script (Windows)
#
# The venv launcher re-spawns the base interpreter, so the PID file records the
# parent while a child may be the one holding the port. Matching only the PID
# therefore reports "not running" while 18791 is still served. Kill every
# process whose command line points at this repo's main.py, then free the port
# if a leftover is still bound.

$ErrorActionPreference = "Continue"
$HelixDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $env:USERPROFILE ".helix\helix.pid"
$MainPy = [System.IO.Path]::GetFullPath((Join-Path $HelixDir "main.py"))
$Port = 18791
$CfgPath = Join-Path $env:USERPROFILE ".helix\config.json"
if (Test-Path $CfgPath) {
    try {
        $cfg = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.web.admin_port) { $Port = [int]$cfg.web.admin_port }
    } catch {}
}

function Get-HelixPids {
    $ids = @()
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        $cmd = $_.CommandLine
        if (-not $cmd) { return }
        # Literal match: the old regex doubled backslashes and silently matched
        # nothing, which is how a "stale PID" left two live processes on 18791.
        if ($cmd.IndexOf($MainPy, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $ids += $_.ProcessId
        }
    }
    $ids | Select-Object -Unique
}

function Get-PortPids {
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

$pids = @(Get-HelixPids)
if (Test-Path $PidFile) {
    $fromFile = ((Get-Content $PidFile -Raw -Encoding UTF8) -replace '\uFEFF', '').Trim()
    if ($fromFile -match '^\d+$') { $pids += [int]$fromFile }
}

$pids = $pids | Select-Object -Unique | Where-Object {
    $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue)
}

$killed = @()
foreach ($id in $pids) {
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    $killed += $id
}

# A child can outlive a killed parent for a moment. Wait, then catch leftovers.
for ($i = 0; $i -lt 10; $i++) {
    $left = @(Get-HelixPids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if (-not $left) { break }
    Start-Sleep -Seconds 1
    $left | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        $killed += $_
    }
}

$portHolders = @(Get-PortPids | Where-Object {
    $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue)
})
foreach ($id in $portHolders) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    $cmd = if ($proc) { $proc.CommandLine } else { "" }
    if ($cmd -and $cmd.IndexOf($MainPy, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        $killed += $id
    }
}

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
$killed = @($killed | Select-Object -Unique)
$still = @(Get-PortPids)

if ($killed.Count -gt 0) {
    Write-Output ("Helix stopped (PID " + ($killed -join ", ") + ")")
} else {
    Write-Output "Helix was not running"
}
if ($still) {
    Write-Output ("WARNING: port $Port still in use by PID " + ($still -join ", ") + " - not killed, command line did not match $MainPy")
}
