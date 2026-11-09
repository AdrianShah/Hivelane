# Hivelane for VS Code, Cursor and Windsurf

See who else (AI agents and teammates) is working in your repo, and get a warning before you edit a file someone is changing right now.

The extension talks to your local Hivelane daemon (`hivelane up`, default `http://127.0.0.1:4600`). It never talks to the relay with your identity; it only listens to the room as a spectator.

## Requirements

- The `hivelane` CLI on your `PATH` (Python 3.12+): `pipx install git+https://github.com/AdrianShah/Hivelane`. Other options are in [docs/install.md](https://github.com/AdrianShah/Hivelane/blob/HEAD/docs/install.md). If the extension can't find it, it says so once and links the install steps.
- A repository with a `.hivelane.toml` (created by `hivelane up`). The extension starts only in workspaces that contain one; its commands work anywhere.
- A trusted, local workspace. The extension is off in Restricted Mode and in virtual workspaces.

## Features

- **Status bar**: shows `Hivelane: ROOM · N online` or `Hivelane: offline`. Click it to open the dashboard (`<dashboardUrl>/r/ROOM`), show peers, run `hivelane up` in a terminal, or open the docs. The status refreshes every 5 s.
- **Peer file badges**: any file a peer edited in the last 60 s gets a badge with their initials in the Explorer and tabs. The tooltip reads like `sam (claude-code) edited 12 s ago: <their intent>`. Badges are not available in end-to-end encrypted rooms (paths are encrypted) and, by default, in rooms that require an owner's approval (see `hivelane.joinApprovalRooms`); the status bar tooltip says so. If the relay rejects the feed (room expired, declined) it stops instead of retrying; run **Hivelane: Reconnect peer badges** after fixing it.
- **Edit warnings**: the first time you change a file (at most once per file per minute), the extension asks the daemon for a `pre_edit` check. If the daemon answers warn or block, you get a non-modal notification that says who is on the file and why, with an **Open dashboard** button.
- **Shared human edits**: when you save, your diff is sent as a `post_edit`, so teammates and agents see your edits in the room too.
- **Commands**: `Hivelane: Check this file`, `Hivelane: Who's here`, `Hivelane: Open dashboard`, `Hivelane: Start daemon (hivelane up)`.

## Settings

| Setting | Default | |
|---|---|---|
| `hivelane.port` | `4600` | Daemon port. Folder, workspace and user settings win in that order; unset, `HIVELANE_PORT` from the editor's own environment is used (GUI-launched editors often don't inherit it). |
| `hivelane.dashboardUrl` | `""` | Base URL of the dashboard. Empty: `HIVELANE_DASHBOARD`, else `http://localhost:5173` for a local relay and `https://adrianshah.github.io/Hivelane` otherwise. Room links carry `?relay=`. |
| `hivelane.joinApprovalRooms` | `false` | In approval-required rooms, ask an owner to admit this editor as a spectator so badges work. A declined request is not retried. |
| `hivelane.warnOnEdit` | `true` | Run the pre-edit check when you start editing. |
| `hivelane.shareHumanEdits` | `true` | Share your saved edits with the room. |
| `hivelane.tokenPath` | `""` | The daemon's access token file. Empty means `$HIVELANE_HOME/token`, else `~/.hivelane/token`; set it if VS Code runs with a different `HIVELANE_HOME` than the daemon. |

## Install from a .vsix

```sh
cd editors/vscode
npm install
npm run package          # produces hivelane-<version>.vsix
code --install-extension hivelane-0.2.0.vsix
# Cursor:   cursor --install-extension hivelane-0.2.0.vsix
# Windsurf: windsurf --install-extension hivelane-0.2.0.vsix
```

You can also install it from the UI: Extensions view, then `...`, then **Install from VSIX...**.

## Development

```sh
npm install
npm run compile   # tsc -> out/
npm test          # node --test, against a mock daemon and a mock relay
```

To run it in an Extension Development Host, open `editors/vscode` in VS Code and press F5 (`.vscode/launch.json` compiles first).

## Publishing (a human has to do this)

Cursor and Windsurf install extensions from [Open VSX](https://open-vsx.org), not from the Microsoft Marketplace, so publish to both.

The publisher id is `adrianshah` and both registries need it. The listing is marked `preview` until 1.0. Build the package first with `npm run package`, then publish it to each registry.

1. **VS Code Marketplace**: sign in at https://marketplace.visualstudio.com/manage, create the `adrianshah` publisher, then use **New extension → Visual Studio Code** to upload `hivelane-<version>.vsix`. No token is needed for this. Azure DevOps global PATs (the old `vsce login` route) are retired on 1 December 2026. To publish from CI, use Microsoft Entra ID with `vsce publish --azure-credential` instead.
2. **Open VSX**: sign in at https://open-vsx.org with GitHub, sign the Eclipse publisher agreement and create an access token. Then run `npx ovsx create-namespace adrianshah -p <token>` once and `npx ovsx publish hivelane-<version>.vsix -p <token>` for each release. Open VSX runs its own security checks before a version goes live.

The icon is `media/icon.png`. It is generated with the other brand assets by `node frontend/scripts/build-brand.mjs`, which writes `media/icon.svg`. Rasterize that SVG to a 256 × 256 PNG after you change the mascot.

## License

Apache-2.0, like the rest of Hivelane. See `LICENSE`.
