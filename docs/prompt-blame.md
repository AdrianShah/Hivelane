# Prompt blame

`git blame` tells you who committed a line. `hivelane blame` also tells you
which agent wrote it, from which prompt, for which task, and when:

```text
$ hivelane blame server/room.py:120-128
server/room.py:120-126  3f2a9c1e  2026-11-26 14:03  Ada Lovelace
  agent:  claude-code (subagent Tester) for ada, edited 2026-11-26 13:58
  task:   Add retry to fetch
  prompt: "make fetch retry three times with exponential backoff and log each attempt"
  in:     fetch
server/room.py:127-128  9ab12cd0  2026-11-20 09:12  Ada Lovelace  - no agent provenance recorded
```

It is entirely local. It needs no relay and no room, so it is just as useful
to a solo developer, and nothing leaves your machine unless you push it
explicitly.

## Set it up

```bash
hivelane up                 # the agent hooks and the daemon (already done if you use Hivelane)
hivelane githook install    # pre-commit, post-commit and post-rewrite hooks
```

`githook install` chains to any hooks you already have. Each existing hook is
moved unchanged to `<hook>.hivelane-prev` and still runs after ours, with the
same arguments and, for `post-rewrite`, the same stdin. `hivelane githook
uninstall` puts them back. `--no-blame` installs only the pre-commit check.

## How it works

1. **Record.** The daemon already sees every agent's prompts, plans, plan
   steps and edits. For each finished edit it appends one record to a journal
   inside the repo's git directory: `.git/hivelane/provenance.jsonl`, or the
   worktree's own git directory in a linked worktree. The journal is
   owner-only (0600) and never committed. A record holds:
   - the file and the agent
   - the person (your Hivelane name) and the session
   - the subagent type and the tool
   - the prompt and plan excerpts
   - the active plan step (the task)
   - the symbols and line ranges
   - short hashes of the lines the edit wrote

   A subagent's prompt is the task its parent handed it.
2. **Attach.** After each commit the `post-commit` hook reads the lines the
   commit adds. It picks the journal records whose written lines are among
   them and stores them as a git note on the commit, under
   `refs/notes/hivelane`. Records whose lines are not in the commit wait for a
   later one, so a partial commit leaves the rest of the work for the next.
   Each record is attached once.
3. **Blame.** `hivelane blame` runs `git blame`, reads the notes of the
   commits it names, and attributes each line to the edit whose written lines
   include it, wherever the line has since moved. A generic line such as `}`
   or a blank line is matched by the edit's line ranges instead. A line
   someone typed by hand in the middle of an agent's edit is not attributed to
   the agent. Uncommitted lines are matched against the journal.

A note is JSON Lines, one entry per edit, each with a unique `id`, so git's
own note merges keep it valid.

## Commands

```text
hivelane blame FILE[:LINE[-LINE]] [--json] [--root PATH]
hivelane notes status [--json]
hivelane notes push  [REMOTE]     # default origin
hivelane notes fetch [REMOTE]
```

`--json` prints one object per group of consecutive lines with the same
commit and edit:

- `file`, `start`, `end`
- `commit`, `author`, `author_mail`, `author_time`, `summary`
- `uncommitted`
- `match`: `line` (the line's own hash), `range` (a generic line inside the
  edit's ranges), `file` (the file was too big to hash), or null
- `provenance`: `agent`, `subagent`, `user`, `session`, `tool`, `prompt`,
  `plan`, `task`, `symbols`, `at`; null when nothing was recorded

Agents can ask too. The MCP server (`hivelane mcp`) has a `why_code(path,
start_line?, end_line?)` tool that answers "why is this code like this?" with
the same information. It reads git directly and works with the daemon
stopped.

## Rewriting history

| What you do | What happens to the notes |
|-------------|---------------------------|
| `git commit` | The post-commit hook attaches the matching journal records. |
| Partial commit (`git add -p`, a subset of files) | Records whose lines were not committed stay in the journal for a later commit. |
| `git commit --amend` | The post-rewrite hook copies the old commit's note to the new one. Edits made since are added by post-commit. |
| `git rebase` (including `-i` with squash or fixup) | post-rewrite copies every rewritten commit's note to its new commit; squashed commits' notes are merged. Nothing new is attached during the rebase. |
| `git reset --soft` and commit again, `git cherry-pick`, `git merge --squash` | Git reports no rewrite, so the notes stay on the original commits and the new commit has none. Use `rebase -i` or `--amend` instead, or copy them with `git notes --ref=hivelane copy OLD NEW`. |
| Commit made without the hooks (another machine, a GUI with hooks disabled) | No note. The edits stay in the journal and attach to a later commit whose added lines include them, until they expire after 30 days. |

## Privacy

- **Local by default.** The journal lives in your git directory. Notes are
  ordinary git objects that `git push` does not send. Only `hivelane notes
  push` shares them, and it shares every entry under
  `refs/notes/hivelane`, prompt excerpts included, with everyone who can read
  that remote. `hivelane notes fetch` merges a remote's notes into yours.
- **Nothing goes through the relay.** Prompt blame sends nothing new to the
  relay, so e2e rooms are unaffected.
- **Redacted.** Prompts and plans pass through `hivelane/redact.py` before they
  are written anywhere, including the journal. Your `redact_patterns` apply.
  Text is redacted even when `redact = false`, which only exists for false
  positives in shared diffs.
- **Capped.** Excerpts are cut to `blame_excerpt_chars` characters (default
  280, at most 2000).
- **No prompt text at all.** `blame_prompts = false` keeps the agent, task,
  person and lines, but no prompt or plan text. Setting it later also
  strips prompt text from records not yet attached.

```toml
# .hivelane.toml (the team's) or ~/.hivelane/config.toml (yours)
blame = true                 # false: record nothing
blame_prompts = true         # false: keep no prompt or plan text
blame_excerpt_chars = 280    # 0-2000
```

For these three keys the more private value wins, whichever file it is in.
The team can keep prompts out of notes, and so can each person.

## Never in the way

The post-commit and post-rewrite hooks discard their output and ignore their
own exit status. Every git call they make has a 3-second timeout, and any
error means nothing is attached: a commit is never failed or held. The
daemon writes the journal off the hook's decision path, and recording never
fails an edit. A journal line that cannot be parsed is skipped.

## Limits

- Edits are journalled only while the daemon is running, for agents whose
  edits it sees (hook adapters, `hivelane mcp` `report_edit`, `hivelane
  watch`).
- When an agent sends no "before" version of a file, the whole file counts as
  that edit's, until the commit's own diff narrows it down.
- Room tasks claimed with `hivelane task claim` are not linked yet. The task
  is the agent's own active plan step.
- Lines are matched by content hash, so two edits that write an identical
  line are told apart only by their line ranges and by which came last.
