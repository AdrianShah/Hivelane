# Deploying and releasing

Maintainer reference for the CI/CD pipeline. To run your own relay, see [self-hosting.md](self-hosting.md).

## Environments

| Environment | Deploys from | Gate |
|-------------|--------------|------|
| `Staging` | pushes to `main` | CI must pass |
| `Production` | `v*` tags | CI must pass; tagging is the deliberate step |

`.github/workflows/deploy.yml` runs when `ci.yml` completes successfully for a
push to `main` or a `v*` tag, so nothing deploys on code that has not passed
tests and the suite is not run a second time. After POSTing the deploy hook it
waits until the relay's `/readyz` reports the deployed commit (`DEPLOY_HOOK_SENDS_REF=true`
as a repository variable makes Render build that exact commit). Required-reviewer gates need a paid GitHub plan on private
repos, so the tag stands in for approval, cut a release with `git tag v1.2.3 &&
git push origin v1.2.3`.

Deploys are host-agnostic: each environment supplies a deploy-hook URL as a
secret and `scripts/deploy_hook.sh` POSTs it (Render, Cloudflare Pages and
Netlify all offer these). Steps skip with a notice when unconfigured.

| Scope | Name | Kind | Purpose |
|-------|------|------|---------|
| Environment secret | `RELAY_DEPLOY_HOOK_URL` | secret | Triggers the relay deploy |
| Environment secret | `DASHBOARD_DEPLOY_HOOK_URL` | secret | Triggers the dashboard deploy |
| Environment variable | `RELAY_URL` | variable | Base URL polled at `/readyz` after deploy |

## Releasing the CLI

The same `v*` tag also runs `.github/workflows/release.yml`, independently of
the deploy. It checks that the tag matches `hivelane/__init__.py`'s
`__version__` (the single source of the version), builds the sdist and wheel,
runs the tests, smoke-installs the wheel on Linux, macOS and Windows, then:

1. publishes to PyPI through trusted publishing (no API token);
2. creates the GitHub release with the artifacts and `SHA256SUMS`;
3. bumps the Homebrew tap (stable releases only).

So a release is: bump `__version__`, merge, `git tag vX.Y.Z && git push origin
vX.Y.Z`. Run the workflow by hand (`workflow_dispatch`) for a dry run that
builds and smoke-tests without publishing.

| Where | Name | Purpose |
|-------|------|---------|
| GitHub environment | `pypi` | Trusted-publishing gate. Register the publisher on PyPI: owner `AdrianShah`, repo `Hivelane`, workflow `release.yml`, environment `pypi`. |
| Repository secret | `HOMEBREW_TAP_TOKEN` | Optional. Push access to `AdrianShah/homebrew-hivelane`; without it the tap bump is skipped. See [packaging/homebrew](../packaging/homebrew/README.md). |

The Scoop bucket follows PyPI on its own (`checkver`/`autoupdate`), and the npm
launcher always runs the latest release; see [packaging/](../packaging).
