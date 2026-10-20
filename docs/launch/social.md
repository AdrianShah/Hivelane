# Social posts

Rules for all of these: no competitor names (say "multiplayer IDEs" or "file
watchers"), no requests to upvote or share, no emoji, numbers only from the
fact sheet in [README.md](README.md). Post on D0 (X, LinkedIn) and from D+1
(Reddit, one community per day). Link the repo, not the HN thread: linking
to a vote-ranked thread reads as a request for votes.

Before posting, replace `pipx install hivelane` with
`pipx install git+https://github.com/AdrianShah/Hivelane` if the PyPI release
(pending) is not out, and attach the 10-second GIF from
[demo-script.md](demo-script.md) to the first post of each.

## X thread (7 posts)

Each post is under 280 characters (links count as 23).

**1/7** (attach the GIF)

> Running two AI coding agents in one repo? They will edit the same function
> sooner or later, and you find out at merge.
>
> Hivelane is an open-source add-on that makes each agent ask "is anyone else
> in this file?" before every edit. (1/7)

**2/7**

> How: `hivelane up` installs a pre-edit hook for Claude Code, Codex CLI,
> Cursor or Gemini CLI. Before each write, the hook asks a small relay what
> the rest of the team's agents are doing. The answer is allow, warn or
> block, with a reason. (2/7)

**3/7**

> A blocked agent reads something like:
>
> Hivelane: alice's claude-code is editing greet in src/hello.py (14s ago) —
> wait, or pick another file
>
> It waits or picks other work. The other agent never notices. (3/7)

**4/7**

> Decisions are per function and line range, not per file. Different
> function in the same file: warning, edit goes ahead. Same code on another
> branch: "this will conflict at merge". Lockfiles and manifests: judged per
> file. (4/7)

**5/7**

> We started as a file watcher. It kept reporting collisions after both
> agents had written, which is too late. On a synthetic 31-session benchmark,
> function-level checks took false-positive blocks from 10 to 0. (5/7)

**6/7**

> Honest limits: agents without hooks are observe-only. If the relay is down,
> edits go ahead (fails open). Whoever runs the relay sees prompts and diffs,
> unless you turn on end-to-end encryption, which hides content but not
> metadata. (6/7)

**7/7**

> Apache-2.0. Self-host the relay with `hivelane relay` or the Docker image,
> dashboard included.
>
> pipx install hivelane
> hivelane up
>
> github.com/AdrianShah/Hivelane (7/7)

The Docker image line needs P3 (public GHCR image); drop "or the Docker
image" if it is still private.

## LinkedIn

> Teams are starting to run several AI coding agents in the same repository
> at once: one per developer, or several per developer. The agents do not
> know about each other. Two of them rewrite the same function, and someone
> finds out at merge time.
>
> We built Hivelane to move that moment earlier. It is an open-source
> (Apache-2.0) add-on, not a new editor: `hivelane up` in a repository
> installs a pre-edit hook into the agents you already use (Claude Code,
> Codex CLI, Cursor and Gemini CLI). Before every edit, the agent's hook asks
> a small shared relay whether a teammate's agent is working on the same
> code. The relay answers allow, warn or block, with a sentence the agent can
> act on, such as "alice's claude-code is editing greet in src/hello.py
> (14s ago) — wait, or pick another file".
>
> A few design choices we would make again:
>
> - Decide per function and line range, not per file. On a synthetic
>   replay benchmark this took false-positive blocks from 10 to 0.
> - Fail open. If the relay or the local daemon is unavailable, the agent
>   keeps working and is told coordination is off. A coordination tool must
>   never stall the work it coordinates.
> - Keep data in the team's hands. The relay is one small process you can
>   run yourself, secrets matching known patterns are redacted before
>   anything leaves a laptop, and end-to-end encrypted rooms leave the relay
>   with hashes instead of code.
>
> And the limits: agents without pre-edit hooks can only be observed, not
> stopped, and the checks work on recent activity, so they reason about text
> rather than meaning.
>
> If your team runs agents in parallel, I would like to hear how you
> coordinate them today, and where this would or would not fit.
>
> https://github.com/AdrianShah/Hivelane

## Reddit

Both communities restrict self-promotion. Read the sidebar rules on the day
and follow them over anything here; Reddit could not be fetched while writing
this (2026-10-06), so the rules below are second-hand. Post from an account
with real participation history in the community, lead with the problem and
how it works, answer every question in the thread, and never cross-post the
same text to both.

### r/ClaudeAI

What could be found about the rules: project posts use the "Built with
Claude" flair and must say what was built, how it was built, include
screenshots or a demo, and include at least one prompt used; posters with
less than 50 karma are pointed to a showcase megathread instead. Source: a
moderator comment on the subreddit's "Built with Claude" showcase megathread,
read through a third-party mirror on 2026-10-06; verify against the sidebar.

Hivelane's commits are co-authored with Claude Code, so the flair is honest.
The prompt below is a placeholder: use a real one from the project's history.

**Title:** How I stopped parallel Claude Code sessions from editing the same
function (pre-edit hooks + a small relay)

**Body:**

> **The problem.** When two people on a team each run Claude Code in the same
> repo, their agents eventually rewrite the same function. Nobody finds out
> until the merge. I first tried a file watcher, but a watcher only sees an
> edit after it is written: by the time it flags the overlap, both versions
> exist.
>
> **What I built.** Hivelane, an open-source (Apache-2.0) coordination layer
> that uses Claude Code's own hooks. `hivelane up` adds three entries to the
> repo's `.claude/settings.json`:
>
> - `UserPromptSubmit`: your prompt is shared with the room as your current
>   intent.
> - `PreToolUse` on `Edit|Write|MultiEdit|NotebookEdit`: before the edit, the
>   hook asks a local daemon, which asks a shared relay, whether a
>   teammate's agent is in the same function. On a block, the hook returns
>   `permissionDecision: "deny"` with a reason Claude reads, for example:
>   `Hivelane: alice's claude-code is editing greet in src/hello.py (14s ago) —
>   wait, or pick another file (their task: "Add a greeting parameter to
>   greet()")`. On a warning it only adds context and a `systemMessage`; it
>   never auto-approves, so your normal permission flow still applies.
> - `PostToolUse` on edits, `TodoWrite` and `ExitPlanMode`: the change (and
>   Claude's todo list or plan) is shared, so a teammate's agent is warned
>   before it touches files your plan names.
>
> Decisions are per function and line range: a different function in the
> same file gets a warning, not a block; the same code on another branch
> gets "this will conflict at merge". If anything is down, the hook lets the
> edit through and says so at most every ten minutes.
>
> **How I built it.** Mostly with Claude Code itself (the commits are
> co-authored). The hook path is stdlib-only Python because Claude Code
> starts it on every edit: p95 went from 1665 ms to 117 ms on Windows once
> nothing heavy was imported (measured with `scripts/bench_hook.py` against
> a local stub daemon). One prompt I used: `[insert a real prompt]`.
>
> **Demo:** [GIF or video link]
>
> **Limits:** sessions in the same checkout on one machine share an identity
> and are not checked against each other; edits made through Bash are not
> checked; whoever runs the relay sees prompts and diffs unless you enable
> end-to-end encryption (which hides content, not metadata). The relay is
> self-hostable (`hivelane relay`, or a Docker image).
>
> Repo: https://github.com/AdrianShah/Hivelane. I would really like to hear
> about blocks that felt wrong.

### r/cursor

The rules could not be read while writing this. Check the sidebar for a
self-promotion rule, a required flair, or a weekly showcase thread, and use
that format. If self-promotion is not allowed at all, do not post.

**Title:** Using Cursor's hooks to stop two agents from editing the same
function

**Body:**

> **The problem.** With more people on a team using Cursor's agent in the
> same repo (and often other agents too), two agents end up rewriting the
> same code and the collision shows up at merge. A file watcher does not
> help much: it reports the overlap after both edits are on disk.
>
> **How Cursor's hooks make prevention possible.** Cursor reads
> `.cursor/hooks.json`. Hivelane (open source, Apache-2.0) registers three
> hooks there:
>
> - `beforeSubmitPrompt`: your prompt becomes your intent in a shared room.
>   Hivelane always answers `{"continue": true}`; it never blocks a prompt.
> - `preToolUse` on the agent's `Write` and `Delete` tools: before the edit,
>   the hook asks a shared relay whether a teammate's agent is in the same
>   function. On a block it answers `{"permission": "deny"}` with a message
>   for you and one for the agent. On a warning it allows, with the same
>   messages, so Cursor's own approval still runs.
> - `afterFileEdit`: the edit is shared with the room.
>
> The relay decides per function and line range: a different function in
> the same file is a warning, the same code on another branch is "this will
> conflict at merge". If anything is down, edits go ahead.
>
> **What it does not cover in Cursor.** Tab completions and your own typing
> have no blocking hook. For those, a small VS Code-compatible extension
> shows which files teammates' agents are in and warns you the first time
> you change one; it never blocks.
>
> Setup is `hivelane up` in the repo (it detects Cursor and writes the hooks);
> teammates pull and run the same command. Works alongside Claude Code,
> Codex CLI and Gemini CLI users in the same room.
>
> Repo: https://github.com/AdrianShah/Hivelane. Questions and "this blocked me
> for no reason" reports are very welcome.

The extension paragraph needs P4 (Open VSX listing, pending). Until then,
either drop it or say it installs from a `.vsix` in the repo.
