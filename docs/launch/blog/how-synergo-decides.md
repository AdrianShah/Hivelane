# How Synergo decides: functions, lines, branches and plans

*Draft. Publish on D+7 (see the launch checklist). Sources:
`docs/conflict-detection.md`, `server/room.py` (`_judge_touch`,
`evaluate_edit`), `server/main.py` (`_edit_check_reason`) and a run of
`scripts/benchmark_decisions.py` on 2026-10-06. The relay's decision code
was still changing when this was written: re-run the benchmark and re-check
the rules against `_judge_touch` before publishing.*

Before every edit, an agent's Synergo hook asks the relay one question: may I
change this? The relay has three answers. **Allow** says nothing and the edit
goes ahead. **Warn** goes ahead too, with a sentence the agent and its human
can read. **Block** denies the edit and says who is in the way and why.

A block interrupts real work, so it has to be right. A warning that is
usually noise teaches people to ignore warnings. This post is about how the
relay chooses, and how we measure whether it chose well.

## What the relay knows

For every file an agent changes, its daemon sends the relay:

- the **symbols** touched: function and class names, worked out from the
  before and after text (Python with the standard library's `ast`; seven
  more languages with tree-sitter when the optional `symbols` extra is
  installed, regex heuristics otherwise). An edit to the import block is the
  special symbol `<imports>`;
- the 1-based **line ranges** touched;
- the git **branch** and an opaque **repo id** (a hash of the origin URL, or
  the root commit), so two repositories that share a room never collide.

Before an edit, the daemon asks with the same fields. For string-replacement
edits (Claude Code's `Edit`, Gemini CLI's `replace`) the hook passes the old
and new strings along, so the daemon can predict the lines and functions an
edit will touch before it happens.

Agents can also declare a **plan** (a summary plus the files and symbols it
names: Claude Code's todo list and plan mode, or the MCP server's
`declare_intent`), and every prompt is shared as the agent's current
**intent**.

## Two windows

Recency matters. A teammate who edited `greet()` five seconds ago is almost
certainly still working on it; one who edited it an hour ago has probably
moved on. The relay uses two windows, set in the team's committed
`.synergo.toml`:

```toml
block_window_seconds = 20   # a peer's edit this recent can block (0: never block)
warn_window_seconds = 60    # this recent still warns
```

A peer's edit older than the warn window is ignored. The relay caps the
windows (600 and 3,600 seconds) so one member cannot switch blocking off for
the room by accident, and a block window of 0 still warns.

## The decision, one peer at a time

For each teammate who touched the same file in the same repo inside the warn
window, the relay applies these rules in order. "Hard" means block if the
teammate's edit is inside the block window, warn otherwise.

| Situation | Answer | Scope |
|-----------|--------|-------|
| The teammate is on a different (known) branch | warn: "this will conflict at merge" | `branch` |
| Both sides have symbols, and they share one | hard | `symbol` |
| Both have symbols, none shared, but it is a manifest, lockfile or config file | hard | `file` |
| Both have symbols, none shared, but the line ranges literally overlap | hard | `lines` |
| Both have symbols, none shared | warn: "different function, safe to proceed" | `symbol` |
| Only line ranges: within 10 lines of each other | hard | `lines` |
| Only line ranges, further apart | warn | `lines` |
| Nothing finer than the file (older clients) | hard | `file` |

If any teammate's touch produces a block, the answer is block. If nobody
touched the file but a teammate's **active plan** names it (or a symbol
being edited), the answer is a `plan` warning. Failing that, if a teammate's
**intent** mentions the file ("fix the room broadcast" matches
`server/room.py`), it is an `intent` warning.

A few of these rules exist because of specific mistakes:

- **Shared files never clear on disjoint symbols.** `package.json`,
  `pyproject.toml`, `requirements*.txt`, `*.lock`, `go.mod`, `Cargo.toml`,
  `tsconfig*.json`, `.env*`, `Dockerfile` and CI workflow files are judged
  per file. Two agents adding different dependencies still collide, in the
  lockfile if nowhere else.
- **Overlapping lines beat symbol names.** Symbol extraction can mislabel a
  hunk that sits on the boundary between two definitions. If the line ranges
  literally overlap, the relay treats it as a collision whatever the names
  say.
- **Branches downgrade to a warning.** Two agents on different branches are
  not overwriting each other right now; they are setting up a merge
  conflict. Saying so while it is still cheap to avoid is the useful
  answer, and blocking would be wrong.

## Every answer says why

Each warn or block carries its scope and one sentence written by the relay
about the teammate that drove it. The agent reads it, and so does the human:

