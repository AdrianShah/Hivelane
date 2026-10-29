/**
 * Posts and changelog entries for /updates, newest first. One list for both,
 * so the blog and the changelog cannot drift apart; `kind` drives the filter.
 * Add an entry here when a notable pull request merges.
 */
export type UpdateKind = 'changelog' | 'post';

export interface Update {
  slug: string;
  date: string; // YYYY-MM-DD
  kind: UpdateKind;
  title: string;
  summary: string;
  items?: string[];
  prs?: number[];
}

export const UPDATES: Update[] = [
  {
    slug: 'new-site',
    date: '2026-10-29',
    kind: 'changelog',
    title: 'A proper home for Hivelane',
    summary:
      'The public site grows up: a full landing page, this updates feed, an About page, and privacy, terms and security pages. The room launcher moved to /start, and the docs were regrouped by task.',
  },
  {
    slug: 'daemon-peer-auth',
    date: '2026-10-27',
    kind: 'changelog',
    title: 'Security hardening round',
    summary: 'A pass over every trust boundary between the daemon, the relay and the dashboard.',
    items: [
      'The daemon proves it holds the token before any client sends data to it.',
      'An auto-adopted repository can no longer turn off transport security or redaction.',
      "Teammates' plan text is cleaned before it reaches Claude Code.",
      'Dashboard connections are capped per room, and owner tokens are bound to the relay that issued them.',
      'The Gemini caption cache is scoped to the room and the diff.',
    ],
    prs: [61, 62],
  },
  {
    slug: 'meet-the-bee',
    date: '2026-10-25',
    kind: 'changelog',
    title: 'Meet the guard bee',
    summary:
      'A new logo and mascot, bee avatars that animate per agent state, a theme toggle that the docs and dashboard share, and a round of UI polish.',
    prs: [52, 53, 54, 55, 56],
  },
  {
    slug: 'landing-and-fixes',
    date: '2026-10-24',
    kind: 'changelog',
    title: 'New landing page and admission fixes',
    summary: 'A redesigned home page with a live-lane preview, a fix for a join-approval race, and steadier CI.',
    prs: [41, 49, 50, 51],
  },
  {
    slug: 'alpha',
    date: '2026-10-21',
    kind: 'post',
    title: 'Hivelane is in alpha',
    summary:
      'Hivelane started as Synergo: a way to stop two AI agents from rewriting the same file on the same afternoon. It is now ready for a small group of teams to try. Here is what to expect.',
    items: [
      'The dashboard is hosted; the relay can be your own or the alpha relay.',
      'Install from GitHub for now. PyPI, Homebrew, Scoop and npm follow the first tagged release.',
      'Expect rough edges, and please open an issue for every one you hit.',
    ],
    prs: [35, 40],
  },
  {
    slug: 'rename',
    date: '2026-10-20',
    kind: 'changelog',
    title: 'Synergo is now Hivelane',
    summary:
      'Six roadmap phases landed together: pre-edit checks, per-agent lanes, conflict radar, heat map and timeline views, notifications and replay, CI/CD, GitHub Pages, and the new name.',
    prs: [1, 2, 3, 4, 5, 6, 13],
  },
];
