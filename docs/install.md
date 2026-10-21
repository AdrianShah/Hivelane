# Installing Hivelane

Hivelane's CLI is the Python package [`hivelane`](https://pypi.org/project/hivelane/)
(Python 3.12+). Every channel below installs the same `hivelane` command; pick
whichever your machine already uses.

| Channel | Command | Notes |
|---------|---------|-------|
| pipx | `pipx install hivelane` | The default. Isolated environment, command on PATH. |
| uv | `uv tool install hivelane` | Same, faster; uv fetches Python 3.12+ if you lack it. |
| Install script (macOS/Linux) | `curl -LsSf https://raw.githubusercontent.com/AdrianShah/Hivelane/main/scripts/install.sh \| sh` | Uses uv or pipx; installs uv if you have neither. |
| Install script (Windows) | `irm https://raw.githubusercontent.com/AdrianShah/Hivelane/main/scripts/install.ps1 \| iex` | Same, in PowerShell. |
| Homebrew | `brew install adrianshah/hivelane/hivelane` | Once the tap is published. |
| Scoop | `scoop bucket add hivelane https://github.com/AdrianShah/scoop-hivelane` then `scoop install hivelane/hivelane` | Once the bucket is published. |
| npm / npx | `npx hivelane-cli up` | A launcher that runs `uvx`/`pipx run`. For trying it out; see below. |
| From a checkout | `pipx install .` or `uv tool install --editable .` | For development, or before the first PyPI release. |

> **Until the first release:** the repository is private and nothing is
> published to PyPI, Homebrew, Scoop or npm yet. The PyPI, Homebrew, Scoop and
> npm commands fail, and the install-script URLs on `raw.githubusercontent.com`
> return 404 for anyone without access to the repository. Install from a
> checkout instead (the last row). Everything else on this page applies once
> the repository is public or the packages are published.

Then, in a repository you work in:

```bash
hivelane up
```

## Why "persistent" matters

`hivelane up` installs agent hooks (for Claude Code, into `.claude/settings.json`)
that run `hivelane hook claude-code` around every edit. They find `hivelane` on
your **PATH**. Every channel above except `npx` puts it there permanently. If
the hooks cannot find it, they fail open: the agent keeps working, but nothing
is coordinated, and nothing tells you. The install scripts warn about it; with
the other channels, check that `hivelane --version` works in a new terminal.

If `hivelane` is "not found" right after installing, the tool's bin directory is
not on PATH yet:

```bash
uv tool update-shell     # uv
pipx ensurepath          # pipx
```

Then open a new terminal.

## pipx

```bash
pipx install hivelane
pipx upgrade hivelane                       # later
pipx install "hivelane==0.1.0" --force      # a specific version
pipx uninstall hivelane
```

pipx uses its default Python; if that is older than 3.12, add
`--python python3.12` (or install with uv, which brings its own).

## uv

```bash
uv tool install hivelane
uv tool upgrade hivelane                    # later
uv tool install "hivelane==0.1.0"           # a specific version
uv tool uninstall hivelane
```

## Install scripts

`scripts/install.sh` (POSIX `sh`) and `scripts/install.ps1` (Windows PowerShell
5.1+ and PowerShell 7) do the same thing without asking questions:

1. use `uv` if it is installed, else `pipx`;
2. with neither, install uv using [uv's official installer](https://docs.astral.sh/uv/getting-started/installation/);
3. run `uv tool install --upgrade hivelane` (or `pipx install --force hivelane`), so
   re-running upgrades;
4. warn if `hivelane` is not on PATH, and print the next steps.

Every command is printed before it runs. To see them without running anything:

```bash
curl -LsSf https://raw.githubusercontent.com/AdrianShah/Hivelane/main/scripts/install.sh | sh -s -- --dry-run
sh scripts/install.sh --dry-run                       # from a checkout
```

```powershell
powershell -ExecutionPolicy ByPass -File scripts\install.ps1 -DryRun
$env:HIVELANE_DRY_RUN = '1'; irm https://raw.githubusercontent.com/AdrianShah/Hivelane/main/scripts/install.ps1 | iex
```

Pin a version with `HIVELANE_VERSION` (a leading `v` is fine):

```bash
curl -LsSf https://raw.githubusercontent.com/AdrianShah/Hivelane/main/scripts/install.sh | HIVELANE_VERSION=0.1.0 sh
```

```powershell
$env:HIVELANE_VERSION = '0.1.0'; irm https://raw.githubusercontent.com/AdrianShah/Hivelane/main/scripts/install.ps1 | iex
```

## Homebrew

```bash
brew install adrianshah/hivelane/hivelane
brew upgrade hivelane
```

The formula installs Hivelane into its own virtualenv on Homebrew's
`python@3.12`. Each stable release updates it automatically. Maintainer
notes are in [packaging/homebrew](../packaging/homebrew/README.md).

## Scoop

```powershell
scoop bucket add hivelane https://github.com/AdrianShah/scoop-hivelane
scoop install hivelane/hivelane
scoop update hivelane
```

The manifest verifies the wheel's hash from PyPI and installs it with uv
(pulled in as a dependency) into Scoop's own app directory, so
`scoop uninstall hivelane` removes everything. Maintainer notes are in
[packaging/scoop](../packaging/scoop/README.md).

## npm / npx

```bash
npx hivelane-cli --version
npx hivelane-cli up
```

[`hivelane-cli`](../packaging/npm/README.md) is a zero-dependency launcher that
runs `uvx --from hivelane hivelane …` (falling back to `uv tool run`, then
`pipx run`), passing your arguments and exit code through. It needs uv or pipx.
It does **not** put `hivelane` on your PATH, so agent hooks will not find it.
Use it to try Hivelane, then install with one of the persistent channels.
`HIVELANE_VERSION` pins the version here too.

## From a checkout

```bash
git clone https://github.com/AdrianShah/Hivelane && cd Hivelane
pipx install .                    # a snapshot of this checkout
uv tool install --editable .      # tracks your edits
```

For working on Hivelane itself (relay, dashboard, tests), see
[Develop locally](../README.md#develop-locally).

## Verify

```bash
hivelane --version
hivelane doctor        # in a repo: Python, .hivelane.toml, hooks, daemon, relay
```

## Uninstall

Stop the daemon and remove the login item before removing the package:

```bash
hivelane down
hivelane service uninstall          # only if you ran `hivelane service install`
hivelane hook uninstall claude-code # in each repo you set up (add --global if you used it)
pipx uninstall hivelane             # or: uv tool uninstall / brew uninstall / scoop uninstall
```

Personal settings, daemon state and logs live in `~/.hivelane` (or
`HIVELANE_HOME`); delete it to remove every trace. Each repo's committed
`.hivelane.toml` belongs to the team, so leave it unless the whole team is
leaving the room (`hivelane leave --forget`).