| Scope | Example |
|-------|---------|
| `symbol`, block | `alice's claude-code is editing greet in src/hello.py (14s ago)` |
| `symbol`, warn | `alice's claude-code is editing greet in src/hello.py (40s ago) — different function, safe to proceed` |
| `lines`, block | `alice's claude-code is editing the same lines of src/hello.py (8s ago)` |
| `branch` | `alice edited src/hello.py on branch feature/greeting 30s ago — this will conflict at merge` |
| `plan` | `alice plans to change src/hello.py: <their plan summary>` |
| `intent` | `alice's claude-code said they are working on src/hello.py` |

The agent's adapter adds what to do ("wait, or pick another file" for a
block, "check with them before you overlap" for a warning) and the
teammate's current task.

The daemon adds one more, local, kind of warning. When a teammate changes a
function's parameters, and you are about to edit a file that imports that
module and calls the function, you get a note like "you're editing a caller
of `parse` (src/cli.py), whose signature alice changed 2 min ago". It is a
couple of regex scans of the one
file being edited, not a whole-repo graph, so it can be wrong in both
directions and only ever warns.

## End-to-end encrypted rooms

In an end-to-end encrypted room the relay cannot read paths or symbols, but
it can still compare them: each one arrives as a keyed hash
(HMAC-SHA256 with a key derived from the room passphrase), and equal names
hash equal. The table above works unchanged. The one exception is the shared
files rule, which needs the file name: for those files the daemon sends no
symbols and no line ranges, so the relay falls back to a file-level answer,
which is the same answer it would have given anyway.

## Measuring it

`scripts/benchmark_decisions.py` replays two-agent sessions against the
relay's real `Room` class, in process, twice: once as a file-level baseline
(symbols, ranges, branches and plans stripped) and once with everything. The
sessions are **synthetic**: 31 hand-built scenarios with 41 edit checks, each
labelled conflict or no conflict. They cover the same function, different
functions, nearby lines, import blocks, config files, other branches, plans,
intents, stale touches and different files.

Run on 2026-10-06:

```
rule=block (interrupts the agent)
  engine      prec recall    FPR   FP   TP
  baseline    0.57   0.59   0.53   10   13
  full        1.00   0.50   0.00    0   11
rule=alert (any warn or block)
  engine      prec recall    FPR   FP   TP
  baseline    0.56   0.86   0.79   15   19
  full        0.59   1.00   0.79   15   22
```

What the numbers say:

- **False blocks fell from 10 to 0.** Every one of the baseline's false
  blocks was two agents in different functions of one file, or the same
  code on different branches. Both now warn.
- **Every real conflict at least warns** (alert recall 1.00, up from 0.86),
  because plans and intents now produce warnings where file-level checks
  saw nothing.
- **Block recall is 0.50.** Of the 11 labelled conflicts that do not block,
  9 are by design: three plan-only and one intent-only conflict, where no
  one has edited anything yet, so a warning is the right answer; and five
  second checks of the same function 40 seconds later, outside the 20-second
  block window, which warn. The remaining two are real misses: different
  functions whose edits are three lines apart warn instead of blocking,
  because symbols are judged before line proximity. Whether those should
  block is an open question.
- **False alerts are unchanged at 15**, by design: different functions and
  other branches still warn, now with a scope that says why.

(`docs/conflict-detection.md` reports block recall 0.45 from an earlier run;
since then, overlapping lines and `.synergo.toml` changes block.)

A synthetic benchmark proves the rules do what we meant. It does not prove we
meant the right thing. That is what the dashboard's feedback is for.

## Letting the team grade it

Every warn and block appears on the blocked agent's lane in the dashboard,
with its scope label (same function, different function, nearby lines, same
file, planned, other branch, intent) and two buttons: **useful** and
**noise**. One vote per decision per dashboard; tallies are live for the
whole room, and Analytics shows the useful-to-noise ratio per scope for the
decisions that dashboard has seen this session. The relay also counts votes
by scope in its Prometheus metrics, without names or paths.

That gives us the question to ask of real use: which scopes are mostly
noise? If "different function" warnings are, they should become silent
allows. If the nearby-lines misses turn out to be real collisions, the
order of the rules should change. Turning voted decisions into new
benchmark sessions is not automated yet; it is the next step, so that a
future version of this table is measured on real cases rather than ones we
made up.

---

Synergo is open source (Apache-2.0):
[github.com/AdrianShah/Synergo](https://github.com/AdrianShah/Synergo). The
full decision table is in
[docs/conflict-detection.md](https://github.com/AdrianShah/Synergo/blob/main/docs/conflict-detection.md).
