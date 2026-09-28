# One-shot local setup: install the synergo daemon.
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
& $VenvPython -m pip install -e .

Write-Host ''
Write-Host 'Setup complete.'
Write-Host '  Install agent hooks: .venv\Scripts\synergo hook install claude-code'
Write-Host '  Join a room:         .venv\Scripts\synergo join <ROOM> --name <your-name>'
