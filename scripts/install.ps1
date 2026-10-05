<#
.SYNOPSIS
    Install the Synergo CLI on Windows.

.DESCRIPTION
    Uses uv if it is installed, else pipx. With neither, installs uv with uv's
    official installer first (uv also fetches a suitable Python if you lack
    3.12+). Asks no questions; every command is printed before it runs.

    Environment:
      SYNERGO_VERSION=0.1.0   install that exact version instead of the latest
      SYNERGO_DRY_RUN=1       same as -DryRun (for `irm ... | iex`, which cannot
                              pass parameters)

.PARAMETER DryRun
    Print the commands without running any of them.

.EXAMPLE
    irm https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.ps1 | iex

.EXAMPLE
    powershell -ExecutionPolicy ByPass -File scripts\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

# Everything runs inside a function: under `irm | iex` the script shares the
# caller's session, so a top-level $ErrorActionPreference would leak into it and
# an `exit` would close their window. Failures `throw` instead, which still gives
# a non-zero exit code under `powershell -File`.
function Install-Synergo {
    param([bool]$DryRun)

    $ErrorActionPreference = 'Stop'

    if ($env:SYNERGO_DRY_RUN -in @('1', 'true', 'yes')) { $DryRun = $true }

    function Say([string]$Message) { Write-Host "synergo-install: $Message" }

    # Print a command, then run it unless this is a dry run.
    function Invoke-Step([string]$Exe, [string[]]$Arguments) {
        $shown = $Arguments | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }
        Write-Host "  > $Exe $($shown -join ' ')"
        if ($DryRun) { return }
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Exe exited with code $LASTEXITCODE" }
    }

    function Test-Command([string]$Name) {
        return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
    }

    $version = "$env:SYNERGO_VERSION".Trim()
    if ($version.StartsWith('v')) { $version = $version.Substring(1) }
    if ($version -and $version -notmatch '^[0-9A-Za-z.+-]+$') {
        throw "SYNERGO_VERSION does not look like a version: $env:SYNERGO_VERSION"
    }
    $spec = if ($version) { "synergo==$version" } else { 'synergo' }

    if ($DryRun) { Say 'dry run: commands are printed, nothing is installed' }

    if (Test-Command 'uv') {
        $tool = 'uv'
    } elseif (Test-Command 'pipx') {
        $tool = 'pipx'
    } else {
        $installer = 'https://astral.sh/uv/install.ps1'
        Say "neither uv nor pipx found; installing uv from $installer"
        # A child process, exactly as uv's docs show it, so the installer's own
        # settings never leak into this session.
        Invoke-Step 'powershell' @('-NoProfile', '-ExecutionPolicy', 'ByPass', '-Command', "irm $installer | iex")
        # The installer updates the user PATH for new terminals only; look where
        # it installs (same precedence as the installer) for this one.
        $candidates = @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME, (Join-Path $HOME '.local\bin'), (Join-Path $HOME '.cargo\bin'))
        foreach ($dir in $candidates) {
            if ($dir -and (Test-Path $dir) -and -not (($env:Path -split ';') -contains $dir)) {
                $env:Path = "$dir;$env:Path"
            }
        }
        if (-not $DryRun -and -not (Test-Command 'uv')) {
            throw 'uv was installed but is not on PATH; open a new terminal and re-run this script'
        }
        $tool = 'uv'
    }

    Say "installing $spec with $tool"
    if ($tool -eq 'uv') {
        # --upgrade so re-running the script moves an existing install to the latest.
        Invoke-Step 'uv' @('tool', 'install', '--upgrade', $spec)
    } else {
        # --force so re-running the script reinstalls.
        Invoke-Step 'pipx' @('install', '--force', $spec)
    }

    if ($DryRun) { return }

    # Agent hooks run `synergo` from PATH, so a missing PATH entry silently turns
    # coordination off (hooks fail open). Nothing later would say so; say it now.
    if (-not (Test-Command 'synergo')) {
        $binDir = $null
        if ($tool -eq 'uv') {
            # try: Windows PowerShell turns redirected native stderr into errors.
            try { $binDir = (& uv tool dir --bin 2>$null | Select-Object -First 1) } catch { }
            $fix = 'uv tool update-shell'
        } else {
            $binDir = Join-Path $HOME '.local\bin'
            $fix = 'pipx ensurepath'
        }
        if (-not $binDir) { $binDir = Join-Path $HOME '.local\bin' }
        Say "installed, but $binDir is not on your PATH yet."
        Say "run ``$fix``, then open a new terminal. Agent hooks need ``synergo`` on PATH."
        $env:Path = "$binDir;$env:Path"
    }

    $installed = 'synergo'
    try { $installed = (& synergo --version 2>$null | Select-Object -First 1) } catch { }

    Write-Host ''
    Write-Host "$installed is installed."
    Write-Host ''
    Write-Host 'Next, in a repository you work in:'
    Write-Host ''
    Write-Host '  cd path\to\your-repo'
    Write-Host '  synergo up        # install agent hooks, create or join the room, start the daemon'
    Write-Host '  synergo doctor    # if anything looks off'
    Write-Host ''
    Write-Host 'Docs: https://github.com/AdrianShah/Synergo/blob/main/docs/install.md'
}

Install-Synergo -DryRun $DryRun.IsPresent
