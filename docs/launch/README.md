# Phase 5 launch kit

Everything needed to launch Hivelane publicly: the checklist, the prerequisites
a human has to finish first, launch copy for each channel, three blog drafts,
distribution targets and a hackathon playbook. Nothing in this directory has
been posted, submitted or sent anywhere.

Positioning, used across every file here:

> Other tools give you a new IDE. Hivelane is a 10-second add-on that stops
> your agents from clobbering each other, in whatever editor you already use.

"10 seconds" is the `hivelane up` step in a repo that already has a relay. It
is not a measured number yet; see [Success metrics](#success-metrics) for the
60-second time-to-first-room target and how to measure it.

## Files

| File | What it is |
|------|------------|
| [demo-script.md](demo-script.md) | 45-second demo video: shot list, commands, on-screen text, reproducible setup, and a 10-second README GIF cut |
| [show-hn.md](show-hn.md) | Show HN title options and post body |
| [product-hunt.md](product-hunt.md) | Tagline, description, first comment, gallery captions |
| [social.md](social.md) | X thread, LinkedIn post, Reddit posts for r/ClaudeAI and r/cursor |
| [blog/why-file-watchers-cant-prevent-agent-conflicts.md](blog/why-file-watchers-cant-prevent-agent-conflicts.md) | Blog draft 1, with [assets/watcher-vs-hook.svg](assets/watcher-vs-hook.svg) |
| [blog/hooks-for-every-coding-agent.md](blog/hooks-for-every-coding-agent.md) | Blog draft 2: how each adapter integrates |
| [blog/how-hivelane-decides.md](blog/how-hivelane-decides.md) | Blog draft 3: functions, lines, branches, plans, and the benchmark |
| [integrations.md](integrations.md) | Distribution targets with submission steps and cited sources |
| [hackathons.md](hackathons.md) | Free-relay playbook and a printable one-page sheet |
| [pricing-draft.md](pricing-draft.md) | DRAFT pricing model with open questions; needs a decision |

## Status of launch claims

Copy in this directory only states what is true at commit `28594f8`
(2026-10-06). Everything below marked **pending** is written conditionally in
the copy, and each one must be confirmed, or the sentence removed, before
anything is posted. Availability checks were run on 2026-10-06; the working
tree was re-checked for in-progress Phase 5 code on 2026-10-09.

| Claim | Status | Evidence / what is missing |
|-------|--------|----------------------------|
| Hooks that can block an edit for Claude Code, Codex CLI, Cursor (agent edits) and Gemini CLI | True | `hivelane/adapters/`, `docs/faq.md` |
| Fallbacks: `hivelane mcp`, `hivelane run aider` / `hivelane watch` (observe-only), `hivelane githook` | True | `docs/cli.md` |
| Editor extension for VS Code, Cursor and Windsurf (warns humans) | Code true; **pending** on any marketplace | `editors/vscode/`; Open VSX returned 404 for `hivelane/hivelane` |
| Function- and line-level decisions, branch and plan warnings | True | `docs/conflict-detection.md` |
| False-positive blocks 10 to 0 on the decision benchmark | True (31 synthetic two-agent sessions, 41 hand-labelled checks; not recorded from real use) | `docs/conflict-detection.md` |
| Hook p95 117 ms on Windows | True for `scripts/bench_hook.py` against a local stub daemon (hook start plus loopback round trip; relay round trip not included) | commit `430e613`; `docs/security.md` says "well under 150 ms p95" |
| End-to-end encrypted rooms, secret redaction, owner approval | True | `docs/security.md` |
| Self-hosting: `hivelane relay`, Docker image with dashboard, Compose + Caddy, Fly, Render, Railway, Helm | Code and docs true | `docs/self-hosting.md` |
| `pipx install hivelane` works | **Pending** | `https://pypi.org/pypi/hivelane/json` returned 404: no release yet |
| `ghcr.io/adrianshah/hivelane-relay` pullable by anyone | **Pending** | anonymous pull was denied |
| Public GitHub repository | **Pending** | `https://github.com/AdrianShah/Hivelane` returned 404 to an anonymous request, so it is private or not pushed |
| Hosted relay (dashboard: https://hivelane.dev/) | **Pending** | `hivelane.dev` did not resolve; the dashboard and docs now publish to GitHub Pages |
| Homebrew tap, Scoop bucket, `npx hivelane-cli` | **Pending** | `docs/install.md` says "once published"; npm returned 404 for `hivelane-cli` |
| Docs site | **Pending** | deploy job commented out in `.github/workflows/docs.yml` |
| Dashboard: live lanes, decision chips with scope and Useful/Noise votes, conflict banner, conflict history, detection-quality analytics | True | `frontend/src/components/Lane.tsx`, `frontend/src/App.tsx` |
| Dashboard: collision timeline and heat map | **In progress, uncommitted** | not in `28594f8`. On 2026-10-09 the working tree has `frontend/src/components/TimelineView.tsx`, `HeatMapView.tsx` and Timeline / Heat map sidebar entries, but `App.tsx` did not render them yet. Use either word only after they merge; until then copy says "conflict history" |
| Phase 5 features being built in parallel: per-file wait queues (`hivelane wait`), edit suggestions, opt-in room history (replay), owner webhooks to Slack/Discord, new dashboard views | **In progress, uncommitted** on 2026-10-09 (`server/file_queue.py`, `server/history.py`, `server/suggest.py`, `server/webhooks.py`, `frontend/src/dashboard/`, new components) | Not mentioned in any launch copy. Add them only after they merge, and re-check every limitation in [show-hn.md](show-hn.md) against them |
| Exact wording of a block message | **Will change if the wait queue merges** | At `28594f8` a Claude Code block reads `Hivelane: <reason> — wait, or pick another file (their task: "...")`. The in-progress `ack_message` in `hivelane/adapters/base.py` replaces the middle with the queue position, a suggested alternative file, and a hint to run `hivelane wait <file>`. Every quoted message in this directory uses the committed wording: re-capture it from the build you record with and update the quotes in [demo-script.md](demo-script.md), [show-hn.md](show-hn.md), [social.md](social.md), [product-hunt.md](product-hunt.md) and the blog drafts |
| Opt-in usage telemetry | **Does not exist** | nothing in `hivelane/` or `server/` reports usage |
| Claude Code plugin, MCP Registry entry, Gemini CLI extension | **Do not exist** | packaging work listed in [integrations.md](integrations.md) |

## Prerequisites a human must complete first

These block the launch. Do them in this order; each has a check that proves
it is done. Owners are TBD.

| # | Prerequisite | Owner | Done when |
|---|--------------|-------|-----------|
| P0 | **Make the repository public.** Install scripts (`raw.githubusercontent.com/.../install.sh`), every docs link and every launch post point at `github.com/AdrianShah/Hivelane`. Add topics (`ai-agents`, `claude-code`, `coding-agents`, `mcp`, `developer-tools`) and a social preview image (Settings > General). | TBD | The repo URL loads in a logged-out browser. |
| P1 | **PyPI trusted publisher and first release.** On PyPI, add a *pending* trusted publisher for project `hivelane`: owner `AdrianShah`, repository `Hivelane`, workflow `release.yml`, environment `pypi`. Create the `pypi` environment in GitHub (Settings > Environments). Run `release.yml` by hand (`workflow_dispatch`) as a dry run. Then, with `__version__ = "0.1.0"` in `hivelane/__init__.py`: `git tag v0.1.0 && git push origin v0.1.0`. | TBD | `pipx install hivelane && hivelane --version` prints `hivelane 0.1.0` on a clean machine; the GitHub release has wheels and `SHA256SUMS`. |
| P2 | **Hosted relay and domain.** Register or point `hivelane.dev`. Deploy the relay image (Fly: `deploy/fly/`, one always-on machine, 1 GB volume). Set `HIVELANE_ENV=production` and `CORS_ORIGINS`. The CLI already sends people to `https://hivelane.dev` (GitHub Pages) for dashboard links whenever the relay is not local (`DEFAULT_DASHBOARD` in `hivelane/cli.py`), so the dashboard must be live there, or that default must change, before launch. Decide the hosted relay URL; the CLI's default relay is still `ws://localhost:8000/ws`, so launch copy has to show `--relay` until a code change makes the hosted relay the default. Decide whether `GEMINI_API_KEY` is set on the hosted relay (if it is, prompts and diffs of cleartext rooms go to Google's API; say so in a privacy note). **Capacity:** `MAX_ROOMS = 100` live rooms per process and `MAX_MEMBERS = 6` per room are hard-coded in `server/room_manager.py`; a launch-day spike can hit 100 live rooms and get `503`. Raise the cap (code change) or scale out with Redis per `docs/self-hosting.md` before posting. | TBD | `curl https://<relay>/readyz` is ready; `hivelane up --relay wss://<relay>/ws` in a fresh repo prints a dashboard link that opens. |
| P3 | **Public container image.** `image.yml` already pushes `ghcr.io/adrianshah/hivelane-relay` (`edge` from `main`, versions and `latest` from `v*` tags). GHCR packages start private: Package settings > Change visibility > Public, and link the package to the repo. `latest` needs the P1 tag. | TBD | `docker logout ghcr.io && docker pull ghcr.io/adrianshah/hivelane-relay:latest` succeeds. |
| P4 | **Editor extension published** to the VS Code Marketplace and Open VSX (Cursor and Windsurf install from Open VSX). Before publishing, fix in `editors/vscode/package.json`: `publisher` is the placeholder `hivelane`; `repository.url` is `https://github.com/hivelane/hivelane` (wrong); add a 128x128 PNG `icon`. `editors/vscode/LICENSE` is an abbreviated MIT text: replace it with the full MIT text or relicense to Apache-2.0 to match the repo. Steps: [integrations.md](integrations.md#open-vsx-and-the-vs-code-marketplace). | TBD | The extension installs by name in VS Code and in Cursor. |
| P5 | **Docs site on GitHub Pages.** Follow the four steps in the comment in `.github/workflows/docs.yml`: set Pages source to GitHub Actions, choose the host (`SITE` and `BASE` in `docs-site/sources.mjs` now point at `https://hivelane.dev/docs`, published by `.github/workflows/pages.yml`), add the `push` trigger, uncomment the upload step and the deploy job. | TBD | The docs URL loads and its "Edit page" links point at `main`. |
| P6 | **Conduct email.** `CODE_OF_CONDUCT.md` sends reports to the maintainer's email on their GitHub profile (it used to name `conduct@hivelane.dev`, a domain that will not be bought). Create the mailbox or forwarding (needs P2's domain), name at least two people who read it, or change the address. | TBD | A test email to the address reaches a named person. |
| P7 | **Vulnerability reporting.** `SECURITY.md` and the issue chooser link to `/security/advisories/new`. Turn on private vulnerability reporting (repo Settings, security section, "Private vulnerability reporting"; it only becomes available once P0 makes the repo public), name a triage owner, and check the SECURITY.md targets (acknowledge within 3 business days) are staffable. | TBD | A test report from a second account arrives as a draft advisory. |
| P8 | **Demo assets.** Record the 45-second video and the 10-second GIF from [demo-script.md](demo-script.md), after P1 so the end card's install command is real. | TBD | Files uploaded; GIF embedded at the top of the README. |
| P9 | Optional for launch day: Homebrew tap (`AdrianShah/homebrew-hivelane` plus the `HOMEBREW_TAP_TOKEN` secret), Scoop bucket (`AdrianShah/scoop-hivelane`), `npm publish` of `packaging/npm` as `hivelane-cli`. Until they exist, launch copy lists only pipx and uv. | TBD | `brew install adrianshah/hivelane/hivelane`, `scoop install hivelane/hivelane`, `npx hivelane-cli --version` work. |

## Launch checklist

Day 0 is the Show HN post. Every item needs P0 to P3; posts that mention the
extension also need P4.

| When | Item | Owner | Needs | Source |
|------|------|-------|-------|--------|
| D-14 | Confirm every **pending** row above, or edit the copy that relies on it | TBD | | this file |
| D-14 | Five moderated first-run sessions on clean machines (macOS, Linux, Windows), timing install to dashboard link | TBD | P1, P2 | [metrics](#success-metrics) |
| D-10 | Record demo video and GIF; put the GIF at the top of the README | TBD | P1, P2 | [demo-script.md](demo-script.md) |
| D-10 | Publish the extension | TBD | P4 | [integrations.md](integrations.md) |
| D-7 | Publish blog draft 1 (file watchers) on the docs site or a blog | TBD | P5 | [blog/](blog/) |
| D-7 | Submit to the MCP Registry (needs the PyPI release) | TBD | P1 | [integrations.md](integrations.md#mcp-registries) |
| D-5 | Load-test the hosted relay: 100+ live rooms, 6 members each, confirm the room cap change from P2 | TBD | P2 | `docs/self-hosting.md` |
| D-3 | Dry run of every post with someone who has not seen Hivelane; fix anything unclear | TBD | | [show-hn.md](show-hn.md) |
| D-1 | Run `scripts/bench_hook.py -n 50` on macOS, Linux and Windows; update the latency number in the copy if it moved | TBD | | [metrics](#success-metrics) |
| D0 08:00 PT | Show HN post, first comment from the maker | TBD | P0 to P3, P8 | [show-hn.md](show-hn.md) |
| D0 | Stay in the HN thread for the first six hours; answer limitations questions plainly | TBD | | |
| D0 | X thread and LinkedIn post, linking the repo (not the HN thread) | TBD | | [social.md](social.md) |
| D+1 | Reddit posts, one per day, each in the community's own format and only where the rules allow it | TBD | | [social.md](social.md) |
| D+2 | Blog draft 2 (hooks for every agent) | TBD | P5 | [blog/](blog/) |
| D+3 12:01 PT | Product Hunt launch | TBD | P8 | [product-hunt.md](product-hunt.md) |
| D+3 | Submit to awesome lists and the Claude Code plugin directory (if the plugin exists by then) | TBD | | [integrations.md](integrations.md) |
| D+7 | Blog draft 3 (how Hivelane decides), with any new benchmark numbers | TBD | P5 | [blog/](blog/) |
| D+7 | First metrics review against the table below | TBD | | |
| Rolling | Hackathon relay offers | TBD | P2 | [hackathons.md](hackathons.md) |

Rules for every post: never name another company or product as a competitor
(say "multiplayer IDEs", "file watchers", "task splitting"); never ask anyone
to upvote; no emoji; every number comes from the fact sheet below.

## Fact sheet

The only numbers launch copy may use, with where each comes from.

| Fact | Value | Source |
|------|-------|--------|
| Agents whose hooks can block an edit | 4: Claude Code, Codex CLI, Cursor (agent edits), Gemini CLI | `docs/faq.md` |
| Hook latency | p95 117 ms on Windows, measured by `scripts/bench_hook.py` against a local stub daemon; was 1665 ms before the stdlib-only hook path | commits `430e613`, `9742890` |
| Longest a pre-edit check waits for the relay | 1.5 s, then the edit goes ahead | `docs/faq.md` |
| False-positive blocks, decision benchmark | 10 to 0 (block precision 0.57 to 1.00) | `docs/conflict-detection.md`; re-run with `scripts/benchmark_decisions.py` on 2026-10-06 |
| Block recall, decision benchmark | 0.59 to 0.50 (11 of 22 true blocks; the rest warn instead). `docs/conflict-detection.md` still says 0.45: it predates commit `5d97a14`, which made overlapping lines and `.hivelane.toml` block. Re-run the script before posting and use its output | `scripts/benchmark_decisions.py` |
| Benchmark size | 31 synthetic two-agent sessions (generated by `scripts/benchmark_decisions.py --generate`), 41 labelled checks. Always call it synthetic | `docs/conflict-detection.md`, `scripts/benchmark_decisions.py` |
| Failure mode | Fails open: relay or daemon down means the edit proceeds, with a notice at most every 10 minutes | `docs/faq.md` |
| Room code | 8 characters (about 40 bits), valid 7 days | `docs/security.md` |
| Room size | 6 members per room (hard-coded); each agent connection in a repo is a member | `server/room_manager.py`, `docs/protocol.md` |
| Platforms | macOS, Linux, Windows; CI on all three with Python 3.12 and 3.13 | `docs/faq.md` |
| License | Apache-2.0 (the extension carries its own license file) | `LICENSE`, `NOTICE` |

## Success metrics

Targets come from the project plan. **Opt-in telemetry does not exist**, so
nothing below can be measured from users' machines today. Each row says what
can be measured now, and what would have to be built to measure it properly.

| Metric | Target | How to measure now | Gap |
|--------|--------|--------------------|-----|
| Time to first room | < 60 s | Definition: on a clean machine with Python 3.12+ or uv, wall time from starting `pipx install hivelane` (or `uv tool install hivelane`) to `hivelane up` printing the dashboard link, in a fresh git repo against the hosted relay. Measure with a stopwatch in moderated first-run sessions (5 before launch, more after), and with a scripted timing job on clean CI runners for macOS, Linux and Windows. | The CI timing job does not exist (a small workflow). Field data needs opt-in telemetry, which does not exist. |
| Prevented collisions | >= 1 per active room per day | On the hosted relay: blocks per day from `/metrics`, `increase(hivelane_edit_checks_total{decision="block"}[1d])` summed over scopes, divided by active rooms per day, counted as distinct room codes in the relay's `"<name> joined room <code>"` log lines that day. Use the dashboard's Useful/Noise votes (`hivelane_decision_feedback_total`) to discount blocks people marked as noise. | Per-room attribution is not possible: the `edit_check` log line has no room code and metrics are aggregate by design. Adding the room code to that log line, or a daily per-room rollup, is a small change. Self-hosted relays are invisible without telemetry. |
| GitHub stars | 500 in 90 days | `gh api repos/AdrianShah/Hivelane --jq .stargazers_count`, weekly. | None, once P0 is done. |
| Weekly rooms | 150 in 90 days | On the hosted relay: distinct room codes in `joined room` log lines over 7 days. Rooms with two or more members (the only ones where a collision can happen) can be counted from the `(N in room)` part of the same line. | No room-creation counter exists (`hivelane_rooms_durable` is a gauge, and codes are dropped 30 minutes after a room empties). A `hivelane_rooms_created_total` counter is a small change. Rooms on self-hosted relays are not counted. |
| Self-hosted relays | 30 in 90 days | Not measurable directly. Proxies: GHCR download count on the package page (after P3), a pinned "Running your own relay?" discussion where people can report one, and mentions in issues. Report it as "at least N known", never as a measured count. | Counting relays needs an opt-in ping from the relay, which does not exist and needs a privacy decision first. |
| Hook latency | p95 < 150 ms | `python scripts/bench_hook.py -n 50` on macOS, Linux and Windows before each release. Last recorded: 117 ms p95 on Windows (commit `430e613`). | Not in CI; a non-blocking benchmark job per OS would catch regressions. |
