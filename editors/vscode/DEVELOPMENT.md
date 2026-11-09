# Developing the Hivelane extension

This file is for contributors. `README.md` is the Marketplace listing, so keep it user-facing.

## Build and test

```sh
npm install
npm run compile   # tsc -> out/
npm test          # node --test, against a mock daemon and a mock relay
```

To run the extension in an Extension Development Host, open `editors/vscode` in VS Code and press F5. `.vscode/launch.json` compiles first.

## Install from a .vsix

```sh
npm run package   # produces hivelane-<version>.vsix
code --install-extension hivelane-<version>.vsix
# Cursor:   cursor --install-extension hivelane-<version>.vsix
# Windsurf: windsurf --install-extension hivelane-<version>.vsix
```

You can also use the Extensions view: `...` → **Install from VSIX...**.

## Settings details

- `hivelane.port`: folder, workspace and user settings win, in that order. If none is set, `HIVELANE_PORT` from the editor's own environment is used. Editors launched from a GUI often don't inherit it.
- `hivelane.dashboardUrl`: when empty, uses `HIVELANE_DASHBOARD`. Without that, it's `http://localhost:5173` for a local relay and `https://adrianshah.github.io/Hivelane` otherwise. Room links carry `?relay=`.
- `hivelane.tokenPath`: when empty, uses `$HIVELANE_HOME/token`, else `~/.hivelane/token`.
- The relay feed stops instead of retrying when the room is rejected, for example when it has expired or a join was declined. **Reconnect peer badges** restarts it.
- Approval-required rooms get no badges unless `hivelane.joinApprovalRooms` is on. A declined request is never retried.

## Publishing (a human has to do this)

Cursor and Windsurf install from [Open VSX](https://open-vsx.org), not the Microsoft Marketplace, so publish to both. The publisher id is `adrianshah`. The listing is marked `preview` until 1.0. Build the package first with `npm run package`.

1. **VS Code Marketplace**: sign in at https://marketplace.visualstudio.com/manage, create the `adrianshah` publisher, then use **New extension → Visual Studio Code** to upload `hivelane-<version>.vsix`. No token is needed for this.
   - Azure DevOps global PATs (the old `vsce login` route) are retired on 1 December 2026.
   - To publish from CI, use Microsoft Entra ID with `vsce publish --azure-credential` instead.
2. **Open VSX**:
   1. Sign in at https://open-vsx.org with GitHub, sign the Eclipse publisher agreement and create an access token.
   2. Once: `npx ovsx create-namespace adrianshah -p <token>`.
   3. For each release: `npx ovsx publish hivelane-<version>.vsix -p <token>`. Open VSX runs its own security checks before a version goes live.

## Icon

`media/icon.png` comes from the brand assets. `node frontend/scripts/build-brand.mjs` writes `media/icon.svg`. After you change the mascot, rasterize that SVG to a 256 × 256 PNG.
