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
| `add TITLE [--body TEXT] [--files A,B] [--after T1,T2]` | Create a task. `--files` are reserved while it is claimed; `--after` lists tasks that must finish first. |
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

## Rules

- **Ready.** A task is ready when it is open and every task it depends on is
  done or cancelled. `claim --next` picks the oldest ready task in the repo.
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
  or cancel it.
- **Reservations.** A teammate's edit to a reserved file is blocked
  (`scope: "task"`) with a reason such as `server/api.py is reserved by
  adrian's task T1 (18 min left on its lease)`, and a hint to pick other work
  from `hivelane task list`. Someone actively editing the file right now
  still takes precedence: their edit is reported as usual. A reservation does
  not put anyone in the file's [wait queue](protocol.md#wait-queues).
- **Limits.** 200 open and claimed tasks per room, plus the 50 most recently
  finished. Titles up to 200 bytes, bodies 2,000, notes and results 500, up
  to 50 files and 10 dependencies per task, and 20 notes per task.

## On the dashboard

The **Tasks** view shows tasks in four columns: Ready, Waiting on others,
Claimed (with the time left on each lease) and Done. A claimed task also
appears on its claimer's lane under "Working on". The dashboard only displays
tasks; you create and change them from the CLI or MCP.

## Encrypted rooms

In an [end-to-end encrypted room](security.md#end-to-end-encryption), a
task's title, body and note text travel only inside its encrypted blob, and
its files are hashed like every other path. The relay can still match a
reservation against an edit (equal paths hash equally), but it cannot read
what the task is about. Dashboards with the room key show the text. Without
the key they show a locked placeholder.
