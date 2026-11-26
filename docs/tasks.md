# Room tasks

A room has a shared task list. People and agents add tasks, claim one under a
lease, post progress notes and mark it done. A task can list the files it
covers, and **claiming the task reserves them**: while the claim lasts, a
teammate's agent that tries to edit one of those files is blocked with a
message naming the task. Your own agents are not blocked. That includes every
chat you have open and every subagent they start.

Tasks live on the relay alongside intents and plans. Like those, they are kept
in memory: a relay restart, or a room that stays empty for 30 minutes, clears
the list.

## From the shell

```bash
hivelane task add "Add per-user rate limiting" --files server/api.py,server/limits.py
hivelane task add "Review the limiter" --after T1
hivelane task list
hivelane task claim --next             # the oldest task that is ready
hivelane task note T1 "limiter done, wiring the API next"
hivelane task done T1 "PR #42"
```

| Command | What it does |
|---------|--------------|
| `add TITLE [--body TEXT] [--files A,B] [--after T1,T2] [--for NAME]` | Create a task. `--files` are reserved while it is claimed; `--after` lists tasks that must finish first; `--for` suggests it for a teammate. |
| `import-github --label LABEL [--limit N]` | Add the repo's open GitHub issues with that label as tasks titled `#123 <issue title>` (needs the [GitHub CLI](https://cli.github.com), signed in). Issues that already have a task are skipped, so it is safe to run again. |
| `list [--all] [--mine] [--json]` | Open and claimed tasks (`--all` adds finished ones). |
| `show ID [--json]` | One task with its notes. |
| `claim [ID \| --next] [--wait DURATION] [--lease DURATION]` | Claim a task. `--next` takes the oldest ready one; `--wait 10m` keeps trying while nothing is ready. |
| `renew ID [--lease DURATION]` | Extend your claim. |
| `note ID TEXT` | Post a progress note (creator or claimer). |
| `done ID [RESULT]` | Finish your claimed task; its files are free again. |
| `release ID` | Give up your claim; the task is open again. |
| `cancel ID [REASON]` | Cancel a task you created or claimed. |

Durations read as `90`, `90s`, `30m`, `2h`. Commands work from anywhere inside
the repo, and the daemon sends them to that repo's room.

## From an agent

