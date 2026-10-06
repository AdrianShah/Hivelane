<!--
Thanks for contributing. Keep the pull request to one change; CONTRIBUTING.md
lists the checks each part of the repo needs. Security fixes: please
coordinate through a private advisory first (see SECURITY.md).
-->

## What and why

<!-- What this changes and the problem it solves. Link the issue: "Fixes #123". -->

## How it was tested

<!-- The commands you ran and what they showed. New behaviour needs a new test. -->

- [ ] `pytest -q`
- [ ] `./scripts/smoke.sh` (daemon, hook or relay changes)
- [ ] `npm run lint && npm run build` in `frontend/` (dashboard changes)
- [ ] `npm test` in `editors/vscode/` (extension changes)
- [ ] `npm run build` in `docs-site/` (changes to `docs/` or `docs-site/`)

## Checklist

- [ ] The docs under `docs/` (and the README, if affected) describe the new behaviour.
- [ ] Wire protocol changes are made in `server/models.py`, `synergo/protocol.py` and `frontend/src/types.ts`, and `python protocol/generate.py` has been re-run. New fields are optional with defaults, or `PROTOCOL_VERSION` is bumped on both sides.
- [ ] Hook-path changes add no third-party or slow standard-library imports, and include `scripts/bench_hook.py` numbers before and after if timing could change.
- [ ] New imports in `server/` follow the dual-import pattern.
- [ ] No secrets, room codes or private prompts in code, tests, fixtures or screenshots.
