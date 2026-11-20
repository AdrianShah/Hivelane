/**
 * The ⌘K / Ctrl+K command menu's model: commands and a forgiving matcher
 * (every typed word must prefix some word of the label or its keywords).
 */

export interface Command {
  id: string;
  label: string;
  group: 'Go to' | 'Actions';
  /** Extra words that should find it ("share" finds Invite). */
  keywords?: string[];
  /** Shown on the right, e.g. a shortcut. */
  hint?: string;
}

function words(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

/** Commands matching `query`, best first: label-prefix matches before keyword-only ones. */
export function matchCommands<T extends Command>(commands: T[], query: string): T[] {
  const typed = words(query);
  if (typed.length === 0) return commands;
  const scored: { command: T; score: number; index: number }[] = [];
  commands.forEach((command, index) => {
    const label = words(command.label);
    const extra = (command.keywords ?? []).flatMap(words);
    let score = 0;
    for (const word of typed) {
      if (label.some((w) => w.startsWith(word))) score += 2;
      else if (extra.some((w) => w.startsWith(word))) score += 1;
      else return;
    }
    if (command.label.toLowerCase().startsWith(query.trim().toLowerCase())) score += 3;
    scored.push({ command, score, index });
  });
  return scored.sort((a, b) => b.score - a.score || a.index - b.index).map((s) => s.command);
}

/** True for the menu's shortcut: ⌘K on macOS, Ctrl+K elsewhere (either accepted). */
export function isCommandMenuKey(event: { key: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean }): boolean {
  return event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey) && !event.altKey;
}
