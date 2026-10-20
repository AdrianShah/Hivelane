# Hivelane for VS Code, Cursor and Windsurf

See who else (AI agents and teammates) is working in your repo, and get a warning before you edit a file someone is changing right now.

The extension talks to your local Hivelane daemon (`hivelane up`, default `http://127.0.0.1:4600`). It never talks to the relay with your identity; it only listens to the room as a spectator.

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
| `hivelane.dashboardUrl` | `""` | Base URL of the dashboard. Empty: `HIVELANE_DASHBOARD`, else `http://localhost:5173` for a local relay and `https://hivelane.dev` otherwise. Room links carry `?relay=`. |
| `hivelane.joinApprovalRooms` | `false` | In approval-required rooms, ask an owner to admit this editor as a spectator so badges work. A declined request is not retried. |
| `hivelane.warnOnEdit` | `true` | Run the pre-edit check when you start editing. |
| `hivelane.shareHumanEdits` | `true` | Share your saved edits with the room. |
| `hivelane.tokenPath` | `""` | The daemon's access token file. Empty means `$HIVELANE_HOME/token`, else `~/.hivelane/token`; set it if VS Code runs with a different `HIVELANE_HOME` than the daemon. |

## Install from a .vsix

```sh
cd editors/vscode
npm install
npm run package          # produces hivelane-<version>.vsix
code --install-extension hivelane-0.1.0.vsix
# Cursor:   cursor --install-extension hivelane-0.1.0.vsix
# Windsurf: windsurf --install-extension hivelane-0.1.0.vsix
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

1. Confirm the publisher id in `package.json` (`"publisher": "adrianshah"` is a suggestion, marked TODO in the `//` field) and the `repository` URL (`https://github.com/AdrianShah/Hivelane`).
2. **VS Code Marketplace**: create the publisher at https://marketplace.visualstudio.com/manage. Create an Azure DevOps PAT with the *Marketplace (Manage)* scope, then run `npx vsce login <publisher>` and `npx vsce publish`.
3. **Open VSX**: sign in at https://open-vsx.org with GitHub and sign the Eclipse publisher agreement. Create an access token, then run `npx ovsx create-namespace <publisher> -p <token>` and `npx ovsx publish hivelane-0.1.0.vsix -p <token>`.
4. Optionally add a 128x128 `icon` to `package.json` before you publish.

## License

Apache-2.0, like the rest of Hivelane. See `LICENSE`.
