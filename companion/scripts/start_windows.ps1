# Start the companion hidden in the background and record its PID.
# Usage: powershell -ExecutionPolicy Bypass -File companion\scripts\start_windows.ps1
$ErrorActionPreference = 'Stop'

$CompanionDir = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $CompanionDir
$PidFile = Join-Path $CompanionDir '.pid'
$LogFile = Join-Path $CompanionDir 'companion.log'

if (Test-Path $PidFile) {
    $existing = Get-Content $PidFile
    if (Get-Process -Id $existing -ErrorAction SilentlyContinue) {
        Write-Host "Companion already running (PID $existing)."
        exit 0
    }
    Remove-Item $PidFile -Force
}

$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }

$process = Start-Process -FilePath $Python `
    -ArgumentList '-m', 'companion.main' `
    -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError "$LogFile.err" `
    -PassThru

Set-Content -Path $PidFile -Value $process.Id
Start-Sleep -Seconds 1

if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
    Write-Host "Companion started (PID $($process.Id)) on http://127.0.0.1:4500"
    Write-Host "Logs: $LogFile"
} else {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Error "Companion failed to start. See $LogFile.err"
    exit 1
}
