# Installing Synergo

Synergo's CLI is the Python package [`synergo`](https://pypi.org/project/synergo/)
(Python 3.12+). Every channel below installs the same `synergo` command; pick
whichever your machine already uses.

| Channel | Command | Notes |
|---------|---------|-------|
| pipx | `pipx install synergo` | The default. Isolated environment, command on PATH. |
| uv | `uv tool install synergo` | Same, faster; uv fetches Python 3.12+ if you lack it. |
| Install script (macOS/Linux) | `curl -LsSf https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.sh \| sh` | Uses uv or pipx; installs uv if you have neither. |
| Install script (Windows) | `irm https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.ps1 \| iex` | Same, in PowerShell. |
| Homebrew | `brew install adrianshah/synergo/synergo` | Once the tap is published. |
| Scoop | `scoop bucket add synergo https://github.com/AdrianShah/scoop-synergo` then `scoop install synergo/synergo` | Once the bucket is published. |
| npm / npx | `npx synergo-cli up` | A launcher that runs `uvx`/`pipx run`. For trying it out; see below. |
| From a checkout | `pipx install .` or `uv tool install --editable .` | For development, or before the first PyPI release. |

Then, in a repository you work in:

```bash
synergo up
```

## Why "persistent" matters

`synergo up` installs agent hooks (for Claude Code, into `.claude/settings.json`)
that run `synergo hook claude-code` around every edit. They find `synergo` on
your **PATH**. Every channel above except `npx` puts it there permanently. If
the hooks cannot find it, they fail open: the agent keeps working, but nothing
is coordinated, and nothing tells you. The install scripts warn about it; with
the other channels, check that `synergo --version` works in a new terminal.

If `synergo` is "not found" right after installing, the tool's bin directory is
not on PATH yet:

```bash
uv tool update-shell     # uv
pipx ensurepath          # pipx
```

Then open a new terminal.

## pipx

```bash
pipx install synergo
pipx upgrade synergo                       # later
pipx install "synergo==0.1.0" --force      # a specific version
pipx uninstall synergo
```

pipx uses its default Python; if that is older than 3.12, add
`--python python3.12` (or install with uv, which brings its own).

## uv

```bash
uv tool install synergo
uv tool upgrade synergo                    # later
uv tool install "synergo==0.1.0"           # a specific version
uv tool uninstall synergo
```

## Install scripts

`scripts/install.sh` (POSIX `sh`) and `scripts/install.ps1` (Windows PowerShell
5.1+ and PowerShell 7) do the same thing without asking questions:

1. use `uv` if it is installed, else `pipx`;
2. with neither, install uv using [uv's official installer](https://docs.astral.sh/uv/getting-started/installation/);
3. run `uv tool install --upgrade synergo` (or `pipx install --force synergo`), so
   re-running upgrades;
4. warn if `synergo` is not on PATH, and print the next steps.

Every command is printed before it runs. To see them without running anything:

```bash
curl -LsSf https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.sh | sh -s -- --dry-run
sh scripts/install.sh --dry-run                       # from a checkout
```

```powershell
powershell -ExecutionPolicy ByPass -File scripts\install.ps1 -DryRun
$env:SYNERGO_DRY_RUN = '1'; irm https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.ps1 | iex
```

Pin a version with `SYNERGO_VERSION` (a leading `v` is fine):

```bash
curl -LsSf https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.sh | SYNERGO_VERSION=0.1.0 sh
```

```powershell
$env:SYNERGO_VERSION = '0.1.0'; irm https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.ps1 | iex
```

## Homebrew

```bash
brew install adrianshah/synergo/synergo
brew upgrade synergo
```

The formula installs Synergo into its own virtualenv on Homebrew's
`python@3.12`. Each stable release updates it automatically. Maintainer
notes are in [packaging/homebrew](../packaging/homebrew/README.md).

## Scoop

```powershell
scoop bucket add synergo https://github.com/AdrianShah/scoop-synergo
scoop install synergo/synergo
scoop update synergo
```

The manifest verifies the wheel's hash from PyPI and installs it with uv
(pulled in as a dependency) into Scoop's own app directory, so
`scoop uninstall synergo` removes everything. Maintainer notes are in
[packaging/scoop](../packaging/scoop/README.md).

## npm / npx

```bash
npx synergo-cli --version
npx synergo-cli up
```

[`synergo-cli`](../packaging/npm/README.md) is a zero-dependency launcher that
runs `uvx --from synergo synergo …` (falling back to `uv tool run`, then
`pipx run`), passing your arguments and exit code through. It needs uv or pipx.
It does **not** put `synergo` on your PATH, so agent hooks will not find it.
Use it to try Synergo, then install with one of the persistent channels.
`SYNERGO_VERSION` pins the version here too.

## From a checkout

```bash
git clone https://github.com/AdrianShah/Synergo && cd Synergo
pipx install .                    # a snapshot of this checkout
uv tool install --editable .      # tracks your edits
```

For working on Synergo itself (relay, dashboard, tests), see
[Develop locally](../README.md#develop-locally).

## Verify

```bash
synergo --version
synergo doctor        # in a repo: Python, .synergo.toml, hooks, daemon, relay
```

## Uninstall

Stop the daemon and remove the login item before removing the package:

```bash
synergo down
synergo service uninstall          # only if you ran `synergo service install`
synergo hook uninstall claude-code # in each repo you set up (add --global if you used it)
pipx uninstall synergo             # or: uv tool uninstall / brew uninstall / scoop uninstall
```

Personal settings, daemon state and logs live in `~/.synergo` (or
`SYNERGO_HOME`); delete it to remove every trace. Each repo's committed
`.synergo.toml` belongs to the team, so leave it unless the whole team is
leaving the room (`synergo leave --forget`).
