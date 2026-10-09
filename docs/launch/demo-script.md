# Demo video: 45 seconds

Two Claude Code agents work in the same repo. Alice's agent edits `greet()`.
Seconds later Bob's agent tries to edit the same function and is blocked
before the write, with Synergo's real message. Alice's agent carries on. The
dashboard shows the decision on Bob's lane.

Everything on screen is real output. Model thinking time is cut; the block
itself is shown at real speed. The take uses a wider block window (120 s
instead of the default 20 s) so it does not depend on how fast the second
prompt is typed. Say so in the video description.

## Shot list

1920x1080, 30 fps. Left half: two terminals stacked (Alice on top, Bob
below). Right half: the dashboard. Terminal font 18 pt or larger, dark theme.

| Time | Picture | On-screen text (overlay) | Notes |
|------|---------|--------------------------|-------|
| 0:00-0:03 | Both terminals idle in `~/synergo-demo/alice` and `~/synergo-demo/bob`; dashboard empty | Two Claude Code agents. One repo. | |
| 0:03-0:09 | Alice's terminal: `synergo up -y` runs (2x speed). Output ends with `room`, `relay`, `name alice`, `dashboard http://127.0.0.1:8000/r/<CODE>` and the "Share with teammates" line | `synergo up`: hooks, room, daemon | Crop out the owner-token path line if it shows a home directory |
| 0:09-0:12 | Bob's terminal: `synergo up -y`, output shows `joining room <CODE> from .synergo.toml` and `connected as bob (1 teammate online: alice)` | Teammate runs the same command | The dashboard grows a second lane |
| 0:12-0:20 | Alice's terminal: `claude` session, prompt `Add a greeting parameter to greet() in src/hello.py`. The Edit to `src/hello.py` is applied | | Dashboard: Alice's lane shows the prompt and the `src/hello.py` diff |
| 0:20-0:30 | Bob's terminal: prompt `Make greet() in src/hello.py return lowercase text. If Synergo blocks an edit, do not retry; tell me who is in the file.` Claude Code reports the Edit denied by the PreToolUse hook, with the reason below. Zoom to the reason and hold 4 s | Blocked before the write | Real speed, no cut between the Edit attempt and the denial |
| 0:30-0:36 | Alice's terminal: her agent finishes and runs `python src/hello.py`, which prints the new greeting | Alice's agent never stopped | |
| 0:36-0:41 | Dashboard: Bob's lane shows the chip `blocked · src/hello.py` with the scope tag `same function` and `— alice editing`; click `useful`. Then Analytics: Detection quality shows the vote under `same function` | Every decision says why, and the team can vote it useful or noise | If a collision timeline has landed in the dashboard by recording day (see the status table in [README.md](README.md)), show it here instead of Analytics |
| 0:41-0:45 | End card | `pipx install synergo` / `synergo up` / github.com/AdrianShah/Synergo / Works with Claude Code, Codex CLI, Cursor, Gemini CLI. Apache-2.0 | **Pending:** until the PyPI release exists, the first line must read `pipx install git+https://github.com/AdrianShah/Synergo` |

### The block, exactly as Synergo prints it

The relay writes the reason (`_edit_check_reason` in `server/main.py`) and
the Claude Code adapter (`render_ack` in `synergo/adapters/claude_code.py`)
adds the call to action and the peer's task. Claude Code receives it as a
`PreToolUse` denial with this `permissionDecisionReason`:

```text
Synergo: alice's claude-code is editing greet in src/hello.py (14s ago) — wait, or pick another file (their task: "Add a greeting parameter to greet() in src/hello.py")
```

The seconds and the task text will differ in your take. The task is Alice's
last prompt, so keep that prompt short.

This is the wording at commit `28594f8`. Work in progress on per-file wait
queues (uncommitted on 2026-10-09) changes the part after the dash: a block
would also give the agent's place in line, suggest another file to work on
meanwhile, and point to `synergo wait src/hello.py`. Record with the build
you are launching, then copy the exact text from the take into every post
that quotes it (see the status table in [README.md](README.md)).

