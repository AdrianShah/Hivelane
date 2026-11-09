# Hivelane

**See which AI agents and teammates are editing your repo, and get warned before you collide.**

When several people or coding agents (Claude Code, Cursor, Codex and others) work in one repository, Hivelane shows who is in which file, live, so you don't find out at merge time.

## Features

- **Who's online.** The status bar shows your room and how many people are in it. Click it for the menu.
- **Peer badges.** Files a teammate or agent edited in the last minute get their initials in the Explorer and on tabs. Hover a badge to see who, when and what they're working on.
- **Edit warnings.** Start typing in a file someone else is changing, and you get a heads-up naming who is on it.
- **Your edits count too.** Saved changes are shared with the room, so agents know where you're working.

## Getting started

1. Install the Hivelane CLI (Python 3.12+):

   ```sh
   pipx install git+https://github.com/AdrianShah/Hivelane
   ```

2. In your repository, run `hivelane up`. It creates or joins the repo's room and writes `.hivelane.toml`. Commit that file so teammates join the same room.
3. Open the repository in VS Code and run **Hivelane: Menu** from the Command Palette. The status bar then shows `Hivelane: ROOM · N online`.

Hivelane stays off until you run one of its commands, so it never runs in projects where you don't use it. If it can't find the CLI, it tells you and links the [install guide](https://github.com/AdrianShah/Hivelane/blob/HEAD/docs/install.md).

## Commands

Open the Command Palette and type **Hivelane**:

| Command | What it does |
|---|---|
| Menu | Everything below, from the status bar |
| Check this file | Ask whether anyone else is on the current file |
| Who's here | List the people and agents in the room |
| Open dashboard | Open the room's live dashboard in your browser |
| Start daemon | Run `hivelane up` in a terminal |
| Reconnect peer badges | Retry the live feed after it stops |

## Settings

| Setting | Default | Description |
|---|---|---|
| `hivelane.warnOnEdit` | `true` | Warn when you start editing a file someone else is on |
| `hivelane.shareHumanEdits` | `true` | Share your saved edits with the room |
| `hivelane.dashboardUrl` | | Dashboard to open room links in |
| `hivelane.joinApprovalRooms` | `false` | In rooms that need an owner's approval, ask to join so badges work |
| `hivelane.port` | `4600` | Port of the local Hivelane daemon |
| `hivelane.tokenPath` | | Daemon token file, if it isn't in `~/.hivelane` |

## Privacy

The extension only talks to the Hivelane daemon on your own machine (`127.0.0.1`). It joins your room as a read-only spectator. It is off in untrusted (Restricted Mode) and virtual workspaces.

Peer badges are not shown in end-to-end encrypted rooms, because file paths are encrypted there.

## Links

[Documentation](https://adrianshah.github.io/Hivelane/docs/) · [Report an issue](https://github.com/AdrianShah/Hivelane/issues) · [Source](https://github.com/AdrianShah/Hivelane)

Apache-2.0 licensed.
