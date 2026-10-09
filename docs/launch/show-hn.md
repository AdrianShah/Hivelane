# Show HN

Show HN is for things people can try right away ("ideally without barriers
such as signups or emails", per the
[Show HN guidelines](https://news.ycombinator.com/showhn.html), read
2026-10-06). So post only once P0 to P3 in [README.md](README.md) are done:
public repo, PyPI release, public relay image, and a hosted relay or a clear
self-host path. Never ask anyone to upvote or comment.

Submit the GitHub repo as the URL and put the body below in the text field.
If the text does not appear above the thread, post it as the first comment
straight away.

## Title options (80 characters max)

| Chars | Title |
|------:|-------|
| 77 | Show HN: Synergo – stop parallel coding agents from editing the same function |
| 69 | Show HN: Synergo – pre-edit hooks that block colliding AI agent edits |
| 77 | Show HN: Synergo – agents ask "is anyone else in this file?" before each edit |
| 77 | Show HN: Synergo – coordination for Claude Code, Codex, Cursor and Gemini CLI |
| 72 | Show HN: Synergo – keep parallel AI agents out of each other's functions |
| 66 | Show HN: Open-source pre-edit checks for parallel AI coding agents |

Recommended: the first. It names the problem and the granularity, and it does
not promise more than the tool does.

## Post body

> Synergo is an open-source (Apache-2.0) coordination layer for AI coding
> agents working in the same repo at the same time. Before an agent writes a
> file, its own hook asks a shared relay whether a teammate's agent is already
> working there, and the relay answers allow, warn or block. A blocked agent
> gets a sentence like this instead of making the edit:
>
>     Synergo: alice's claude-code is editing greet in src/hello.py (14s ago)
>     — wait, or pick another file (their task: "Add a greeting parameter to greet()")
>
> Why: with several agents in one repo (yours, or your teammates'), you find
> out about overlaps at merge time. We started as a file watcher and dropped
> it, because a watcher only sees an edit after it is written: by the time it
> flags two agents in one file, both have already changed it. A pre-edit hook
> can stop the second write.
>
> How it works:
>
> - `synergo up` in a repo installs hooks for the agents it finds (Claude
>   Code, Codex CLI, Cursor, Gemini CLI), creates a room on the relay, writes
>   it to a committed `.synergo.toml`, and starts a small daemon on
>   127.0.0.1:4600. Teammates pull and run `synergo up`; no accounts.
> - Each hook call is a short-lived process that asks the local daemon, which
>   asks the relay over a WebSocket. Measured with `scripts/bench_hook.py`
>   against a local stub daemon, the hook's p95 is 117 ms on Windows (relay
>   round trip not included). A check waits at most 1.5 s for the relay.
> - Decisions are per function and line range, not per file. The daemon
>   extracts touched symbols (Python via `ast`, other languages via
>   tree-sitter if installed, regex otherwise). Same function recently, or
>   overlapping lines: block. Different function: warn. Without symbols,
>   line ranges within 10 lines block.
>   Same code on another branch: warn "this will conflict at merge".
>   Dependency manifests and lockfiles are judged per file, since two
>   "different" dependency edits still collide. A declared plan (Claude
>   Code's todo list or plan) warns teammates before they touch files it
>   names.
> - On a benchmark of 31 synthetic two-agent sessions (41 labelled edit
>   checks, replayed against the relay's decision code), false-positive blocks went from 10 with file-level checks to 0. Block
>   recall dropped from 0.59 to 0.50; the missed cases warn instead of
>   blocking. The method and the misses are in docs/conflict-detection.md,
>   and `scripts/benchmark_decisions.py` reproduces the numbers.
> - A dashboard shows each agent's prompt, plan, diffs and decisions, and the
>   team can mark each decision useful or noise.
> - The relay is one FastAPI process you can run yourself: `synergo relay`
>   on a LAN, or a Docker image with the dashboard bundled (Compose + Caddy,
>   Fly, Render, Railway and Helm configs are in the repo).
>
> Limitations, plainly:
>
> - Blocking depends on the agent. Claude Code, Codex CLI and Gemini CLI can
>   deny an edit from a hook. In Cursor, only the agent's write and delete
>   tool calls can be denied; Tab completions and your own typing are not
>   covered. The VS Code/Cursor extension only warns humans.
> - Agents without pre-edit hooks are observe-only: `synergo run aider` and
>   `synergo watch` report edits after they happen, and the MCP server only
>   helps if the agent actually calls `check_file`. Files changed through a
>   shell command are invisible to every adapter.
> - Coordination is per checkout per machine. Several agent sessions in one
>   checkout share one identity and are not checked against each other; to
>   coordinate your own parallel sessions, give each its own checkout and
>   display name. Checkouts on different branches get "conflict at merge"
>   warnings, not blocks.
> - The relay runs as a single instance by default; rooms live in its memory.
>   Several instances need Redis plus routing by room code (automatic on
>   Fly). A room holds at most 6 members today.
> - End-to-end encryption hides content, not metadata. In an e2e room the
>   relay sees hashed paths and symbols (equal names hash equal), line
>   numbers, timing, sizes and who is connected, and never diffs or prompts.
>   Without e2e, whoever runs the relay sees prompts and diffs; secrets
>   matching known patterns are redacted before they leave the machine.
> - It works on time windows (default: block within 20 s, warn within 60 s)
>   and reasons about text, not meaning. A changed signature in one file and
>   a new caller in another is caught only on a best-effort basis.
> - It fails open. If the relay or daemon is down, edits go ahead and the
>   agent shows a notice at most once every ten minutes.
>
> Try it (Python 3.12+):
>
>     pipx install synergo
>     cd your-repo && synergo up --relay wss://<relay>/ws
>
> Or locally with two checkouts and `synergo relay`; the README has the steps.
>
> I would most like to hear about false positives (blocks you think were
> wrong), agents you want adapters for, and what would make you comfortable
> running this on a real team.
>
> Repo: https://github.com/AdrianShah/Synergo

## Before posting, replace or confirm

- `pipx install synergo`: pending the PyPI release. Until then use
  `pipx install git+https://github.com/AdrianShah/Synergo`.
- `wss://<relay>/ws`: the hosted relay URL once it exists (pending). If there
  is no hosted relay on launch day, replace the "Try it" block with the local
  path: `pipx install "synergo[relay]"`, `synergo relay`, then
  `synergo up` in two clones.
- The 117 ms number: re-run `scripts/bench_hook.py -n 50` the day before and
  use the fresh number, with the OS.
- The benchmark numbers: re-run `scripts/benchmark_decisions.py`; the relay's
  decision code is still changing (0.50 block recall on 2026-10-06).
- Phase 5 features still in progress (file queues, room history, webhooks,
  dashboard timeline and heat map): if they have merged, check each
  limitation above still holds.
- The quoted block message: if the wait queue has merged, its wording
  changes (place in line, a suggested file, `synergo wait`). Paste the exact
  text from the demo recording.
- Tree-sitter symbols need the optional `symbols` extra
  (`pipx install "synergo[symbols]"`); keep the "if installed" wording.

## Likely questions, with honest answers

**Does this slow my agent down?** A hook process starts on every edit. On
Windows that is 117 ms at p95 against a local stub daemon; add the relay
round trip on top. If the relay takes more than 1.5 s, the edit proceeds.

**What does the relay see?** In a normal room: names, agent kinds, paths,
symbols, line ranges, branches, prompts, plans and diffs (diffs can be turned
off with `share_diffs = false`). In an e2e room: hashes and metadata only.
The relay logs never contain prompts or diffs. If the operator sets
`GEMINI_API_KEY`, cleartext rooms' prompts and diffs go to Google's API for
captions; never from an e2e room.

**Why not just use branches or worktrees?** Keep using them. Synergo warns
"this will conflict at merge" when two agents change the same code on
different branches, which is when the conflict is still cheap.

**Why not split the work up front?** Do that too. Declared plans are shared,
so a teammate's agent is warned before it edits a file your plan names, and
edits are still checked when the plan drifts.

**Who can join my room?** Anyone with the 8-character code, like a document
share link. Codes expire after 7 days and guesses are rate-limited. The
creator can require approval for every newcomer
(`synergo room require-approval on`).

**Is there a hosted version / pricing?** Pending. Everything, relay
included, can be self-hosted today.
