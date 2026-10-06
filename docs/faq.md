# FAQ

## What problem does Synergo solve?

When several AI coding agents (yours, your teammates', or several of your
own) work in one repository at the same time, they edit the same files
without knowing about each other. You find out at merge time, or later.
Synergo lets each agent ask "is anyone else in this file?" before every edit,
and warns or blocks it when a teammate's agent already is. See
[Conflict detection](conflict-detection.md) for how the answer is worked out.

## Which agents does it work with?

| Agent | How | Can block an edit? |
|-------|-----|--------------------|
| Claude Code | Native hooks (`synergo hook claude-code`) | Yes |
| Codex | Native hooks (`synergo hook codex`) | Yes |
| Cursor | Native hooks (`synergo hook cursor`) | Yes, for agent edits |
| Gemini CLI | Native hooks (`synergo hook gemini-cli`) | Yes |
| Aider, or any other tool | `synergo run -- <command>`, `synergo watch` | No, observe-only |
| Any MCP-capable agent | `synergo mcp` (check_file, declare_intent, ...) | Only if the agent calls the tool |
| Humans in VS Code, Cursor or Windsurf | The [editor extension](https://github.com/AdrianShah/Synergo/tree/main/editors/vscode) | No, it warns |

`synergo up` detects the agents installed on your machine and installs their
hooks. Support for another agent is one adapter module; see
[Writing an agent adapter](adapters.md).

## Do I need to run a server?

You need a relay that everyone in the room can reach. It is one small
FastAPI process. For a team on one network, or a hackathon, `synergo relay`
runs one in a terminal (it needs the `relay` extra:
`pip install "synergo[relay]"`). For a team across the internet, deploy the
Docker image; [Self-hosting the relay](self-hosting.md) covers the options.
Each person also runs a background daemon, but `synergo up` starts and
manages that for you.

## Does it slow my agent down?

A little. Each hook call starts a small Python process that asks the local
daemon over loopback, so it costs about one interpreter start plus a round
trip. The hook path imports only the standard library, and the tests fail if
a change pulls anything heavier in. `scripts/bench_hook.py` prints the
numbers for your machine next to a bare `python -c pass`. A pre-edit check
waits at most 1.5 seconds for the relay before it gives up and lets the edit
through.

## What happens when the relay or the daemon is down?

The edit goes ahead. Synergo fails open: a hook that cannot reach the daemon,
a daemon that cannot reach the relay, or any error in Synergo itself means
"allow". So that coordination never stops silently, the agent shows a
"Synergo offline" notice, at most once every ten minutes. If the repo has a
`.synergo.toml`, the hook also starts the daemon in the background so the
next edit is checked again.

## What leaves my machine?

For each edit: the file path (relative to the repo root), the diff, the
names of the functions and classes it touches, line ranges, the branch, and an
opaque repo id (a hash of the origin URL, or the id of the first commit). For each prompt: the prompt text,
shared as the agent's intent. All of it goes to the relay your
`.synergo.toml` names, and from there to the room's members and dashboards.

You can narrow that:

- `share_diffs = false` in `.synergo.toml` sends which files changed and
  keeps the diffs on your machine.
- `ignore = [...]` keeps matching paths out of coordination entirely.
- Secret redaction and the end-to-end encryption mode, in which the relay only
  sees ciphertext, are described in [Security](security.md).

If the relay's operator sets `GEMINI_API_KEY`, the relay also sends prompts
and diffs to Google's Gemini API to write captions and explain conflicts.
Without the key, captions are placeholders and conflicts are labelled by
heuristics. Ask whoever runs your relay, or run your own.

## Who can join my room?

Anyone with the room code, like a document share link. Codes are eight
characters, rate-limited against guessing, and expire a week after the room
is created. A room can also require the owner to approve each new member. See
[Security](security.md) for the trust model and the controls.

## Is the daemon reachable from the network?

No. It listens on `127.0.0.1` only (port 4600, or `SYNERGO_PORT`), and its
management endpoints refuse cross-origin requests from web pages. Do not
tunnel or forward that port.

## Will it block me for no reason?

It tries hard not to. The relay compares functions and line ranges, not just
file names: two agents editing different functions of one file get a warning,
not a block, and only a recent edit to the same function, or to lines close
by, blocks. Shared files such as dependency manifests and lockfiles are the
exception: two edits to different parts of them still count as a collision.
Every decision comes with a reason and a scope (`symbol`, `lines`, `file`,
`intent`, `plan`, `branch`), and the dashboard lets the room vote each one
useful or noise.
[Conflict detection](conflict-detection.md) has the full decision table.

## How do I change how strict it is?

The conflict windows live in the committed `.synergo.toml`, so the whole team
shares them:

```toml
block_window_seconds = 20   # a peer's edit this recent can block (0: never block)
warn_window_seconds = 60    # this recent still warns
```

The relay applies the windows the most recently joined member brings, and
caps them (at most 600 and 3600 seconds) so one member cannot switch blocking
off for the room by accident. Setting the block window to 0 still warns.

## Does it work with branches and worktrees?

Yes. Two agents on different branches editing the same code get a warning
that the work will conflict at merge, rather than a block. Agents in
different directories of one checkout are matched by paths relative to the
repo root, and the repo id keeps unrelated repositories that share a room
apart.

## Can one daemon serve several repositories?

Yes. One background daemon per machine holds a relay connection for every
repo you have run `synergo up` in, and routes each hook to the right room by
the repo the agent is working in. `synergo status` lists them.

## Does it replace git, code review or merge tools?

No. Synergo works before an edit lands, while git, review and merge tools work
after. It reduces the number of conflicts that reach a merge; it does not
resolve the ones that do. See [Approaches compared](comparison.md).

## Which operating systems are supported?

macOS, Linux and Windows. CI runs the full test suite on all three with
Python 3.12 and 3.13.

## How do I remove it?

`synergo leave` disconnects a repo from its room, `synergo down` stops the
daemon, and `synergo hook uninstall <agent>` removes an agent's hooks.
[Installing Synergo](install.md#uninstall) covers removing the CLI.

## Is it free? What is the license?

Synergo is open source under the Apache License 2.0. You can self-host every
part of it.

## Where do I report a bug or a security problem?

Bugs and feature requests go in
[GitHub issues](https://github.com/AdrianShah/Synergo/issues/new/choose).
Security problems go through a private
[security advisory](https://github.com/AdrianShah/Synergo/security/advisories/new),
never a public issue; see the
[security policy](https://github.com/AdrianShah/Synergo/blob/main/SECURITY.md).
