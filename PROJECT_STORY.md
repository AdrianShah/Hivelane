## Inspiration

Hackathons are supposed to feel like shared momentum. In practice, once everyone opens an AI coding agent, the room goes quiet — and dangerous. Two people refactor the same handler. Someone “quickly” rewrites a file another agent just touched. You find out at merge time, or worse, during the demo.

We wanted the opposite of that silence: a **live spectator board** for multiplayer AI work. Something that feels like Cursor and Linear met a sports broadcast — every agent’s prompts, diffs, and collisions visible at a glance, so the team can course-correct *before* the PR fight.

The spark was simple: if AI agents are teammates now, they deserve the same situational awareness humans get from sitting shoulder-to-shoulder.

## What it does

**Synergo** is a real-time multiplayer AI agent viewer.

- Create a **shared room** (a short code, Docs-style trust — no accounts).
- Run a **watcher** on each laptop (or launch one from the optional local companion).
- The **dashboard** shows per-agent lanes: last prompt, AI caption, expandable git-style diffs, and live connection status.
- When two agents edit the **same file** within a short window, Synergo raises a **conflict alert** with Resolve / View Overlap actions.
- Optional **Gemini** captions summarize what changed; conflict classification labels overlaps as overlapping / related / unrelated.

In one sentence: *Synergo catches overlapping AI edits before they become merge conflicts.*

## How we built it

Synergo is four cooperating pieces:

1. **Relay (`server/`)** — FastAPI + WebSockets. In-memory rooms (`POST /rooms`, `WS /ws/{room_id}`), spectator joins, activity broadcast, best-effort Gemini captions and conflict evaluation.
2. **Watcher (`watcher/`)** — Python CLI using `watchdog` + unified diffs, 1.5s debounce batching, Cursor prompt capture from local `state.vscdb` (best-effort), plus prompt-only polling so chat updates appear without waiting for a save.
3. **Dashboard (`frontend/`)** — React + Vite. Room create/join, dark developer UI, agent lanes, conflict banner, analytics/history, Deploy Agent / companion wiring.
4. **Companion (`companion/`)** — localhost-only helper (`127.0.0.1:4500`) that starts/stops watchers from the UI so teammates don’t need a terminal. Never exposed publicly.

Deploy split: relay on Render (`wss://…`), dashboard on Vercel (`VITE_WS_URL`), companion stays on each machine.

Data flow (conceptually):

\[
\text{IDE save / prompt} \rightarrow \text{Watcher} \xrightarrow{\text{activity}} \text{Relay} \xrightarrow{\text{broadcast}} \text{Dashboard lanes + conflict radar}
\]

## Challenges we ran into

- **Prompt capture is undocumented.** Cursor’s SQLite layout (`state.vscdb`, composer bubbles) is version-sensitive. We treated capture as best-effort: failures return `null`, diffs still flow.
- **Prompts ≠ file events.** Early on, writing a prompt did nothing on the board until a file saved. We added prompt-only activity updates and taught the UI to merge empty `changes: []` without wiping prior diffs.
- **Room-based deploy vs local CLI.** The live relay moved to `/ws/{ROOM}` while older watchers still hit bare `/ws` (403). We added `--room`, URL helpers, and clearer errors.
- **Free-tier WebSocket flakiness.** Render proxies and busy loops dropped connections; we tuned ping intervals/timeouts and reconnect backoff.
- **AI on the hot path is risky.** Gemini timeouts can’t block broadcasts. Captions and conflict classification run best-effort with heuristics/fallbacks when keys or networks fail.
- **Security for “start a process from the browser.”** The companion must never bind to `0.0.0.0` or be tunneled — spawn APIs are localhost-only by design.

## Accomplishments that we're proud of

- A **complete vertical slice**: room → watcher → live lanes → conflict banner, demoable end-to-end.
- A **glanceable UI** that feels like a real developer product (Synergo branding, hierarchy, Resolve / View Overlap).
- **Agent-aware** labeling (`cursor`, `claude-code`, `codex`) without locking the protocol.
- **Graceful degradation**: no Gemini key? Still works. Prompt capture miss? Diffs still show. Companion offline? Copy a CLI command.
- A **shareable trust model** that matches how hackathon teams actually collaborate: one room code.

## What we learned

- Multiplayer AI tooling is less about smarter models and more about **shared state and visibility**.
- Undocumented local IDE databases are powerful — and fragile. Design for `null`.
- WebSocket products need reconnect, rate limits, and “never block the broadcast loop” as first-class requirements.
- Separating **relay** (public) from **companion** (private spawn) is the right security boundary.
- Good conflict UX is half detection, half **navigation** (scroll to the lane, highlight the file, don’t make judges hunt).

## What's next for Synergo

- Stronger prompt capture across more agents (Claude Code, Codex) with explicit adapters.
- Richer conflict UX: side-by-side overlap views, suggested owners, “claim this file” soft locks.
- Persistent session history and replay for demos and postmortems.
- Optional auth / org rooms while keeping the one-code join flow for hackathons.
- Deeper IDE integrations (status bar badge when you’re about to collide).
- Scaling the relay beyond in-memory rooms (Redis / durable room state) for longer workshops.

Synergo started as “can we see the room?” — next is “can the room coordinate itself?”
