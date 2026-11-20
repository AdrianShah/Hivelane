/**
 * The dashboard's opening sentence (v3): one line that says what matters in
 * the room right now, built from who is online, the live overlaps and the
 * edits Hivelane is holding. Pure, so the wording is unit-tested.
 *
 * Precedence: held edits (someone is stopped) > overlaps (someone should
 * look) > all clear > nobody here yet.
 */

export type HeadlineTone = 'empty' | 'clear' | 'warn' | 'block';

export interface HeadlineInput {
  /** Members online now (offline lanes do not count). */
  online: number;
  /** Live overlaps: who and which file. */
  conflicts: { users: string[]; file_path: string }[];
  /** Edits held right now (blocked and not released): who waits on which file. */
  held: { user_id: string; file_path: string }[];
}

export interface Headline {
  text: string;
  tone: HeadlineTone;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Last path segment: headlines name files, not their directories. */
export function baseName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

function names(users: string[]): string {
  if (users.length <= 1) return users[0] ?? 'Someone';
  if (users.length === 2) return `${users[0]} and ${users[1]}`;
  return `${users.slice(0, -1).join(', ')} and ${users[users.length - 1]}`;
}

export function headline({ online, conflicts, held }: HeadlineInput): Headline {
  if (held.length > 0) {
    const [first] = held;
    if (held.length === 1) {
      return {
        text: `Hivelane is holding ${first.user_id}'s edit to ${baseName(first.file_path)}.`,
        tone: 'block',
      };
    }
    return {
      text: `Hivelane is holding ${plural(held.length, 'edit')}, starting with ${first.user_id} on ${baseName(first.file_path)}.`,
      tone: 'block',
    };
  }
  if (conflicts.length > 0) {
    const [first] = conflicts;
    if (conflicts.length === 1) {
      return {
        text: `${names(first.users)} are both in ${baseName(first.file_path)}.`,
        tone: 'warn',
      };
    }
    return {
      text: `${plural(conflicts.length, 'overlap')} need a look, starting with ${baseName(first.file_path)}.`,
      tone: 'warn',
    };
  }
  if (online === 0) {
    return { text: 'Listening for your first agent.', tone: 'empty' };
  }
  if (online === 1) {
    return { text: 'One agent is working, and nothing overlaps.', tone: 'clear' };
  }
  return { text: `${plural(online, 'agent')} are working, and nothing overlaps.`, tone: 'clear' };
}
