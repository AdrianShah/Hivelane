/**
 * Stable per-person colours and labels. A colour comes from a hash of the
 * user id, so it is the same in every view, on every dashboard and after a
 * reload; colour is never the only cue (initials and names sit beside it).
 */
import type { AgentKind } from '../types';

/**
 * Mid-tone hues that read on both the dark and the light theme. Only cool
 * hues: red, orange and amber mean block / conflict / warn on the same card,
 * so a person must never wear them (raj in red looked "blocked").
 */
export const PERSON_PALETTE = [
  '#2dd4bf', // teal
  '#818cf8', // indigo
  '#38bdf8', // sky
  '#a78bfa', // violet
  '#94a3b8', // slate
  '#22d3ee', // cyan
  '#c084fc', // purple
  '#60a5fa', // blue
] as const;

/** Hues a person colour must stay clear of (status colours). */
export function isStatusHue(hexColor: string): boolean {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hexColor.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max - min < 0.15) return false; // grey: no hue to clash with
  let h: number;
  if (max === r) h = ((g - b) / (max - min)) % 6;
  else if (max === g) h = (b - r) / (max - min) + 2;
  else h = (r - g) / (max - min) + 4;
  const deg = (h * 60 + 360) % 360;
  // Red through amber and yellow (block, conflict, warn), plus pink/rose.
  return deg < 65 || deg > 320;
}

export function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) hash = (hash * 31 + value.charCodeAt(i)) | 0;
  return Math.abs(hash);
}

export function personColor(userId: string): string {
  return PERSON_PALETTE[hashString(userId) % PERSON_PALETTE.length];
}

export function initials(name: string): string {
  const parts = name.trim().split(/[\s_-]+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

export const AGENT_LABEL: Record<AgentKind, string> = {
  cursor: 'Cursor',
  'claude-code': 'Claude Code',
  codex: 'Codex',
  antigravity: 'Antigravity',
  'gemini-cli': 'Gemini CLI',
  devin: 'Devin',
  grok: 'Grok',
  aider: 'Aider',
  human: 'Human',
  unknown: 'Agent',
};

export function agentLabel(agent: string | undefined): string {
  return (agent && AGENT_LABEL[agent as AgentKind]) || agent || 'Agent';
}

/** "12s ago" / "4m ago" / "2h ago" from a relay timestamp (seconds). */
export function relativeTime(ts: number | null | undefined, nowSeconds: number): string {
  if (ts == null) return 'never';
  const delta = Math.max(0, Math.floor(nowSeconds - ts));
  if (delta < 5) return 'just now';
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function clockTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** Added / removed line counts of a unified diff (headers excluded). */
export type DiffLineKind = 'meta' | 'hunk' | 'add' | 'del' | 'context' | 'note';

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
  /** New-file line number for added and context lines; null otherwise. */
  ln: number | null;
}

/**
 * A unified diff, line by line. `---`/`+++` are file headers only outside a
 * hunk: inside one, `--- x` is a removed `-- x` line and `+++x` an added
 * `++x`. A hunk header's counts say where the hunk ends. A diff with no hunk
 * header at all (agents send bare `-a` / `+b` snippets) is all body.
 * `\ No newline at end of file` is a note and does not advance the numbers.
 */
export function parseDiff(diff: string): DiffLine[] {
  const lines = diff.replace(/\r\n/g, '\n').split('\n');
  const hasHunks = lines.some((l) => l.startsWith('@@'));
  const out: DiffLine[] = [];
  let inHunk = !hasHunks;
  let oldLeft = Number.POSITIVE_INFINITY;
  let newLeft = Number.POSITIVE_INFINITY;
  let ln = 0;
  for (const text of lines) {
    if (text.startsWith('@@')) {
      const m = /^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(text);
      oldLeft = m ? (m[1] === undefined ? 1 : Number(m[1])) : Number.POSITIVE_INFINITY;
      newLeft = m ? (m[3] === undefined ? 1 : Number(m[3])) : Number.POSITIVE_INFINITY;
      ln = m ? Number(m[2]) - 1 : ln;
      inHunk = true;
      out.push({ kind: 'hunk', text, ln: null });
      continue;
    }
    if (text.startsWith('\\')) {
      out.push({ kind: 'note', text, ln: null });
      continue;
    }
    // Counts used up: the hunk ends, but only at something that looks like a
    // header (agents send hand-made hunks whose counts are wrong).
    if (inHunk && hasHunks && oldLeft <= 0 && newLeft <= 0 && /^(diff |index |--- |\+\+\+ )/.test(text)) {
      inHunk = false;
    }
    if (!inHunk) {
      // Before the first hunk or between files: diff/index/---/+++ and git's
      // extended headers.
      out.push({ kind: 'meta', text, ln: null });
      continue;
    }
    if (text.startsWith('+')) {
      newLeft -= 1;
      ln += 1;
      out.push({ kind: 'add', text, ln });
    } else if (text.startsWith('-')) {
      oldLeft -= 1;
      out.push({ kind: 'del', text, ln: null });
    } else {
      oldLeft -= 1;
      newLeft -= 1;
      ln += 1;
      out.push({ kind: 'context', text, ln });
    }
  }
  return out;
}

export function diffStat(diff: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of parseDiff(diff)) {
    if (line.kind === 'add') added += 1;
    else if (line.kind === 'del') removed += 1;
  }
  return { added, removed };
}
