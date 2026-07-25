# One-shot local setup: create .venv and install companion + watcher deps.
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = 'Stop'

$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host 'Creating virtualenv at .venv'
    python -m venv .venv
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r companion\requirements.txt
& $VenvPython -m pip install -r watcher\requirements.txt

Write-Host ''
Write-Host 'Setup complete.'
Write-Host '  Start companion: powershell -ExecutionPolicy Bypass -File companion\scripts\start_windows.ps1'
Write-Host '  Stop companion:  powershell -ExecutionPolicy Bypass -File companion\scripts\stop_windows.ps1'