Agents with shell access can run the commands above. Agents connected through
`hivelane mcp` have the tools `task_list`, `task_add`, `task_claim`,
`task_note`, `task_done` and `task_release` (see [the CLI reference](cli.md#hivelane-mcp)).

To hand an idle agent work, tell it to **grab the next task**. The MCP
instructions (and `hivelane mcp --agents-md`) tell it to call `task_claim`
with `next: true`, or `hivelane task claim --next` from a shell, and to work
on what that returns.

## Agent plans

When a coding agent writes a step plan, each step becomes a room task by
itself, claimed under your name and marked as that agent's plan
(`source: "agent_plan"`, with the agent's lane). Teammates see what your
agent intends to do, step by step, before it touches anything.

| The agent's plan | The room task |
|------------------|---------------|
| a new step | created and claimed by you at once |
| `pending` | claimed, not active: it reserves nothing |
| `in_progress` | the **active** step: it reserves its files |
| `completed` | done |
| removed from the list | cancelled |

A step starts with no files. While it is in progress, every file the agent
edits is attached to it, so the reservation grows with the work and is freed
when the step is done. Re-sent lists update the same tasks: steps are matched
by the agent's own id, then by their text, then by their place in the list
(a step reworded in place keeps its task). Plan steps are never offered by
`claim --next`, and a plan holds at most 30 steps.

Which agents: **Claude Code** (its `TodoWrite` tool, and the `TaskCreate` /
`TaskUpdate` tools newer releases use) and **Codex** (`update_plan`, if Codex
runs hooks for that tool; this is not confirmed, and if it does not, nothing
happens). Cursor and Gemini CLI do not expose a plan in their hooks, so their
work shows up only as edits.

The daemon does this in the background, after the hook has answered, so it
adds nothing to an agent's tool calls. It remembers which task belongs to
which step for as long as it runs: after a daemon restart, a re-sent list
makes new tasks (the old ones lapse with their leases). A pending step is
held for 8 hours between updates, the active step 30 minutes (renewed by the
agent's edits and by each new list).

To turn it off for a repo, set `plan_tasks = false` in `.hivelane.toml` (or in
your personal config, for every repo on your machine).

## Rules

- **Ready.** A task is ready when it is open and every task it depends on is
  done or cancelled. `claim --next` picks the oldest ready task in the repo:
  one suggested for you first, then one suggested for nobody, then any. A
  task without a repo (a dashboard's, in a room with several repos) belongs
  to every repo.
- **Lease.** A claim lasts 30 minutes unless you set `--lease` (1 minute to
  8 hours). When the lease runs out, the task is open again. A claim is
  renewed by `renew`, and also automatically whenever one of your agents
  edits one of the task's files, so a claim stays alive while work is
  happening on it. A disconnect does not drop the claim.
- **Who claims.** Tasks are claimed under your main name in the room
  (`adrian`), whichever chat or tool ran the command. All of your lanes
  (`adrian-claude`, `adrian-cursor`, `adrian-explore-3f2a`) can then edit the
  reserved files.
- **Who may change a task.** Anyone may add or claim a task. Only the claimer
  may renew, release or finish it. The creator or the claimer may add notes
  or cancel it. A task added from a dashboard can be cancelled by that
  dashboard, or by a member once they have claimed it.
- **Reservations.** A teammate's edit to a reserved file is blocked
  (`scope: "task"`) with a reason that names the task, such as
  `server/api.py is reserved by adrian's task T1 "Add per-user rate limiting"
  (18 min left on its lease)` (in an encrypted room the relay cannot read the
  title, so the reason gives the id only), and a hint to pick other work from
  `hivelane task list` or take one with `hivelane task claim --next`. Someone actively editing the file right now
  still takes precedence: their edit is reported as usual. A reservation does
  not put anyone in the file's [wait queue](protocol.md#wait-queues).
- **Limits.** 200 open and claimed tasks per room, plus the 50 most recently
  finished. Titles up to 200 bytes, bodies 2,000, notes and results 500, up
  to 50 files and 10 dependencies per task, and 20 notes per task. Of the
  open tasks, at most 50 may come from dashboards.

## On the dashboard

The **Tasks** view shows agents' plans first, one card per agent lane
("adrian's claude: 3/5 steps") with the step it is on, then the team's own
tasks in four columns: Ready, Waiting on others, Claimed (with the time left
on each lease) and Done. A claimed task also appears on its claimer's lane
under "Working on", and each lane row on the main view shows its agent's
current plan step.

**Add task** opens a form: title, details, files, the tasks it comes after,
who it is suggested for, and your name. The dashboard sends it to the relay
itself; a member's agent then claims it with `claim --next` (or by id). A task
you added shows a **Cancel task** button in this browser.

Who may do this: any dashboard admitted to the room, the same people who can
already watch it (anyone with the room code, or only those an owner approved
when the room [requires approval](security.md#join-approval)). Dashboards may
only add tasks and cancel their own; claiming, finishing and notes stay with
members. The name you type is credited as the creator but nobody checks it,
so the task is marked as added from a dashboard everywhere it shows. Each
browser keeps a random key in `localStorage`; the relay stores only its hash
with each task it adds, and a cancel must present the same key. Clearing site
data gives up the ability to cancel those tasks from that browser.

## Encrypted rooms

In an [end-to-end encrypted room](security.md#end-to-end-encryption), a
task's title, body and note text travel only inside its encrypted blob, and
its files are hashed like every other path. The relay can still match a
reservation against an edit (equal paths hash equally), but it cannot read
what the task is about. Dashboards with the room key show the text. Without
the key they show a locked placeholder. Agents' plan steps are sealed the
same way. A dashboard adds a task to an encrypted room only with the room
key: it seals the task like a daemon does, and the relay refuses a
dashboard's task with readable text in such a room.
