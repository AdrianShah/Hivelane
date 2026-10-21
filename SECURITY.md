# Security policy

Hivelane sits in the path of every edit an AI coding agent makes, and the relay
carries prompts, file paths and diffs between teammates. We treat security
reports as the highest-priority work in the project.

The threat model, what the relay can and cannot see, and how to harden a
deployment are in [docs/security.md](docs/security.md). This file covers how
to report a problem.

## Supported versions

Hivelane is pre-1.0. Fixes land on `main` and ship in the next release; older
releases are not patched.

| Component | Supported |
|-----------|-----------|
| `hivelane` CLI and daemon (PyPI) | The latest release |
| Relay (`server/`, the Docker image, the Helm chart) | The latest release and `main` |
| Dashboard (`frontend/`) | `main` (deployed continuously) |
| VS Code / Cursor extension (`editors/vscode/`) | The latest published version |

If you run a self-hosted relay, upgrading to the latest release is the fix for
any advisory we publish.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security
problem.**

The repository is private for now, so GitHub's private vulnerability reporting
is not available to you yet. Report it by email instead:

1. Email the maintainer at the address on their GitHub profile
   ([github.com/AdrianShah](https://github.com/AdrianShah)). Put "Hivelane
   security" in the subject.
2. Describe the problem, the affected component and version (`hivelane
   --version`, the relay's `/readyz` output, or a commit SHA), and the steps
   to reproduce it. A proof of concept helps, but is not required.
3. Tell us how you would like to be credited, if at all.

Once the repository is public you can also report through GitHub Security
Advisories: the repository's **Security** tab, then **Report a vulnerability**
(<https://github.com/AdrianShah/Hivelane/security/advisories/new>). Only the
maintainers and you can see such a report.

If you cannot find the address, open a public issue (once the repository is
public) that says only that you have a security report and need a private
contact. Leave out every detail.

## What to expect

| Step | Target |
|------|--------|
| Acknowledge the report | within 3 business days |
| Confirm or rule out the issue, with a severity | within 7 days |
| Fix released: critical or high severity | within 30 days |
| Fix released: medium or low severity | in the next regular release, at most 90 days |

We keep you updated as we go and agree a disclosure date with you. By default
we publish the advisory, with a CVE where one applies, once a fixed release is
out, and no later than 90 days after your report. If an issue is being
exploited in the wild, we may move faster and will tell you first.

We will not take legal action against anyone who reports in good faith, makes
a reasonable effort to avoid privacy violations, data destruction and service
disruption, and only tests against their own installations or rooms.

## Scope

In scope:

- **The relay** (`server/`, the Docker image and the Helm chart): the HTTP and
  WebSocket endpoints, room isolation, join approval and owner tokens, payload
  limits, rate limiting, CORS and security headers, and the room store.
- **The daemon** (`hivelane serve` / `hivelane up`), which listens on
  `127.0.0.1` only (port 4600 by default): its local HTTP API, the local auth
  token, and protections against requests from web pages in the user's browser
  (cross-origin requests, DNS rebinding).
- **The agent hooks** (`hivelane hook <agent>` and the adapters in
  `hivelane/adapters/`): parsing of agent payloads, the hook configuration files
  that `hivelane up` writes, secret redaction before content leaves the machine,
  and anything that could make a hook block, approve or run something it should
  not.
- **End-to-end encryption mode**: any way for the relay, or anyone without the
  room key, to read protected content.
- **The VS Code / Cursor extension** (`editors/vscode/`) and **the dashboard**
  (`frontend/`), for example script injection through content a room member
  sends.
- The install scripts (`scripts/install.sh`, `scripts/install.ps1`) and the
  release pipeline.

Out of scope:

- **Holding a room code.** A room code (or share link) is a bearer secret: by
  design, anyone who has it can join the room unless join approval is turned
  on. Ways around join approval, or ways to guess codes faster than the rate
  limits allow, are in scope.
- **Behaviour the docs already call out as a trade-off**, such as
  `TRUST_PROXY_HEADER=true` without a proxy in front, or hooks failing open when
  the daemon is down.
- **Exposing the daemon port yourself**, for example by tunnelling 4600. The
  docs tell you not to.
- **Vulnerabilities in dependencies** with no demonstrated impact on Hivelane.
  Report those upstream; Dependabot and the weekly audit workflow track them
  here.
- Denial of service through sheer traffic volume, and social engineering.

If you are unsure whether something is in scope, report it anyway.
