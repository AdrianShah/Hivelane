# One-shot local setup: install the hivelane daemon and enable the repo git hooks.
# Mirrors setup.sh.
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

# Graph auto-refresh on commit + conflict-free graph.json merges (see .githooks/)
git config core.hooksPath .githooks
git config merge.graphify.name 'graphify union merge'
git config merge.graphify.driver 'graphify merge-driver %O %A %B'
if (-not (Get-Command graphify -ErrorAction SilentlyContinue)) {
    Write-Host "Tip: install graphify for the code graph:  uv tool install 'graphifyy[mcp]'"
}

Write-Host ''
Write-Host 'Setup complete.'
Write-Host '  Install agent hooks: .venv\Scripts\hivelane hook install claude-code'
Write-Host '  Join a room:         .venv\Scripts\hivelane join <ROOM> --name <your-name>'
