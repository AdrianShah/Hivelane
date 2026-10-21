/**
 * Stable per-person colours and labels. A colour comes from a hash of the
 * user id, so it is the same in every view, on every dashboard and after a
 * reload; colour is never the only cue (initials and names sit beside it).
 */
import type { AgentKind } from '../types';

/** Mid-tone hues that read on both the dark and the light theme. */
export const PERSON_PALETTE = [
  '#2dd4bf', // teal
  '#818cf8', // indigo
  '#f59e0b', // amber
  '#f43f5e', // rose
  '#38bdf8', // sky
  '#84cc16', // lime
  '#d946ef', // fuchsia
  '#f97316', // orange
] as const;

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
export function diffStat(diff: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++') || line.startsWith('---')) continue;
    if (line.startsWith('+')) added += 1;
    else if (line.startsWith('-')) removed += 1;
  }
  return { added, removed };
}
