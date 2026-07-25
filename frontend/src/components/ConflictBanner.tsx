import type { ConflictAlert } from '../types';

interface ConflictBannerProps {
  conflicts: ConflictAlert[];
  onDismiss: (filePath: string) => void;
  onDismissAll: () => void;
}

function formatUsers(users: string[]): string {
  if (users.length === 0) return 'agents';
  if (users.length === 1) return users[0];
  if (users.length === 2) return `${users[0]} and ${users[1]}`;
  return `${users.slice(0, -1).join(', ')}, and ${users[users.length - 1]}`;
}

export default function ConflictBanner({
  conflicts,
  onDismiss,
  onDismissAll,
}: ConflictBannerProps) {
  if (conflicts.length === 0) return null;

  const primary = conflicts[0];

  return (
    <div
      className="bg-error-container/20 border-b border-error/20 px-8 py-2.5 flex items-center justify-between neon-border-error relative z-40"
      aria-live="polite"
    >
      <div className="flex items-center gap-3 min-w-0">
        <span
          className="material-symbols-outlined text-error text-xl shrink-0"
          style={{ fontVariationSettings: "'FILL' 1" }}
        >
          report
        </span>
        <p className="text-sm font-medium text-on-surface/90 truncate">
          <span className="text-error font-bold uppercase tracking-wider text-xs mr-2">
            Conflict{conflicts.length > 1 ? ` (${conflicts.length})` : ''}:
          </span>
          {formatUsers(primary.users)} are modifying{' '}
          <code className="bg-surface-container-highest/60 px-1.5 py-0.5 rounded font-mono text-xs text-primary">
            {primary.file_path}
          </code>
          {primary.explanation ? (
            <span className="text-on-surface-variant ml-2 hidden lg:inline">
              — {primary.explanation}
            </span>
          ) : null}
        </p>
      </div>
      <div className="flex gap-3 shrink-0">
        <button
          type="button"
          className="bg-error text-on-error px-5 py-1.5 rounded-lg font-bold text-[10px] uppercase tracking-widest hover:brightness-125 transition-all shadow-lg shadow-error/20"
          onClick={() => onDismiss(primary.file_path)}
        >
          Dismiss
        </button>
        <button
          type="button"
          className="border border-error/30 text-error px-5 py-1.5 rounded-lg font-bold text-[10px] uppercase tracking-widest hover:bg-error/10 transition-colors"
          onClick={onDismissAll}
        >
          Dismiss all
        </button>
      </div>
    </div>
  );
}
