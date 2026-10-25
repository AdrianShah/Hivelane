<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/mascot-clear-dark.svg">
    <img src="docs/brand/mascot-clear.svg" alt="Hivelane logo" width="120">
  </picture>
</p>

<h1 align="center">Hivelane</h1>

<p align="center">
  <strong>Live coordination for parallel AI coding agents.</strong>
</p>

<p align="center">
  <a href="https://github.com/AdrianShah/Hivelane/actions/workflows/ci.yml"><img src="https://github.com/AdrianShah/Hivelane/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+">
</p>

<p align="center">
  <a href="https://adrianshah.github.io/Hivelane/docs/">Documentation</a> ·
  <a href="https://adrianshah.github.io/Hivelane/">Dashboard</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

Hivelane streams each agent's intent, prompts, AI-generated captions and file
diffs into a shared real-time board, and checks with the relay before every
edit whether another agent is already working in that file.

When several developers or AI agents work in one repository at once, collisions
usually surface at pull-request time. Hivelane catches them before the edit
lands.

## Features

- **Pre-edit checks.** Agents ask the relay before writing a file and are warned or blocked when a teammate is already editing it.
- **Per-agent lanes.** Declared intent, prompts, captions and expandable diffs for every agent in the room.
- **Conflict radar.** Overlapping edits within a short window are flagged as they happen.
- **Shared rooms.** One room per repository, recorded in a committed config file. No accounts required.
- **Agent support.** Works with Claude Code, Cursor, Codex, Gemini CLI and others through hooks.
- **Self-hostable.** The relay ships as a Docker image with ready-made Compose, Fly.io and Render configurations.

## How it works

```
┌─────────────┐  intent / edit_check  ┌──────────────────┐     broadcast      ┌──────────────┐
│    Daemon   │ ────────────────────► │  Hivelane relay  │ ─────────────────► │   Dashboard  │
│ (per laptop)│ ◄──────────────────── │  FastAPI + rooms │   WebSocket        │  React / Vite│
└─────────────┘  allow / warn / block └────────┬─────────┘                    └──────────────┘
       ▲                                       │
       │ hook call, before each edit           │ captions / conflict classification
┌──────┴──────┐                                ▼
│ Agent hooks │                        (optional AI layer)
└─────────────┘
```

| Component | Description |
|-----------|-------------|
| [`hivelane/`](hivelane/) | The `hivelane` CLI. Installs agent hooks and runs one background daemon per machine that answers hooks on `127.0.0.1:4600`. |
| [`server/`](server/) | The relay. Manages rooms, broadcasts activity, answers pre-edit checks and optionally runs AI captioning and conflict classification. |
| [`frontend/`](frontend/) | The spectator dashboard. Live lanes, conflict alerts and analytics, at `<dashboard>/r/<ROOM>`. |
| [`editors/vscode/`](editors/vscode/) | VS Code, Cursor and Windsurf extension with status-bar info, file badges and pre-edit warnings. |
| `.hivelane.toml` | Per-repository config, committed to git, that records the room and relay so teammates join automatically. |

## Getting started

### Requirements

- Python 3.12 or later
- A Hivelane relay: your team's, a [self-hosted](docs/self-hosting.md) one, or a [local](#development) one for development

### Install

```bash
pipx install git+https://github.com/AdrianShah/Hivelane
```

Homebrew, Scoop, npm and install-script options are covered in
[docs/install.md](docs/install.md). Install the CLI so that `hivelane` is on
your `PATH`, since agent hooks call it before every edit.

### Set up a repository

The first teammate creates the room and commits its config:

```bash
cd your-repo
hivelane up --relay wss://relay.example.com/ws
git add .hivelane.toml
git commit -m "Add Hivelane room"
```

Everyone else pulls the repository and runs:

```bash
hivelane up
```

`hivelane up` installs the agent hooks, joins the room recorded in
`.hivelane.toml` and starts the background daemon. Use `hivelane open` to view
the dashboard, `hivelane status` to inspect the daemon and `hivelane doctor` to
diagnose problems.

> [!WARNING]
> Do not expose the daemon port (4600) publicly or through a tunnel. It is
> intended to serve hooks on loopback only.

## CLI reference

```text
hivelane up          Set up this repo: hooks, room and background daemon
hivelane down        Stop the background daemon (hooks stay installed)
hivelane leave       Disconnect this repo from its room
hivelane status      Show the daemon and its connected repos
hivelane doctor      Check the setup and suggest fixes
hivelane open        Open this repo's room in the dashboard
hivelane serve       Run the multi-repo daemon in the foreground
hivelane service     Install or uninstall the daemon as a login service
hivelane join ROOM   Run a single-room daemon in the foreground
hivelane hook ...    Agent hook entry point
hivelane --version   Print the version
```

See [docs/cli.md](docs/cli.md) for every flag and configuration option.

## Documentation

| Topic | Link |
|-------|------|
| Installation | [docs/install.md](docs/install.md) |
| CLI and configuration files | [docs/cli.md](docs/cli.md) |
| Environment variables | [docs/configuration.md](docs/configuration.md) |
| Wire protocol (HTTP and WebSocket) | [docs/protocol.md](docs/protocol.md) |
| Conflict detection | [docs/conflict-detection.md](docs/conflict-detection.md) |
| Agent adapters | [docs/adapters.md](docs/adapters.md) |
| Self-hosting a relay | [docs/self-hosting.md](docs/self-hosting.md) |
| Security model | [docs/security.md](docs/security.md) |
| Deploying and releasing | [docs/releasing.md](docs/releasing.md) |
| FAQ | [docs/faq.md](docs/faq.md) |

## Development

Prerequisites: Python 3.12+, Node.js 18+ and, optionally, a
[Gemini API key](https://aistudio.google.com/apikey) for AI captions. Without
a key, Hivelane falls back to heuristic conflict labels.

`./setup.sh` (or `setup.ps1` on Windows) creates a virtual environment,
installs the CLI in editable mode and enables the repository's git hooks.

**Relay**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt
cp .env.example .env               # optional: set GEMINI_API_KEY
uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
```

**Dashboard**

```bash
cd frontend
npm install
npm run dev                        # http://localhost:5173
```

**CLI**

```bash
pip install -e ".[dev]"
cd path/to/a/test-repo
hivelane up                        # defaults to the local relay
```

**Tests**

```bash
pip install -r server/requirements-dev.txt
pytest -q
```

### Project layout

```text
hivelane/      CLI: hooks, multi-repo daemon, doctor
server/        FastAPI relay
frontend/      Dashboard (React + Vite)
editors/       Editor extensions
protocol/      Wire protocol schema and test vectors
deploy/        Relay deployment configs (Compose + Caddy, Fly.io, Render)
packaging/     Homebrew, Scoop and npm distribution
docs/          User and operator documentation
tests/         Unit and end-to-end tests
```

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request, and follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please do not report vulnerabilities in public issues. See
[SECURITY.md](SECURITY.md) for how to report them privately.

## License

Hivelane is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attributions.
