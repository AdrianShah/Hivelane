# Dashboard v3: implementation plan

Branch: `ui/dashboard-v3`. Hivelane room for this work: `YHKJZNAG` on
`wss://hivelane.onrender.com/ws` (in `.hivelane.toml`; run `hivelane up` in the
repo to join).

Design reference: the "Synergo dashboard redesign" Design canvas on claude.ai
(owner: Adrian; ask him to share it). Artboards that matter:

- **Dashboard v3: premium, clickable**: the target. Every sidebar view, the
  command menu, invite sheet, notch, room switcher and the resolve flow work.
- **Start → room → dashboard**: the start page and the inline setup steps.
- **Bee states** / **Bee inside agent apps**: the avatar and the in-agent badge.

## Decisions already made

- **Theme.** Light is the default, but the **first visit follows the system
  setting** (`prefers-color-scheme`). With no stored choice and no system
  preference, use light. The toggle still lets people pin light or dark.
- **Server work is in scope** for this round: "go first", "release hold" and
  "ping", plus sending room expiry to the dashboard (see Phase 2.5).
- **Tailwind moves off the CDN** and into the Vite build (Phase 0).
- No swarm of agents: work is done by Adrian and Hossein, coordinated through
  the Hivelane room above. Split by files (see "Who does what").

## Look and feel rules (from the canvas)

- One white main surface on a warm off-white page (`#FBFBFA`). The main
  surface has a 1px rgba(14,15,18,.06) ring and a soft shadow; inner sections
  are separated by hairlines, not boxed.
- Type: Geist for UI, Geist Mono for numbers, codes and paths. Big numbers are
  44px weight 300, tracking -0.04em. The page opens with a sentence headline,
  not a page title.
- Colour: black and white plus one accent (`#0062D1`). State colours only as
  7px dots or thin rings: amber `#F5A524` warn/overlap, red `#E5484D` block,
  green `#2FA36B` live/clear, blue for waiting. No filled status chips.
- Buttons: ink (`#0E0F12`) primary with an inset top highlight; white secondary
  with a 1px ring; 9–10px radius in the app, pills on the start page.
- Motion: one easing curve `cubic-bezier(.16,1,.3,1)`. Page content blurs up
  once on load; lines draw in; lane bar charts rise in sequence; only live
  problems pulse. All of it off under `prefers-reduced-motion`.
- Icons: thin (1.6 stroke) inline SVG, no icon font.
- The bee: centred in a square box. Ring colour and motion carry state
  (editing bob + buzz, idle slow float, conflict wobble, blocked shake,
  offline grey, waiting blue orbit).

## Data map: what each piece of the mockup reads

| Mockup piece | Source | Notes |
|---|---|---|
| Headline sentence | online people + active conflicts + blocks | new pure fn `headline()`; unit test it |
| Active agents | `onlineUserIds` | delta from `sessionLog` joins |
| Total diffs | `totalDiffs` | trend from `stats.editHistogram` |
| Active conflicts | `displayConflicts` + recent blocks | trend from `stats.collisionHistogram` |
| Relay | connection status + RTT | RTT: time the `/readyz` fetch (new, small) |
| Session chart + dots | `stats.editHistogram`, `sessionLog.timeline` | 15m / 1h / All re-bucket |
| Lane rows | lanes state, `laneStatus()` | bar chart from `recentTouches`; overlap bar from `FileChange.line_ranges` |
| Activity rail | `sessionLog.timeline` + joins | |
| Prevented card | `summarizePrevention`, `shareCard` | existing |
| Room switcher | `roomAccess.ts` (owner keys in this browser) | |
| Room expiry | **not sent today** | Phase 2.5 |
| Go first / Release / Ping | **no protocol today** | Phase 2.5 |
| "eu-west" region | not known | cut |
| Past sessions table | relay history, limited by `history_hours` | show only what history covers |

## Phases

Tick the boxes as you go so whoever picks this up knows where it stands.

### Phase 0: Foundations (no visible change)

- [ ] **Tailwind into the build.** Remove the CDN script and inline
  `tailwind.config` from `index.html`. Add Tailwind v4 with `@tailwindcss/vite`
  (or v3 + PostCSS if v4 breaks the forms/container-queries usage). Port the
  inline config: colours map to `rgb(var(--c-*) / <alpha-value>)` so
  `bg-primary/10` keeps working. Check `npm run build` output size and that no
  class went missing (grep a few dynamic class strings).
- [ ] **Tokens.** In `src/index.css`, make light the `:root` default and move
  dark under `[data-theme='dark']` (today it is the reverse). Add the v3
  values: page `#FBFBFA`, sheet `#FFFFFF`, hairline, ink, accent, state colours,
  shadows, radii, and the motion curve and durations as custom properties.
  Dark gets matching values (near-black page, `#141416` sheet).
- [ ] **Theme start-up.** In `index.html`'s inline script and
  `src/dashboard/theme.ts`: stored choice → else `prefers-color-scheme` → else
  light. Update `status-shortcuts-theme.test.ts`.
- [ ] **Fonts.** Geist is already loaded; add weight 300. Drop the Material
  Symbols font once Phase 3 has replaced every use.
- [ ] **`src/components/Icon.tsx`.** Inline-SVG icon set, one `name` prop,
  paths copied from the canvas (dashboard, feed, tasks, timeline, heatmap,
  conflicts, history, replay, analytics, invite, notifications, settings,
  search, chevron, close, copy, share).