If you cut a longer version, Bob's agent can then edit `fib()` in the same
file. That is a different function, so it gets a warning, not a block, and
the edit goes ahead:

```text
Synergo: alice's claude-code is editing greet in src/hello.py (40s ago) — different function, safe to proceed — check with them before you overlap (their task: "Add a greeting parameter to greet() in src/hello.py")
```

A warning is shown as a `systemMessage` and added to the model's context; it
never approves or denies the edit itself.

## Reproducible setup (one machine)

Each daemon connection is one member of the room, and the relay never blocks
a member against its own edits. Two agents on one machine therefore need two
daemons, which means two Synergo homes, two ports and two names. Each
terminal's environment selects its own: the hook Claude Code runs inherits
`SYNERGO_PORT` and `SYNERGO_HOME` from the terminal it was started in.

The two clones must share a repo id. With no `origin` remote, the id is the
root commit, which every clone of one history shares. Both clones must also
be on the same branch, or the relay answers "conflict at merge" (a warning)
instead of blocking.

You need: Python 3.12+, Node.js 18+ (to build the dashboard), git, Claude
Code signed in, and a checkout of Synergo at `$SYNERGO_SRC`.

### 1. Install the CLI with the relay extra

```bash
cd "$SYNERGO_SRC"
pipx install --editable ".[relay]"        # after the PyPI release: pipx install "synergo[relay]"
synergo --version
```

It must be a persistent install: the agent hooks run `synergo` from PATH.

### 2. Build the dashboard

```bash
cd "$SYNERGO_SRC/frontend" && npm ci && npm run build
```

`synergo relay` serves `frontend/dist` when it finds it next to the relay
code, which an editable install does.

### 3. Terminal R: a local relay

```bash
mkdir -p ~/synergo-demo
ROOM_CREATE_LIMIT=100 synergo relay --host 127.0.0.1 --state ~/synergo-demo/relay.db
```

`--host 127.0.0.1` keeps it to this machine, so the room code shown on screen
is harmless. `ROOM_CREATE_LIMIT` matters only for rehearsals: by default one
address may create 10 rooms an hour, and every fresh `synergo up` creates
one. The relay prints `dashboard: http://127.0.0.1:8000`.

### 4. The demo repo

```bash
DEMO=~/synergo-demo
rm -rf "$DEMO/alice" "$DEMO/bob" "$DEMO/home-alice" "$DEMO/home-bob"
git init -q "$DEMO/alice"
cp -r "$SYNERGO_SRC/examples/demo_proj/." "$DEMO/alice/"
cd "$DEMO/alice"
printf 'block_window_seconds = 120\nwarn_window_seconds = 300\n' > .synergo.toml
git add -A && git commit -qm "Demo project"
```

The windows are for the recording only (defaults: 20 s and 60 s). They live in
the committed `.synergo.toml`, so both members use them.

### 5. Terminal A: Alice

```bash
export SYNERGO_HOME=~/synergo-demo/home-alice SYNERGO_PORT=4601 SYNERGO_NAME=alice
export SYNERGO_DASHBOARD=http://127.0.0.1:8000
cd ~/synergo-demo/alice
synergo up -y
git add .synergo.toml .claude && git commit -qm "Join the Synergo room"
git clone -q ~/synergo-demo/alice ~/synergo-demo/bob
git -C ~/synergo-demo/bob remote remove origin
```

PowerShell equivalent of the two `export` lines:

```powershell
$env:SYNERGO_HOME="$HOME\synergo-demo\home-alice"; $env:SYNERGO_PORT="4601"; $env:SYNERGO_NAME="alice"
$env:SYNERGO_DASHBOARD="http://127.0.0.1:8000"
```

