# Stop the background companion started by start_windows.ps1.
# Usage: powershell -ExecutionPolicy Bypass -File companion\scripts\stop_windows.ps1
$ErrorActionPreference = 'Stop'

$CompanionDir = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $CompanionDir '.pid'

if (-not (Test-Path $PidFile)) {
    Write-Host 'No PID file; companion does not appear to be running.'
    exit 0
}

$companionPid = Get-Content $PidFile
$process = Get-Process -Id $companionPid -ErrorAction SilentlyContinue
if (-not $process) {
    Write-Host "Stale PID file for $companionPid; removing."
    Remove-Item $PidFile -Force
    exit 0
}

# CloseMainWindow has no effect on a hidden console app, so stop directly.
Stop-Process -Id $companionPid
for ($i = 0; $i -lt 10; $i++) {
    if (-not (Get-Process -Id $companionPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}

if (Get-Process -Id $companionPid -ErrorAction SilentlyContinue) {
    Write-Host 'Companion did not exit; forcing.'
    Stop-Process -Id $companionPid -Force
}

Remove-Item $PidFile -Force
Write-Host 'Companion stopped.'
