# Synergo for VS Code, Cursor and Windsurf

See who else (AI agents and teammates) is working in your repo, and get a warning before you edit a file someone is changing right now.

The extension talks to your local Synergo daemon (`synergo up`, default `http://127.0.0.1:4600`). It never talks to the relay with your identity; it only listens to the room as a spectator.

## Features

- **Status bar**: shows `Synergo: ROOM · N online` or `Synergo: offline`. Click it to open the dashboard (`<dashboardUrl>/r/ROOM`), show peers, run `synergo up` in a terminal, or open the docs. The status refreshes every 5 s.
- **Peer file badges**: any file a peer edited in the last 60 s gets a badge with their initials in the Explorer and tabs. The tooltip reads like `sam (claude-code) edited 12 s ago: <their intent>`.
- **Edit warnings**: the first time you change a file (at most once per file per minute), the extension asks the daemon for a `pre_edit` check. If the daemon answers warn or block, you get a non-modal notification that says who is on the file and why, with an **Open dashboard** button.
- **Shared human edits**: when you save, your diff is sent as a `post_edit`, so teammates and agents see your edits in the room too.
- **Commands**: `Synergo: Check this file`, `Synergo: Who's here`, `Synergo: Open dashboard`, `Synergo: Start daemon (synergo up)`.

## Settings

| Setting | Default | |
|---|---|---|
| `synergo.port` | `4600` | Daemon port. If you don't set it, `SYNERGO_PORT` is used. |
| `synergo.dashboardUrl` | `http://localhost:5173` | Base URL of the dashboard. |
| `synergo.warnOnEdit` | `true` | Run the pre-edit check when you start editing. |
| `synergo.shareHumanEdits` | `true` | Share your saved edits with the room. |
| `synergo.tokenPath` | `""` | The daemon's access token file. Empty means `$SYNERGO_HOME/token`, else `~/.synergo/token`; set it if VS Code runs with a different `SYNERGO_HOME` than the daemon. |

## Install from a .vsix

```sh
cd editors/vscode
npm install
npm run package          # produces synergo-<version>.vsix
code --install-extension synergo-0.1.0.vsix
# Cursor:   cursor --install-extension synergo-0.1.0.vsix
# Windsurf: windsurf --install-extension synergo-0.1.0.vsix
```

You can also install it from the UI: Extensions view, then `...`, then **Install from VSIX...**.

## Development

```sh
npm install
npm run compile   # tsc -> out/
npm test          # node --test, against a mock daemon and a mock relay
```

To run it in an Extension Development Host, open `editors/vscode` in VS Code and press F5.

## Publishing (a human has to do this)

Cursor and Windsurf install extensions from [Open VSX](https://open-vsx.org), not from the Microsoft Marketplace, so publish to both.

1. Pick a publisher id and set `"publisher"` in `package.json` (it is currently a placeholder, `synergo`). Also update the `repository` URL.
2. **VS Code Marketplace**: create the publisher at https://marketplace.visualstudio.com/manage. Create an Azure DevOps PAT with the *Marketplace (Manage)* scope, then run `npx vsce login <publisher>` and `npx vsce publish`.
3. **Open VSX**: sign in at https://open-vsx.org with GitHub and sign the Eclipse publisher agreement. Create an access token, then run `npx ovsx create-namespace <publisher> -p <token>` and `npx ovsx publish synergo-0.1.0.vsix -p <token>`.
4. Optionally add a 128x128 `icon` to `package.json` before you publish.