- [ ] **`src/components/Bee.tsx`.** Wraps `avatarSvg` from `brand/mascot.ts`
  in a square, centred box (`size` × `size`); props `state`, `accent`, `size`,
  optional `ring`. Add a `waiting` avatar state.
- [ ] **Feature flag.** `ui=v3` URL parameter, remembered in localStorage;
  `App.tsx` renders the new shell when it is on. Old UI stays the default
  until Phase 5.

### Phase 1: Page frame

- [ ] `src/v3/Shell.tsx`: header + sidebar + main sheet + right rail slot.
- [ ] Header: logo (home), `/`, room code dropdown (rooms from
  `roomAccess.ts`, "+ New room"), ⌘K trigger, avatar stack (to feed), Invite.
- [ ] `src/v3/Notch.tsx`: collapsed 252×42 black pill with bee, agent count and
  one status (overlap / held / all clear). Click or hover expands to a sentence
  plus Review / Ping. Driven by conflicts and blocks.
- [ ] Sidebar: restyle `Sidebar.tsx`: two groups split by one hairline,
  active item as a raised white pill, conflict count badge, footer box with
  relay status and room expiry (expiry after Phase 2.5).
- [ ] Remove the Cluster/Logs/Analytics/Config top tabs (`TOP_NAV` in
  `App.tsx`) and the floating invite button. Restyle `MobileNav.tsx`.

### Phase 2: Dashboard view

Replaces the `view === 'dashboard'` block in `App.tsx`.

- [ ] `headline()` in `src/dashboard/headline.ts` + unit test.
- [ ] Four key numbers with trend lines (`Sparkline.tsx`, path from a number
  array; unit test the path builder). Each one is a button to its view.
- [ ] Session chart (`EditsChart.tsx`): area + line, range switch, clickable
  dots for warns/blocks/overlaps.
- [ ] Lane rows (`LaneRow.tsx`): bee, name, app, file, 24-bucket bar chart,
  status dot, chevron. Click opens intent, overlap bar (from `line_ranges`) and
  actions. Bucketing in a pure fn + test.
- [ ] Activity rail and prevented card.

### Phase 2.5: Server: resolve actions and room expiry

Bump `PROTOCOL_VERSION` in **both** `server/models.py` and
`synergo/protocol.py` (a test fails on drift); update `protocol/schema.json`
and `frontend/src/types.ts`.

- [ ] **Release a held edit**: owner-authenticated spectator →
  relay `hold_release { decision_id }`. The relay clears the block and tells
  the waiting daemon it may proceed (reuse the queue path in `server/room.py`).
- [ ] **Go first**: `queue_order { file_path, first_user_id }`. The
  relay moves that user to the front of the file's queue; the other gets a
  `waiting` decision with `queue_position`.
- [ ] **Ping**: `nudge { to_user_id, text }`. The daemon surfaces it
  through the agent's hook output and desktop notifications.
- [ ] **Room expiry**: include `created_at` and `expires_at` in the room
  settings the relay sends to spectators.
- [ ] Rate-limit the three new messages like other spectator messages; only
  the owner token may send them. Tests in `server/tests`; daemon tests in
  `hivelane/tests`.

### Phase 3: Other views, restyled with their logic unchanged

Order: Feed (`Lane.tsx` as cards with intent, plan, diff) → Conflicts (side by
side via `diffFor`, wired to the Phase 2.5 actions; "All clear" empty state) →
Timeline → Heat map → Tasks → History → Replay → Analytics → Invite →
Notifications → Settings.

### Phase 4: Overlays

- [ ] Command menu with a real ⌘K / Ctrl+K shortcut: views and actions; files and agents later.
- [ ] Invite sheet (merge `SharePanel` + `AddTeammate`).
- [ ] Confirmation toasts and the room menu.

### Phase 5: Start page, setup and cutover

- [ ] Restyle `pages/Start.tsx` like the flow artboard.
- [ ] Move `Onboarding.tsx` from a modal into the empty dashboard as an
  inline four-step guide with the "Listening for your first agent…" bee.
- [ ] Remove the flag and the old dashboard code.

### Phase 6: Bee in agent apps (separate track)

- [ ] `hivelane status --line` (one line: hex mark, state dot, counts).
- [ ] `hivelane up` offers to set Claude Code's status line to it; shell
  prompt snippet for Codex / Gemini CLI.
- [ ] Inline held/overlap card in the agent chat through the existing hooks.
- [ ] Short trial: a tiny always-on-top overlay pill started by the daemon
  for GUI apps (Cursor, VS Code, Claude desktop). Decide after the trial.

## Who does what (to keep Hivelane lanes apart)

- **Adrian**: Phases 1, 2, 3, 4 (frontend: `App.tsx`, `src/v3/*`,
  `src/components/*`).
- **Hossein**: Phase 0 build + theme (`index.html`, `index.css`, `theme.ts`,
  Vite config) first, then Phase 2.5 (`server/*`, `synergo/protocol.py`,
  `hivelane/daemon.py`, `protocol/*`).
- `src/types.ts` is shared: whoever adds Phase 2.5 types announces it in the
  room first.

## Checks before each merge

- `npm run lint`, `npm run build`, `npm run test:unit` in `frontend/`;
  `pytest` for server and daemon changes. CI (`ci.yml`) must be green.
- Run the dev server and check light, dark and system-follow, phone width
  (375px), reduced motion, and a live room with at least two agents.