`synergo up -y` installs the Claude Code hooks into `.claude/settings.json`,
creates the room on the local relay (the default relay is
`ws://localhost:8000/ws`, so no `--relay` is needed), writes the room into
`.synergo.toml` and starts Alice's daemon on port 4601.

### 6. Terminal B: Bob

```bash
export SYNERGO_HOME=~/synergo-demo/home-bob SYNERGO_PORT=4602 SYNERGO_NAME=bob
export SYNERGO_DASHBOARD=http://127.0.0.1:8000
cd ~/synergo-demo/bob
synergo up -y
```

The room comes from the committed `.synergo.toml`.

Rehearse steps 4 to 8 once end to end. For the take, run `synergo down` in
both terminals, repeat step 4 to reset, and film steps 5 and 6 for real: the
first `synergo up -y` creates the room on camera, and the commit and clone
between the two `synergo up` runs happen off camera.

### 7. Check before every take

```bash
synergo doctor        # in both terminals: every check passes
synergo status        # Alice sees peer bob, Bob sees peer alice
synergo open          # in Terminal A: opens http://127.0.0.1:8000/r/<CODE>
```

### 8. Start the agents

In both terminals, from the repo directory:

```bash
claude --permission-mode acceptEdits
```

Accept-edits mode keeps Claude Code's own permission prompt off screen, so the
only interruption in the take is Synergo's. (Shift+Tab in a session switches
to the same mode.) Paste Alice's prompt, wait for her Edit to land, then
paste Bob's.

### If the two-home setup misbehaves

`scripts/smoke.sh` proves the same block with single-room daemons. Use one
`SYNERGO_HOME` for both terminals, run `synergo hook install claude-code` in
each clone, and in each terminal run a foreground daemon:
`synergo join <CODE> --name alice --port 4601` (Bob: `--name bob --port
4602`), keeping `SYNERGO_PORT` set per terminal before starting `claude`.
`synergo join` ignores `.synergo.toml`, so the default 20-second block window
applies: paste Bob's prompt promptly.

### Teardown

```bash
synergo down          # in Terminal A, then in Terminal B (each stops its own daemon)
```

Ctrl+C the relay, then `rm -rf ~/synergo-demo`.

## README GIF: 10 seconds

For the top of the README. 1200 px wide, 12 fps, under 4 MB, no audio, loops.

| Time | Picture | Caption burned in (bottom, 28 px) |
|------|---------|-----------------------------------|
| 0-3 s | Alice's terminal: her Edit to `greet()` lands; dashboard lane shows the diff | Agent A edits greet() |
| 3-7 s | Bob's terminal, cropped to the denial and the full `Synergo: alice's claude-code is editing greet in src/hello.py ...` reason | Agent B is blocked before the write |
| 7-10 s | Dashboard: Bob's lane chip `blocked · src/hello.py` `same function` | Every decision says why |

Cut it from the 45-second master, then:

```bash
ffmpeg -ss 00:00:12 -t 10 -i master.mp4 \
  -vf "fps=12,scale=1200:-1:flags=lanczos,palettegen=stats_mode=diff" -y palette.png
ffmpeg -ss 00:00:12 -t 10 -i master.mp4 -i palette.png \
  -lavfi "fps=12,scale=1200:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5" \
  -y synergo-demo.gif
```

(Adjust `-ss` to wherever the three beats sit in your edit; the 10 seconds
will usually need two cuts, not one continuous range.)

Alt text: "Two Claude Code agents in one repo. The first edits greet() in
src/hello.py. When the second tries to edit the same function, Synergo
blocks the edit and names who is in the file. The dashboard shows the block
on the second agent's lane."

## Video description (for YouTube, Product Hunt and the README)

> Two Claude Code agents in one repository. When the second one tries to edit
> a function the first is changing, Synergo's pre-edit hook blocks the write
> and tells it who is in the file. Recorded on one machine with a local relay
> (`synergo relay`); thinking time is cut, the block is real time. The take
> uses `block_window_seconds = 120` so it does not depend on timing; the
> default is 20 seconds.
