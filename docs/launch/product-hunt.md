# Product Hunt

Limits from Product Hunt's
[preparing-for-launch checklist](https://www.producthunt.com/launch/preparing-for-launch)
(read 2026-10-06): tagline at most 60 characters, description at most 500,
square thumbnail (240x240 recommended, under 3 MB), at least two gallery
images at 1270x760, video as a full YouTube URL. Its
[launch guide](https://www.producthunt.com/launch) has one hard rule: you
cannot ask people directly to upvote; ask them to visit and comment.

Launch at 12:01 am PT (the checklist's recommendation), on D+3 after Show HN,
once the demo video exists ([demo-script.md](demo-script.md)).

Topics (pick three): Developer Tools, Artificial Intelligence, Open Source.

## Tagline

| Chars | Option |
|------:|--------|
| 53 | Stop your AI coding agents from clobbering each other |
| 55 | Stops parallel coding agents from editing the same code |
| 53 | Keep parallel AI agents out of each other's functions |
| 56 | Your agents ask before they edit code someone else is in |

Recommended: the first. It is the positioning line, shortened.

## Description (433 characters)

> Synergo is an open-source add-on for teams running several AI coding agents
> in one repo. Before an agent edits a file, its own hook asks a shared relay
> whether a teammate's agent is already in that function, and gets allow,
> warn or block with a reason. Hooks for Claude Code, Codex CLI, Cursor and
> Gemini CLI, in the editor you already use. One command per repo: synergo
> up. Self-host the relay; encrypt rooms end to end if you like.

The first 260 characters end at "...allow, warn or block with a reason.",
which works on its own if the preview cuts there.

## Links

- Website: the GitHub repo until the docs site is live (pending, P5 in
  [README.md](README.md)).
- Video: the 45-second demo on YouTube.
- Pricing field: "Free" (Apache-2.0, self-hostable). Do not mention paid
  plans; [pricing-draft.md](pricing-draft.md) is undecided.

## First comment (from the maker)

> Hi Product Hunt. Synergo started at a hackathon as a live board of what
> every teammate's AI agent was doing, fed by a file watcher. It kept
> telling us about collisions after both agents had already written the same
> file, which is too late to matter. So we rebuilt it around the agents' own
> hooks.
>
> Now each agent asks before every edit: is anyone else in this file? A
> small relay answers from what the rest of the team's agents are doing:
>
> - same function, edited seconds ago: block, and the agent is told who and
>   why, so it waits or picks other work;
> - different function in the same file: a warning, the edit goes ahead;
> - same code on another branch: "this will conflict at merge";
> - a file a teammate's plan says they are about to change: a warning.
>
> It works in the editor and agent you already use: blocking hooks for
> Claude Code, Codex CLI, Cursor and Gemini CLI, observe-only fallbacks for
> agents without hooks (Aider through `synergo run`, an MCP server, a git
> pre-commit check), and a VS Code/Cursor extension that warns humans too.
> Setup is `synergo up` in the repo; teammates pull and run the same command.
>
> Some honest limits: agents without pre-edit hooks can only be observed,
> not stopped; if the relay is down, edits go ahead (it fails open); and
> whoever runs the relay sees prompts and diffs unless you turn on
> end-to-end encryption, which hides content but not who is working when.
> The relay is one small process, so you can run your own.
>
> Who it is for: anyone running two or more agents in one repo, alone or as
> a team, and hackathon teams.
>
> I would love to hear where it gets in your way: decisions you think were
> wrong, agents you want supported, and what you would need before using it
> at work.

Edit before posting: if a hosted relay exists by launch day (pending), add
one sentence on how to use it; if the editor extension is not on the
marketplaces yet (pending), drop "and a VS Code/Cursor extension that warns
humans too".

## Gallery (1270x760, in this order)

The first image is the one shown in feeds and social shares, so it must show
the block.

| # | Image | Caption |
|---|-------|---------|
| 1 | Split screen from the demo at the moment of the block: Bob's terminal with `Synergo: alice's claude-code is editing greet in src/hello.py ... wait, or pick another file`, Alice's terminal with her finished edit | Agent B is blocked before it edits the function Agent A is changing |
| 2 | Alice's `synergo up` output: hooks added, room created, dashboard link, share line | One command per repo: hooks, room and a background daemon |
| 3 | Dashboard with both lanes: prompts, the `src/hello.py` diff, branch badges | Every agent's prompt, plan, diffs and decisions on one board |
| 4 | Close-up of a decision chip: `blocked · src/hello.py`, scope tag `same function`, useful/noise buttons | Each decision says why, and the team can mark it useful or noise |
| 5 | Simple table: Claude Code, Codex CLI, Cursor, Gemini CLI (block); Aider, MCP agents, git pre-commit (observe or warn); VS Code/Cursor extension (warns humans) | Blocking hooks for four agents, fallbacks for the rest |
| 6 | Terminal: `synergo relay` output with the join line, next to `synergo up --e2e` printing the room key notice | Run the relay yourself, or encrypt rooms end to end |

Thumbnail: the logo (`docs-site/src/assets/logo.svg`) exported as a 240x240
PNG.

Do not use the words "timeline" or "heat map" in captions unless those views
exist in the dashboard on launch day (see the status table in
[README.md](README.md)).
