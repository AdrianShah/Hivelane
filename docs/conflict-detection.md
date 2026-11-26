# Conflict detection

How the relay decides whether an agent's pending edit is `allow`, `warn` or
`block`, how the dashboard shows it, and how we measure whether it is any good.

## Inputs

Each daemon sends, per changed file, the touched **symbols** (function/class
names; `<imports>` marks an import-block edit) and 1-based **line ranges**,
plus the git **branch** and an opaque **repo_id**. Before an edit the daemon
calls `check_edit` with the same fields. Agents may also declare a **plan**
(`plan_update`: summary, files, symbols, expiry) and an **intent** (the current
prompt).

With each set of line ranges the daemon also sends a **version**: a
fingerprint of the file content those line numbers count (the current content
on an edit check, the new content on an edit). Line numbers are only compared
between equal versions. Two checkouts of a repo hold different versions of a
file as soon as one agent edits it, and after an agent adds lines, the same
number points at different code in the other copy. Across versions, symbols
decide on their own.

A Claude Code `Write` (a whole-file rewrite) is predicted by diffing the new
content against the current file, so it is judged by the definitions it
actually changes, not as touching every function. A pure insertion claims no
existing definition.

Older clients send none of the new fields; the relay then falls back to the
file-level decision it always made.

## Scopes

Every warn/block decision carries a `scope` telling the user why it fired.

| scope    | dashboard label                     | meaning                                         |
|----------|-------------------------------------|-------------------------------------------------|
| `symbol` | same function / different function  | judged by symbol overlap                        |
| `lines`  | nearby lines / distant lines        | judged by line-range proximity (no symbols)     |
| `file`   | same file                           | file-level only (legacy, or always-warn file)   |
| `plan`   | planned                             | a peer's active plan lists this file/symbol     |
| `branch` | other branch                        | peer edited the same file on a different branch |
| `intent` | intent                              | a peer's current prompt mentions this file      |
| `task`   | reserved by task                    | a claimed room task reserves this file          |
| `pending`| unpushed work                       | a peer changed this file and has not pushed yet |

## Decision table

Evaluated per recent peer touch of the same file in the same repo (warn
window), first match wins. "Hard" means `block` if the part of the touch that
matched (the shared symbol, the nearby line range, or for file scope the
latest batch) is inside the block window, else `warn`.

A peer's touch of a file is the **union of its batches** inside the window,
not just its latest one: each symbol and line range keeps the time it was last
edited. So if alice edited `foo` 6 s ago and `bar` 1 s ago, an edit to `foo`
is still blocked (judged by `foo`'s age), and symbols older than the warn
window drop out. A batch with no symbols (or no ranges) resets that part to
unknown, and the file is judged whole until symbols arrive again.

| situation                                               | decision | scope    |
|---------------------------------------------------------|----------|----------|
| peer is on a different (known) branch                   | warn, "conflict at merge" | `branch` |
| both sides have symbols and they intersect              | hard     | `symbol` |
| symbols on both sides, disjoint, always-warn file       | hard     | `file`   |
| both only share `<imports>`, same version, lines literally overlap | hard | `lines` |
| both only share `<imports>`                             | warn, "also adding imports" | `symbol` |
| symbols on both sides, disjoint, but line ranges literally overlap (same version) | hard | `lines` |
| symbols on both sides, disjoint                          | warn, "different function" | `symbol` |
| line ranges on both sides within 10 lines               | hard     | `lines`  |
| line ranges on both sides, further apart                | warn     | `lines`  |
| no symbols/ranges (legacy)                              | hard     | `file`   |
| no peer touch, but someone else has unpushed changes to the file (to a requested symbol, when both sides name symbols) | warn | `pending` |
| no peer touch, but a peer's active plan names the file (or, for a plan naming no files, a requested symbol) | warn | `plan` |
| no peer touch, but a peer's intent mentions the file    | warn     | `intent` |

Always-warn files (`package.json`, `pyproject.toml`, `requirements*.txt`,
`*.lock`, `go.mod`, `Cargo.toml`, `tsconfig*.json`, `.env*`, `Dockerfile`,
`.github/**/*.yml`) are never cleared by disjoint symbols. Two agents both
adding imports are separate edits (a warning), unless they change literally
the same import lines of the same version. One agent's new import line never
blocks a teammate editing an unrelated function in the same file.

**Checks in flight.** A peer's edit is only known once its activity arrives
(within a batch, about 1.5 s). So that two agents asking about the same
function in that gap are not both let through, every passed edit check
stands in for its edit: the relay judges later checks against the functions
it predicted until that member's activity for the file arrives (at most 30 s,
or until the member leaves). Pending checks never raise conflict alerts.

Plans: a plan's symbols belong to its files. `main` in a plan for
`api/server.py` says nothing about `cli/tool.py`, so plan symbols are matched
only when the plan lists no files, and `<imports>` never matches across files
in plan logic (edit checks or `plan_conflicts`).

