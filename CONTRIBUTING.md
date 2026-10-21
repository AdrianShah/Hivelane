# Contributing to Hivelane

Thanks for helping. Hivelane is small enough that one person can hold most of
it in their head, and we would like to keep it that way: small changes, each
with a test, each explaining why.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Please
report security problems privately, as [SECURITY.md](SECURITY.md) describes,
not in an issue.

- [What lives where](#what-lives-where)
- [Set up a checkout](#set-up-a-checkout)
- [Run the checks](#run-the-checks)
- [Conventions](#conventions)
- [Changing the wire protocol](#changing-the-wire-protocol)
- [Writing an adapter](#writing-an-adapter)
- [Docs](#docs)
- [Commits and pull requests](#commits-and-pull-requests)

## What lives where

| Path | What | Language |
|------|------|----------|
| `hivelane/` | The `hivelane` CLI, the per-machine daemon, the agent hook fast path and adapters | Python 3.12+ |
| `server/` | The relay: rooms, pre-edit decisions, broadcasts | Python 3.12+ (FastAPI) |
| `frontend/` | The dashboard | TypeScript (React + Vite) |
| `editors/vscode/` | The VS Code / Cursor / Windsurf extension | TypeScript |
| `protocol/` | `schema.json`, the language-neutral wire contract, and its generator | JSON Schema |
| `tests/e2e/` | A real relay and two daemons in one process | Python |
| `docs/` | The Markdown docs, also published by `docs-site/` | Markdown |
| `docs-site/` | The docs website (Astro Starlight) | Node |
| `deploy/`, `packaging/` | Relay deployment configs; Homebrew, Scoop and npm packaging | |

## Set up a checkout

You need Python 3.12 or newer, Git, and Node.js (20 for the dashboard and the
extension, 22.12 or newer for the docs site).

```bash
git clone https://github.com/AdrianShah/Hivelane.git
cd Hivelane
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r server/requirements-dev.txt -e ".[dev]"
```

That is the same install CI runs: the relay's pinned dependencies, pytest, and
the CLI in editable mode, so `hivelane` on your PATH runs your working tree.
Add `-e ".[dev,symbols]"` to work on tree-sitter symbol extraction.

`./setup.sh` (or `setup.ps1` on Windows) creates the venv and installs the CLI
in one step, and also points `core.hooksPath` at `.githooks/`, whose
pre-commit hook refreshes the optional code graph in `graphify-out/`.

To run the whole stack locally (relay, dashboard, CLI against a test repo),
follow [Develop locally](README.md#develop-locally) in the README.

## Run the checks

Run what CI runs for the parts you touched before you open a pull request.

| Check | Command | When |
|-------|---------|------|
| Python tests | `pytest -q` | Any Python change. About 800 tests across `hivelane/tests`, `server/tests` and `tests/e2e`; CI runs them on Linux, macOS and Windows with Python 3.12 and 3.13. |
| Cross-cwd smoke test | `./scripts/smoke.sh` | Daemon, hook or relay changes. Needs bash, and `hivelane` and `uvicorn` on PATH (the venv above). CI runs it on Ubuntu. |
| Schema is current | `python protocol/generate.py --check` | Any change to `hivelane/protocol.py` (the contract tests also check it). |
| Dashboard | `cd frontend && npm ci && npm run lint && npm run build` | Any change under `frontend/`. |
| Extension | `cd editors/vscode && npm ci && npm test` | Any change under `editors/vscode/`. Runs against a mock daemon and a mock relay. |
| Docs site | `cd docs-site && npm ci && npm run build` | Changes to `docs/` or `docs-site/`. |
| Hook latency | `python scripts/bench_hook.py` | Changes on the hook path; compare against `main`. |
| Relay image | `docker build -t hivelane-relay .` | Changes to `Dockerfile` or `server/requirements.txt`. |

On Windows, run Python tools through the venv explicitly if your shell has not
activated it: `.venv\Scripts\python -m pytest -q`.

## Conventions

These are the rules the code already follows. Reviews will ask about them.

### Comments say why

Comments and docstrings explain the reason a thing is the way it is, the
constraint it protects or the bug it prevents, not what the next line does. If
you find yourself writing "do not change this", write down what breaks.

### Python style

- Every module starts with `from __future__ import annotations` and is fully
  type-hinted.
- Prefer the standard library. A new runtime dependency of the `hivelane`
  package needs a good reason: it is installed on every contributor's machine.
- Never let Hivelane's own failure stop an agent. Hooks and adapters fail
  open: any error means exit 0 and no decision, and adapter `normalize` and
  `render` never raise.

### Dual imports in `server/`

The relay runs two ways: as the `server` package from the repo root
(`uvicorn server.main:app`, the tests, the Docker image) and as a flat module
tree with `server/` as the working directory (`uvicorn main:app`, which hosts
like Render use). Every module in `server/` therefore imports its siblings
twice:

```python
try:
    from . import config
    from .models import ActivityMessage
except ImportError:  # Render runs with rootDir=server as a flat module tree
    import config  # type: ignore[no-redef]
    from models import ActivityMessage
```

A new `server/` module, or a new sibling import, has to follow the same
pattern. `server/` never imports `hivelane`, and the client side of `hivelane`
(the hooks, the daemon, the protocol models) never imports `server`: the
relay is deployed on its own. The one exception is `hivelane relay`, which
imports the relay lazily, inside the command, to run one on your machine.

### The hook fast path stays stdlib-only

Agents start `hivelane hook <agent>` as a fresh process before and after every
edit, so every import on that path is paid around every file write. The path
is `hivelane/cli.py` (module level only), `hivelane/hook_fast.py`,
`hivelane/hook_json.py`, `hivelane/adapters/__init__.py`,
`hivelane/adapters/base.py` (its top half) and the one adapter being run.

- No third-party imports there: no pydantic, fastapi, websockets, httpx.
- Not even the slower parts of the standard library: `json`, `re`, `enum`,
  `typing`, `argparse`, `pathlib`, `socket`, `urllib` and friends. Use
  `hivelane.hook_json` for JSON, `open()` and `os.path` for files, and
  `TYPE_CHECKING = False` / `if TYPE_CHECKING:` for type-only imports.
- Import heavier modules inside the function that needs them, as the install
  helpers in `hivelane/adapters/base.py` do.

`hivelane/tests/test_hook_fast.py` and `hivelane/tests/test_adapter_hook_path.py`
run the hook in a subprocess and fail if any forbidden module shows up in
`sys.modules`; the `HEAVY` and `SLOW_STDLIB` sets at the top of
`test_hook_fast.py` are the budget. `scripts/bench_hook.py` measures the wall
time; a change that makes it noticeably slower needs a reason in the PR.

### Tests

Every behaviour change comes with a test, and every bug fix with a test that
failed before it. Tests live next to the code they cover (`hivelane/tests/`,
`server/tests/`), and `tests/e2e/` covers what only a real relay with two
daemons can show.

## Changing the wire protocol

The relay and the daemon each keep their own copy of the wire models, because
the CLI package must not import the relay:

- `server/models.py`, the relay's copy;
- `hivelane/protocol.py`, the client's copy;
- `frontend/src/types.ts`, the dashboard's copy;
- `protocol/schema.json`, generated from `hivelane/protocol.py`.

Change all of them together, then regenerate the schema:

```bash
python protocol/generate.py          # rewrite protocol/schema.json
python protocol/generate.py --check  # what the tests check
```

`server/tests/test_protocol_contract.py` fails when the two Python copies
produce different JSON schemas, when `schema.json` is stale, or when
`types.ts` misses a field. Defaults that differ on purpose are listed there,
each with its reason.

Versioning rules, spelled out in [docs/protocol.md](docs/protocol.md#versioning):
a new **optional** field with a default, or a new message type that older
peers can ignore, does **not** change `PROTOCOL_VERSION`. Anything an older
peer cannot satisfy does, in both `server/models.py` and
`hivelane/protocol.py` at once.

## Writing an adapter

An adapter teaches Hivelane one coding agent's hook format: it turns the
agent's native hook payload into Hivelane events and renders the daemon's
allow / warn / block answer the way that agent expects. It is one stdlib-only
module in `hivelane/adapters/`, a registry entry, and a set of golden fixtures
that the conformance suite runs against every adapter.

[docs/adapters.md](docs/adapters.md) has the contract, a worked example and
the fixture workflow. Issues labelled
[`good first adapter`](https://github.com/AdrianShah/Hivelane/labels/good%20first%20adapter)
are agents people have asked for; the **Adapter request** issue form collects
what an adapter author needs to know about an agent's hooks.

## Docs

The Markdown under `docs/` is the source for both GitHub and the docs site.
Edit it there. `docs-site/scripts/sync-docs.mjs` copies it into the site at
build time, adds the frontmatter and rewrites links, so write ordinary
relative links (`[CLI](cli.md)`, `[the Dockerfile](../Dockerfile)`) and they
work in both places. A new page needs a line in `docs-site/sources.mjs` to
get a place in the sidebar; until then it shows up under "More".

```bash
cd docs-site
npm ci
npm run dev        # http://localhost:4321/docs/
```

`npm run dev` syncs once at start. Re-run `npm run sync` in another terminal
after you edit a file under `docs/`.

## Commits and pull requests

Commit messages follow the existing history (`git log`):

- A subject line in the imperative or as a short summary, sentence case, no
  trailing period, around 50 to 70 characters: `Fix path keying, intent expiry
  and outbox starvation`, `Add protocol versioning, structured logs and a relay
  image`. No `feat:`/`fix:` prefixes. Roadmap work is prefixed with its phase:
  `Phase 3: relay decision engine, plans and decision feedback`.
- A body, wrapped at about 72 columns, that says why, and for larger changes a
  bullet per area, led by the file or component: `- hivelane/symbols.py:
  function/class extraction ...`.
- Credit co-authors, human or AI, with a `Co-Authored-By:` trailer.

Pull requests:

- Keep each one to one change. A refactor and the feature that needed it are
  easier to review as two.
- Fill in the template: what changed, why, and how you tested it.
- CI must pass. If you touched the hook path, include the `bench_hook.py`
  numbers before and after.
- Update the docs in the same pull request as the behaviour they describe.

Contributions are licensed under the [Apache License 2.0](LICENSE), the
project's license, as section 5 of the license describes. There is no CLA to
sign.
