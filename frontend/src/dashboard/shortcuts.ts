/**
 * Keyboard shortcuts: a tiny key-sequence matcher ("g" then "t") plus the
 * "is the user typing?" guard. Pure so both are unit-tested; App.tsx wires
 * them to a keydown listener.
 */

export type ShortcutAction =
  | 'go-lanes'
  | 'go-timeline'
  | 'go-heatmap'
  | 'focus-search'
  | 'toggle-help'
  | 'close';

export interface Shortcut {
  /** Keys as typed, space-separated for sequences. */
  keys: string;
  action: ShortcutAction;
  description: string;
}

export const SHORTCUTS: Shortcut[] = [
  { keys: 'g l', action: 'go-lanes', description: 'Go to agent lanes' },
  { keys: 'g t', action: 'go-timeline', description: 'Go to the collision timeline' },
  { keys: 'g h', action: 'go-heatmap', description: 'Go to the file heat map' },
  { keys: '/', action: 'focus-search', description: 'Search intents and captions' },
  { keys: '?', action: 'toggle-help', description: 'Show or hide this help' },
  { keys: 'Escape', action: 'close', description: 'Close dialogs and overlays' },
];

/** How long the second key of a sequence may lag the first. */
export const SEQUENCE_TIMEOUT_MS = 1500;

export type ShortcutMatcher = (key: string, nowMs: number) => ShortcutAction | null;

export function createShortcutMatcher(
  shortcuts: Shortcut[] = SHORTCUTS,
  timeoutMs = SEQUENCE_TIMEOUT_MS,
): ShortcutMatcher {
  const sequences = shortcuts.map((s) => ({ keys: s.keys.split(' '), action: s.action }));
  let pending: string[] = [];
  let lastAt = 0;
  return (key, nowMs) => {
    if (pending.length && nowMs - lastAt > timeoutMs) pending = [];
    const attempt = [...pending, key];
    lastAt = nowMs;
    const exact = sequences.find(
      (s) => s.keys.length === attempt.length && s.keys.every((k, i) => k === attempt[i]),
    );
    if (exact) {
      pending = [];
      return exact.action;
    }
    const prefix = sequences.some(
      (s) => s.keys.length > attempt.length && attempt.every((k, i) => s.keys[i] === k),
    );
    if (prefix) {
      pending = attempt;
      return null;
    }
    // Not a continuation: maybe this key starts a fresh sequence ("g g t").
    pending = sequences.some((s) => s.keys.length > 1 && s.keys[0] === key) ? [key] : [];
    const single = sequences.find((s) => s.keys.length === 1 && s.keys[0] === key);
    return single && pending.length === 0 ? single.action : null;
  };
}

interface TargetLike {
  tagName?: string;
  isContentEditable?: boolean;
  type?: string;
}

const TEXT_INPUT_TYPES = new Set([
  'text',
  'search',
  'email',
  'password',
  'number',
  'url',
  'tel',
  'date',
  'datetime-local',
  'month',
  'time',
  'week',
]);

/** True when a key press belongs to a field the user is typing in. */
export function isTypingTarget(target: unknown): boolean {
  if (!target || typeof target !== 'object') return false;
  const el = target as TargetLike;
  if (el.isContentEditable) return true;
  const tag = (el.tagName ?? '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') return TEXT_INPUT_TYPES.has((el.type ?? 'text').toLowerCase());
  return false;
}
