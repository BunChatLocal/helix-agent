# Copy the committed Helix home onto this Windows user.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\restore-home.ps1
$ErrorActionPreference = "Stop"
$Src = Join-Path (Split-Path -Parent $PSScriptRoot) "runtime-home"
$Dest = Join-Path $env:USERPROFILE ".helix"
if (-not (Test-Path (Join-Path $Src "config.json"))) {
    throw "runtime-home is missing. Clone with Git LFS so the printer installer is included."
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
& robocopy $Src $Dest /E /R:1 /W:1 | Out-Host
if ($LASTEXITCODE -ge 8) { exit $LASTEXITCODE }
Write-Output "Restored Helix home to $Dest"
Write-Output "Then, from the repo root: python -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt"
