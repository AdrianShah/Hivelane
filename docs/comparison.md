# Approaches compared

Teams running several coding agents in one repository coordinate them in a
handful of ways. This page compares those approaches in general terms, so you
can pick what fits your team, including when that is not Hivelane. They are
not exclusive: most teams combine two or three.

## The approaches

**Git-only workflows.** Each agent or person works on its own branch (often in
its own worktree or clone), and conflicts are found and resolved when the
branches merge. Nothing extra to run, and it is how most teams already work.

**Up-front task splitting.** A person, or an orchestrating agent, divides the
work so that no two agents are given the same files. Overlap is avoided by
planning rather than detected.

**File watchers.** A process watches the working tree, or each person's
changes, and reports when two people have modified the same file. Works with
any editor or agent, because it only looks at the disk.

**Standalone multiplayer IDEs.** Everyone, human and agent, works inside one
shared editing environment that merges keystrokes in real time, so there is
only ever one copy of each file.

**Hook-based pre-edit checks (Hivelane).** Each agent's own hook asks a shared
relay "is anyone else in this file?" before every edit, and the relay answers
allow, warn or block from what the rest of the team's agents are doing, down
to the function and line range.

## Side by side

| | Git-only | Task splitting | File watcher | Multiplayer IDE | Pre-edit checks |
|---|---|---|---|---|---|
| **When an overlap is noticed** | At merge | Before work starts, if the plan is right | After both sides have written | Never: edits merge live | Before the second edit is written |
| **Can it prevent the overlap?** | No | Yes, for overlaps the plan foresaw | No, it reports | Not needed for text; logical conflicts remain | Yes: warn or block |
| **Granularity** | Lines, at merge | Whatever the plan names | Usually whole files | Characters | Functions and line ranges, file as a fallback |
| **Works across different agents and editors** | Yes | Yes | Yes | No: everyone uses the one environment | Agents with hooks; others observe-only |
| **Handles work that changes direction mid-task** | At merge | No: the plan goes stale | Yes | Yes | Yes |
| **Extra infrastructure** | None | An orchestrator, or a person's time | A watcher per machine, plus a way to share results | The environment itself | A relay, plus a small daemon per machine |
| **What is shared beyond git** | Nothing | The plan | File activity | Everything, live | File paths, diffs, prompts and touched symbols (narrower settings and an encrypted mode exist) |
| **When it breaks** | Painful merges | Silent overlap | Late warnings | Lost work if the session or service fails | Coordination lapses, edits proceed (fails open) |

## Strengths and limits of each

### Git-only workflows

Strong: no new tools, a complete history, and review before anything lands.
Isolated branches mean agents never step on each other's files while they
work.

Limited: every overlap surfaces at merge, after both sides have spent the
effort. With several fast agents, the branches diverge quickly, and the person
merging has to understand both sides of each conflict. Nothing tells you
while the work is happening that two agents are rewriting the same function.

### Up-front task splitting

Strong: when the plan is right, overlap never happens, and agents work at
full speed. Cheap when the work divides cleanly (separate services, separate
features).

Limited: plans are made before anyone knows exactly which files a task will
need. Agents discover shared helpers, configuration and dependency manifests
as they go. Nothing notices when the plan and the actual work drift apart, and
it only covers agents the planner controls, not a teammate's.

### File watchers

Strong: tool-agnostic and simple, since anything that writes to disk is
covered, including humans in any editor and agents with no hook support.

Limited: a watcher sees an edit only after it is written, so by the time two
people are flagged on one file, both have already changed it. Most watchers
compare whole files, so two agents in unrelated functions of a large file
raise the same alarm as two agents in the same function, and frequent false
alarms train people to ignore them. (Hivelane started as a file watcher and
moved to hooks for exactly this reason; `hivelane watch` and `hivelane run`
remain as an observe-only fallback for agents without hooks.)

### Standalone multiplayer IDEs

Strong: there is one copy of the code, so text conflicts cannot happen, and
everyone sees everyone else's cursor and edits as they happen.

Limited: everyone has to work inside the same environment, which rules out a
team where people use different editors, terminal agents and local
toolchains. Merging keystrokes keeps the text consistent, not the logic: two
agents can still make incompatible changes to the same function at the same
time, now without any conflict marker to flag it. Work usually depends on the
environment's servers being available.

### Hook-based pre-edit checks

Strong: the check happens before the edit is written, so a collision can be
prevented, not just reported. Decisions are made per function and line range,
so agents can share a file without blocking each other. Each agent keeps its
own tool, editor and git workflow, and the dashboard shows every agent's
intent and plan as the work happens.

Limited, and worth knowing before you adopt it:

- **It needs hooks.** An agent that cannot run a command before an edit can
  only be observed, after the fact. Edits an agent makes outside its edit
  tools, such as a shell command that rewrites a file, are not checked.
- **It needs a relay** that the whole team can reach, run by you or by
  someone you trust with prompts and diffs. Narrower sharing settings and an
  end-to-end encrypted mode reduce what the relay sees; see
  [Security](security.md).
- **It works on time windows.** A recent edit by a peer counts; an old one
  does not. That is a heuristic: it can warn about work that would have merged
  cleanly, and it does not see a conflict between changes made hours apart.
- **It reasons about text, not meaning.** Two edits to different files that
  break each other (a changed function signature and a new caller) are only
  caught on a best-effort basis.
- **It fails open.** When the relay or the daemon is down, edits go ahead
  uncoordinated. The agent is told, but nothing is blocked.

[Conflict detection](conflict-detection.md) explains how decisions are made,
and the benchmark there measures how often they are wrong.

## Combining them

Pre-edit checks complement the other approaches rather than replacing them:

- **With git branches and worktrees**, edits on different branches get a
  "this will conflict at merge" warning instead of a block, so you hear about
  the merge conflict while it is still cheap to avoid.
- **With task splitting**, an agent's declared plan is shared with the room,
  and a teammate's agent is warned before it edits a file the plan claims.
  The plan stays useful after it drifts, because the edits themselves are
  checked too.
- **With review**, nothing changes: everything still lands through your
  normal pull requests.