Note: symbols are checked before line ranges, so two edits in *different*
functions whose ranges are within 10 lines get "different function" (warn),
not "nearby lines" (block). See benchmark results below.

## Unpushed work

Touches answer "who is typing now" and are forgotten after the warn window
(60 s by default). That misses the slow collision: ada edits the header,
leaves the change uncommitted or unpushed, and ten minutes later raj's agent
edits the same code. Nobody is typing at the same time, and git reports the
conflict at the next pull. So each daemon also reports its user's **pending
work** per repo (`pending_changes`, built by `hivelane/pending.py`):

* uncommitted working-tree changes (`git status`, untracked files included),
  plus the files of commits ahead of the branch's upstream; with no upstream,
  ahead of the merge-base with the default remote branch (`origin/HEAD`,
  `origin/main`, `origin/master`). A repo with no remote reports only its
  uncommitted files;
* where affordable, the definitions each file's unpushed hunks touch (one
  `git diff -U0` against the merge-base, the same symbol extraction as edits,
  cached per file version, at most 60 files parsed per scan); the rest are
  sent as whole files;
* `since` per file: its oldest unpushed commit, or when the daemon first saw
  it changed (its mtime on first sight).

The daemon scans every 45 s and 5 s after an edit, with timeouts on every git
call, at most 200 files, and the files your `.hivelane.toml` ignores left
out. It sends a report only when the set changed (and unchanged every 10
minutes as a refresh). Only the repo's main connection reports: your agent
lanes share your working tree. `HIVELANE_PENDING=0` turns it off.

The relay keeps the latest report per member and repo for as long as it is
true: each report replaces the last, an empty one (everything pushed) clears
it. When a member disconnects, its last report is **kept with its age and
marked offline** for 4 hours (`HIVELANE_PENDING_OFFLINE_SECONDS`), because
closing a laptop does not push anything; a report from it again replaces the
record. Nothing is persisted across a relay restart: daemons re-report within
a minute of reconnecting. Reports are bounded per member (8 repos), per room
(256 reports, `HIVELANE_MAX_PENDING_REPORTS_PER_ROOM`) and in bytes per room
(16 MiB, `HIVELANE_MAX_PENDING_BYTES_PER_ROOM`), oldest dropped first; see
[payload caps](security.md#payload-caps-and-quotas).

On an edit check, another person's pending report for the same file in the
same repo gives a **warn** with scope `pending`, never a block:
`ada has unpushed changes to web/Header.tsx (renderNav) from 25 min ago; pull
or coordinate first`. If both sides name symbols, only shared ones count
(always-warn files count whatever changed). On a different known branch the
reason says it will conflict at merge instead. "Another person" excludes your
own names: the requester, its chat's lanes (`same_chat`) and every name its
daemon speaks under (`own_names`, the same exemption room tasks use), so
`ada-claude` is never warned about `ada`'s working tree.

Live touches keep precedence: a block, a task reservation or a live warn
decides the check, and pending peers then only ride along in `others`
(`kind: "pending"`). The pending check comes before plans and intents.

**Pull hint.** Each scan also notes when the upstream branch has commits the
local branch does not (`git rev-list HEAD..upstream`) and which files they
change. An edit check for one of those files gets a one-time note: "origin/main
has 2 commits you have not pulled that change app.py; pull (or rebase) before
editing it". The daemon never fetches (that needs the network and your
credentials), so this only sees what your last fetch, or your IDE's
autofetch, brought in.

## Symbol names

Symbols are compared by exact string, so every daemon must name the same
definition the same way. `hivelane/symbols.py` has two tiers for non-Python
languages, tree-sitter (`hivelane[symbols]`) and a regex/brace heuristic, and
both give one format:

* qualified by containment with `.`: `Room.check_edit`, `Outer.Inner.run`;
* Go methods by receiver type, pointer or not, named or not: `Server.Handle`,
  `Stack.Push` for `func (s *Stack[T]) Push`;
* generic arguments and paths dropped: `impl<T> Foo<T>` and
  `impl From<T> for a::Foo<T>` are both `Foo`; `type Stack[T any]` is `Stack`;
* JS/TS methods only directly inside a class or interface (a semicolon-free
  bare call `doThing(x)` is not a definition), interface members included;
* Ruby one-line (`def a; 1; end`) and endless (`def b = 2`) defs end on their
  own line;
* Python top-level and class-level assignments to a single name are their own
  definitions (`LIMIT`, `Store.timeout`), so two agents changing different
  constants do not both count as `<module>`. Tuple unpacking, chained and
  attribute targets stay `<module>`, and inside a function the function is
  the unit.

