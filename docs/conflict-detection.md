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
| symbols on both sides, disjoint, but line ranges literally overlap | hard | `lines` |
| symbols on both sides, disjoint                          | warn, "different function" | `symbol` |
| line ranges on both sides within 10 lines               | hard     | `lines`  |
| line ranges on both sides, further apart                | warn     | `lines`  |
| no symbols/ranges (legacy)                              | hard     | `file`   |
| no peer touch, but a peer's active plan names the file (or, for a plan naming no files, a requested symbol) | warn | `plan` |
| no peer touch, but a peer's intent mentions the file    | warn     | `intent` |

Always-warn files (`package.json`, `pyproject.toml`, `requirements*.txt`,
`*.lock`, `go.mod`, `Cargo.toml`, `tsconfig*.json`, `.env*`, `Dockerfile`,
`.github/**/*.yml`) are never cleared by disjoint symbols. `<imports>` is
a symbol like any other: two agents both editing the import block collide
(`symbol`), but one agent's new import line does not block a teammate editing
an unrelated function in the same file.

Plans: a plan's symbols belong to its files. `main` in a plan for
`api/server.py` says nothing about `cli/tool.py`, so plan symbols are matched
only when the plan lists no files, and `<imports>` never matches across files
in plan logic (edit checks or `plan_conflicts`).

Note: symbols are checked before line ranges, so two edits in *different*
functions whose ranges are within 10 lines get "different function" (warn),
not "nearby lines" (block). See benchmark results below.

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
  own line.

`hivelane/tests/symbol_cases.py` holds the shared fixtures; the heuristic is
checked against them in every test run and tree-sitter when it is installed.
Lines are counted on `\n` only everywhere (pre_edit prediction, post_edit
ranges, both tiers), and a UTF-8 BOM no longer pushes Python off `ast`.

## Dashboard

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
