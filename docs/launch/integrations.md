# Distribution targets

Where Synergo can be listed, what has to exist first, and the exact steps a
person takes to submit. Nothing here has been submitted. Every external
process was read on **2026-10-06**; the pages are listed under
[Sources](#sources). Re-read each page on the day you submit, since these
processes change often.

Order of work, because of dependencies:

1. Public repo and the first PyPI release (P0, P1 in [README.md](README.md)).
   The MCP Registry verifies ownership through the PyPI package's README, so
   add the `mcp-name` line (below) **before** tagging `v0.1.0`, or plan a
   `v0.1.1`.
2. Open VSX and VS Code Marketplace (needs the `package.json` fixes in P4).
3. MCP Registry, then the directories that read from it.
4. Awesome lists (one of them requires the repo to be 14 days old with
   active commits, or to have 100 stars).
5. Claude Code plugin and Gemini CLI extension: both need packaging code
   that does not exist yet.

| Target | Status | Needs code? | Who can submit |
|--------|--------|-------------|----------------|
| [Open VSX](#open-vsx-and-the-vs-code-marketplace) | Ready after P4 fixes | No | Anyone with an Eclipse account and the publisher agreement |
| [VS Code Marketplace](#open-vsx-and-the-vs-code-marketplace) | Ready after P4 fixes | No | Anyone with an Azure DevOps PAT |
| [Official MCP Registry](#mcp-registries) | After the PyPI release | One README line and a `server.json` | GitHub login for the `io.github.AdrianShah` namespace |
| [awesome-mcp-servers](#awesome-lists) | After P0 | No | Pull request |
| [awesome-claude-code](#awesome-lists) | After P0, once eligible | No | A human, through the web issue form only |
| [Claude Code plugin marketplace](#claude-code-plugin) | Not started | Yes | Self-published, no review |
| [Anthropic's plugin directory](#claude-code-plugin) | Not started | Yes, plus the plugin above | A paid claude.ai account with GitHub connected |
| [Gemini CLI extension gallery](#gemini-cli-extension-gallery) | Not started | Yes | Automatic crawl of a tagged public repo |

## Open VSX and the VS Code Marketplace

Cursor and Windsurf install extensions from Open VSX, VS Code from the
Microsoft Marketplace, so publish to both (as `editors/vscode/README.md`
already says).

**Fix first** (`editors/vscode/`):

- `package.json` `publisher` is the placeholder `synergo`. Pick the real
  publisher id; it must match on both registries.
- `package.json` `repository.url` is `https://github.com/synergo/synergo`.
  Change it to `https://github.com/AdrianShah/Synergo`.
- Add a 128x128 PNG `icon`. The Marketplace rejects an SVG icon.
- `LICENSE` is an abbreviated MIT text. The Marketplace requires a LICENSE
  file; make it the full MIT text, or Apache-2.0 to match the repo, and keep
  `package.json` `license` in step.
- Images in the extension README must be `https` URLs, and not SVG unless
  from a trusted badge provider. The current README has no images.
- The default `synergo.dashboardUrl` is `http://localhost:5173`. Consider
  pointing it at the hosted dashboard once it exists (pending).

**Build:**

```bash
cd editors/vscode
npm ci
npm test
npx vsce package            # writes synergo-0.1.0.vsix
```

**Open VSX** (per the Open VSX publishing wiki):

1. Create an Eclipse account at `https://accounts.eclipse.org/user/register`,
   with the GitHub username filled in to match the GitHub account.
2. Sign in to `https://open-vsx.org` with GitHub, open the profile settings,
   choose "Log in with Eclipse", then "Show Publisher Agreement", and agree.
3. Generate an access token at `https://open-vsx.org/user-settings/tokens`
   and store it in a password manager.
4. `npx ovsx create-namespace <publisher> -p <token>`
5. `npx ovsx publish synergo-0.1.0.vsix -p <token>`
6. Creating a namespace does not make you its verified owner. Claim
   ownership through the Namespace Access process the wiki links to, so the
   listing shows as verified.

Open VSX scans uploads for secrets, blocklisted files and namespaces similar
to existing ones; a hit fails the publish.

**VS Code Marketplace** (per the VS Code publishing guide):

1. Create an Azure DevOps organization if you have none.
2. In Azure DevOps, User settings > Personal access tokens > New Token,
   Organization "All accessible organizations", Scopes "Custom defined" >
   Marketplace > Manage.
3. Create the publisher at `https://marketplace.visualstudio.com/manage`
   (Create publisher; the ID must equal `package.json` `publisher`).
4. `npx vsce login <publisher>` (paste the PAT), then `npx vsce publish`.

Check: `code --install-extension <publisher>.synergo` and, in Cursor, a
search for "Synergo" in the Extensions view.

## MCP registries

`synergo mcp` is a stdio MCP server for agents without native hooks, with
four tools: `check_file`, `declare_intent`, `who_is_here` and `report_edit`.
It talks to the local daemon, so a user still needs `synergo up` in the repo.
Be clear about that in every listing: it only helps when the agent calls
`check_file` before editing (`synergo mcp --agents-md` prints instructions
for `AGENTS.md` / `CLAUDE.md` that tell it to).

### Official MCP Registry

Per the registry's quickstart and package-type docs:

1. **Ownership line in the PyPI README.** The registry looks for
   `mcp-name: <server name>` in the package README, which becomes the PyPI
   description. Add a hidden line to the repo's `README.md` (the PyPI
   readme per `pyproject.toml`), then release:

   ```markdown
   <!-- mcp-name: io.github.AdrianShah/synergo -->
   ```

2. **Install the publisher CLI:** `brew install mcp-publisher`, or the
   release binary from `github.com/modelcontextprotocol/registry/releases`.
3. **Create `server.json`** at the repo root (`mcp-publisher init` writes a
   template). Draft, to adjust to the version you release:

   ```json
   {
     "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
     "name": "io.github.AdrianShah/synergo",
     "title": "Synergo",
     "description": "Ask whether a teammate's AI agent is already editing a file before you edit it. Needs the Synergo daemon (synergo up).",
     "version": "0.1.0",
     "packages": [
       {
         "registryType": "pypi",
         "registryBaseUrl": "https://pypi.org",
         "identifier": "synergo",
         "version": "0.1.0",
         "runtimeHint": "uvx",
         "packageArguments": [{ "type": "positional", "value": "mcp" }],
         "transport": { "type": "stdio" }
       }
     ]
   }
   ```

   The name must use the exact namespace `mcp-publisher login github`
   grants (the GitHub account, `io.github.<login>`); check its casing after
   logging in, and keep the README line identical.
4. **Log in:** `mcp-publisher login github`, then complete the device-code
   flow in the browser.
5. **Publish:** `mcp-publisher publish`.
6. **Verify:**
   `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.AdrianShah/synergo"`

`server.json` is outside `docs/launch/` and was not created here. Bump its
two `version` fields with every release that should reach the registry.

### Directories that follow the registry

- **PulseMCP** is not accepting submissions at the moment and says it will
  pick servers up from the official registry automatically once it reopens.
  Nothing to do beyond the registry.
- **Glama** lists MCP servers and has an "Add Server" button on
  `https://glama.ai/mcp/servers`. The page did not document the steps or how
  ownership is claimed; open the button on the day and follow it.

## Awesome lists

### awesome-mcp-servers (`punkpeye/awesome-mcp-servers`)

Per its CONTRIBUTING.md: the server must have a public GitHub repository
that people install and run themselves (Synergo qualifies). Fork, add one
line to `README.md` in the right category in alphabetical order, open a pull
request. Match the existing line format exactly, including any legend
markers the README uses for language and scope. Suggested description:

> Lets agents without native hooks ask whether a teammate's agent is already
> editing a file (`check_file`), declare intent and see who is in the repo.
> Needs the Synergo daemon.

### awesome-claude-code (`hesreallyhim/awesome-claude-code`)

Per its CONTRIBUTING.md:

- Submit only through the web issue form "recommend a resource":
  `https://github.com/hesreallyhim/awesome-claude-code/issues/new?template=recommend-resource.yml`.
  Pull requests are not accepted, and the form cannot be submitted with the
  `gh` CLI.
- Recommendations must be made by a human (the resource itself may be
  written with an agent). This one cannot be delegated to an agent.
- The resource must be at least 14 days old with active commits, or have at
  least 100 stars. One recommendation at a time.
- Descriptions: one line, a description rather than a sales pitch, not
  addressed to the reader, no emoji.

Suggested description:

> Pre-edit hooks that check a shared relay before each edit and block or
> warn when another developer's agent is changing the same function;
> includes a dashboard and a self-hostable relay.

## Claude Code plugin

**What exists today:** nothing. `synergo up` writes Claude Code hooks into
each repo's `.claude/settings.json` instead.

**What a plugin would be:** a directory with `.claude-plugin/plugin.json`
and `hooks/hooks.json`, where `hooks/hooks.json` holds the same entries
`hook_settings()` in `synergo/adapters/claude_code.py` produces
(`UserPromptSubmit`; `PreToolUse` matching `Edit|Write|MultiEdit|NotebookEdit`
with a 5 s timeout; `PostToolUse` matching those plus
`TodoWrite|ExitPlanMode`), all running `synergo hook claude-code`.

**Code needed before it is worth publishing:**

- A plugin cannot install the Python CLI, so `synergo` must already be on
  PATH. The plugin's README, and ideally a session-start hook, has to say so.
- An installed plugin is enabled in every project, so its hooks would run
  in repos that do not use Synergo. Today the hook shows a "Synergo offline"
  notice (at most every ten minutes) whenever the daemon is unreachable, and
  it costs a process start per edit everywhere. The hook should exit
  silently, before any network call, when the working directory has no
  `.synergo.toml` above it.
- A repo set up with `synergo up` also has the project-level hooks, so both
  would fire on every edit. Either `synergo up` skips Claude Code hooks when
  the plugin is installed, or the hook deduplicates.

**Your own marketplace (no review):** add
`.claude-plugin/marketplace.json` next to the plugin, for example in a
`plugins/claude-code/` directory of this repo:

```json
{
  "name": "synergo",
  "owner": { "name": "Synergo contributors" },
  "plugins": [
    { "name": "synergo", "source": "./plugins/claude-code", "description": "Pre-edit coordination hooks for Synergo rooms" }
  ]
}
```

Then: `claude plugin validate --strict ./plugins/claude-code`,
`claude plugin validate .`, test with
`claude plugin marketplace add ./` and `claude plugin install synergo@synergo`,
push, and tell users to run
`claude plugin marketplace add AdrianShah/Synergo` followed by
`claude plugin install synergo@synergo`. Marketplace names such as
`claude-plugins-official` are reserved for Anthropic.

**Anthropic's directory (reviewed):** Anthropic's official marketplace does
not take submissions through the directory portal; the public route is the
directory at claude.ai. Steps:

1. Work through the plugin pre-submission checklist on claude.com and run
   `claude plugin validate ./<plugin-folder> --strict`.
2. Connect the GitHub account (with push access to the repo) in the
   claude.ai organization you submit from. A paid plan is required (Pro or
   Max from your own account; an Owner on Team or Enterprise).
3. Open `https://claude.ai/directory/manage`, "Submit new", "Plugin bundle".
4. Source: repository `AdrianShah/Synergo`, plugin path
   `plugins/claude-code`, tracked branch or tag. Validate, fix, validate
   again.
5. Listing details come from `plugin.json` and the plugin README.
6. Data handling: answer truthfully that the plugin sends prompts, file
   paths, symbols and (unless `share_diffs = false`) diffs to the relay the
   repo's `.synergo.toml` names, which is a service other than a declared
   connector.
7. Compliance: contact email and the four acknowledgements; then "Submit
   for review", and set up the GitHub push webhook if you have repo admin.
8. A passing version still needs "Publish"; by default an Anthropic
   reviewer publishes it.

Hooks are a Claude Code component. Check the directory's component support
table: people who add the plugin on claude.ai or in Cowork may get nothing
useful from it, and the listing should say it is for Claude Code.

## Gemini CLI extension gallery

**What exists today:** nothing; `synergo up` writes `.gemini/settings.json`
hooks per repo.

**How the gallery works:** a public GitHub repo with `gemini-extension.json`
at the absolute root of the repo (or of the release archive) and the
`gemini-cli-extension` topic is crawled daily and listed if it passes
validation. Extensions can bundle hooks in `hooks/hooks.json` and refer to
their own files with `${extensionPath}`.

**Code needed:** the manifest must sit at a repo root, so this is best done
as a small separate repo (for example `AdrianShah/synergo-gemini`) holding
`gemini-extension.json` and `hooks/hooks.json` with the entries
`hook_settings()` in `synergo/adapters/gemini_cli.py` produces. It has the
same caveats as the Claude Code plugin: `synergo` must be on PATH, the hook
should stay silent outside repos with a `.synergo.toml`, and repos set up
with `synergo up` would otherwise run the hooks twice.

## Not researched

Codex CLI and Cursor do not appear to have a public hook or extension
gallery that fits; this was not checked in depth. The Anthropic directory's
"MCP connector" listing is for remote MCP servers, and Synergo's MCP server
is a local stdio process that needs a local daemon, so it was not pursued.

## Sources

All read on 2026-10-06.

| Page | Used for |
|------|----------|
| [code.claude.com/docs/en/plugins](https://code.claude.com/docs/en/plugins) | Plugin structure, official marketplace tiers |
| [code.claude.com/docs/en/plugin-marketplaces](https://code.claude.com/docs/en/plugin-marketplaces) | `marketplace.json`, validate, add, install, reserved names |
| [code.claude.com/docs/en/plugins/publish](https://code.claude.com/docs/en/plugins/publish) | Distribution routes, Anthropic's directory, official marketplace not taking portal submissions |
| [code.claude.com/docs/en/plugins/components](https://code.claude.com/docs/en/plugins/components) | `hooks/hooks.json` format, `${CLAUDE_PLUGIN_ROOT}` |
| [claude.com/docs/plugins/submit](https://claude.com/docs/plugins/submit) | Developer portal steps, data handling, review and publish |
| [MCP Registry quickstart](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx) | `mcp-publisher`, `server.json`, login, publish, verify |
| [MCP Registry package types](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx) | PyPI `registryType`, `mcp-name` README verification |
| [MCP Registry generic server.json](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md) | `runtimeHint`, positional `packageArguments` |
| [pulsemcp.com/submit](https://www.pulsemcp.com/submit) | Submissions paused; ingests the official registry |
| [glama.ai/mcp/servers](https://glama.ai/mcp/servers) | "Add Server" entry point |
| [Open VSX: Publishing Extensions](https://github.com/eclipse/openvsx/wiki/Publishing-Extensions) | Eclipse account, agreement, token, namespace, publish, scanning |
| [VS Code: Publishing Extensions](https://code.visualstudio.com/api/working-with-extensions/publishing-extension) | PAT scope, publisher, `vsce`, icon/README/LICENSE rules |
| [awesome-mcp-servers CONTRIBUTING.md](https://github.com/punkpeye/awesome-mcp-servers/blob/main/CONTRIBUTING.md) | PR process and scope |
| [awesome-claude-code CONTRIBUTING.md](https://github.com/hesreallyhim/awesome-claude-code/blob/main/CONTRIBUTING.md) | Issue-form-only, human-only, eligibility rules |
| [geminicli.com/docs/extensions/releasing](https://geminicli.com/docs/extensions/releasing/) | Gallery listing by topic and root manifest |
| [geminicli.com/docs/extensions/reference](https://geminicli.com/docs/extensions/reference/) | Extensions can bundle hooks, `${extensionPath}` |