`hivelane/tests/symbol_cases.py` holds the shared fixtures; the heuristic is
checked against them in every test run and tree-sitter when it is installed.
Lines are counted on `\n` only everywhere (pre_edit prediction, post_edit
ranges, both tiers), and a UTF-8 BOM no longer pushes Python off `ast`.

## Several agents per person

Every agent is its own party to these checks. The daemon gives each agent
session a lane, which is a relay member of its own named after you: the first
chat is `ada`, a second chat is `ada-claude`, Cursor beside Claude Code is
`ada-cursor`, and a Claude Code subagent is `ada-explore-3f2a` (its type and a
short id). Their collisions are checked like anyone else's, with one
exception. Agents of the same chat never block each other. A subagent editing
a file its parent just touched is treated as a hand-off and allowed. Two
subagents of one chat in the same file get a warning. The daemon names the
chat's other lanes on each edit check, so the relay decides this itself and
the dashboard shows the same decision the agent got. A subagent's lane shows
the task it was given as its intent and closes a few seconds after it
finishes. Idle lanes close after 15 minutes.

A [room task](tasks.md) adds one more rule. While a task is claimed, its
files are reserved. A teammate's edit to one of them is blocked with
`scope: "task"` ("server/api.py is reserved by adrian's task T1"), unless a
live edit by someone else already decides the check. The claimer's own lanes
are exempt: the daemon names them on every edit check (`own_names`).

If the relay will not admit a lane (a room that requires approval, a full
room), that agent falls back to sharing your main lane and the daemon tries
again two minutes later. `HIVELANE_LANES=0` turns lanes off.

## Dashboard

* Each lane shows **Unpushed: N files** when its member has pending work;
  the lane's detail lists the files (symbols, and how long each has been
  pending), oldest first.
* Each lane shows a **branch badge**, the agent's **active plan** (summary;
  planned files as hollow markers, filled once the agent has edited them), and
  the **symbols** touched per changed file.
* Each warn/block chip shows its scope label and **Useful / Noise** buttons.
  A vote sends `{type:"decision_feedback", decision_id, useful}` over the
  dashboard's spectator socket, once per decision (buttons disable after).
  Live tallies come back as `decision_feedback_update`.
* Analytics → **Detection quality** aggregates useful vs noise votes per scope
  for decisions seen this session.

## Feedback loop

Votes are cheap labels from the people being interrupted. Per-scope noise
share tells us which rules to relax (e.g. if "different function" warns are
mostly noise, drop them to allow) and the votes can be exported into new
benchmark sessions.

## Semantic matching (`server/semantic.py`)

Pure, stdlib-only, no server imports.

* `similar_intents(text, others, threshold=0.8)`, returns `(user_id, score)`
  for peer intents describing the same task. Text is normalized (camelCase /
  snake_case split, lowercase), stopwords and generic coding verbs dropped,
  lightly stemmed; the score blends char-trigram cosine (0.5) with token
  Jaccard (0.5), mapped onto a 0..1 confidence scale where 0.8 corresponds to
  the calibrated lexical cutoff (0.32 raw).
* `HIVELANE_EMBEDDINGS=fastembed` switches to sentence embeddings if the
  optional `fastembed` package is installed (lazy import; any failure falls
  back to lexical). It is not a dependency.
* `files_mentioned(text, known_paths)`, fuzzy path/basename/stem/symbol
  matching: "fix the room broadcast" → `server/room.py`.

Calibration on `server/tests/fixtures/intent_pairs.json` (40 labelled pairs):
precision 1.00, recall 1.00 at the default threshold, and anywhere in
0.6–0.9. The fixture separates cleanly, so treat these numbers as an upper
bound until real prompts are collected.

## Benchmark (`scripts/benchmark_decisions.py`)

Replays `benchmarks/sessions/*.json` (31 sessions, 41 labelled `check_edit`
calls) against `server.room.Room` in-process, twice: a file-level baseline
(symbols, ranges, branch, plans stripped) and the full engine.

```
rule=block (interrupts the agent)
  engine      prec recall    FPR   FP   TP
  baseline    0.57   0.59   0.53   10   13
  full        1.00   0.45   0.00    0   10
rule=alert (any warn or block)
  baseline    0.56   0.86   0.79   15   19
  full        0.59   1.00   0.79   15   22
```

* False-positive **blocks** fall 10 → 0 (-100%, target was -50%): different
  functions and other branches are downgraded to warn.
* False-positive **alerts** are unchanged (15 → 15) by design: those cases
  still warn, now with a scope that says why. Recall rises because plans now
  warn.
* Block recall drops (13 → 10 TP): the two `nearby_lines_*` sessions (different
  symbols, ranges 3 lines apart) warn instead of block because symbols are
  judged before line proximity; and `config_file_2` (`.hivelane.toml`, ranges
  far apart) warns because `.hivelane.toml` is not in the always-warn list.

Run with `-v` for per-check rows.
