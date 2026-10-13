import type { ConflictAlert } from '../types';

interface ConflictBannerProps {
  conflicts: ConflictAlert[];
  onResolve: (filePath: string) => void;
  onViewOverlap: (filePath: string) => void;
}

function formatUsers(users: string[]): string {
  if (users.length === 0) return 'agents';
  if (users.length === 1) return users[0];
  if (users.length === 2) return `${users[0]} and ${users[1]}`;
  return `${users.slice(0, -1).join(', ')}, and ${users[users.length - 1]}`;
}

function secondsAgo(timestamp: number): number {
  return Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
}

export default function ConflictBanner({
  conflicts,
  onResolve,
  onViewOverlap,
}: ConflictBannerProps) {
  if (conflicts.length === 0) return null;

  const primary = conflicts[0];
  const ago = secondsAgo(primary.timestamp);
  const windowLabel = ago <= 60 ? 'within the last 60s' : `${ago}s ago`;

  return (
    <div
      className="conflict-banner px-3 sm:px-5 py-3 z-40 animate-fade-up"
      aria-live="assertive"
      role="alert"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 max-w-[1600px]">
        <div className="flex items-center gap-3.5 min-w-[min(100%,18rem)] flex-1">
          <div className="w-8 h-8 rounded-lg bg-amber/15 border border-amber/30 flex items-center justify-center shrink-0">
            <span
              className="material-symbols-outlined text-amber text-[18px]"
              style={{ fontVariationSettings: "'FILL' 1" }}
              aria-hidden
            >
              warning
            </span>
          </div>
          <p className="text-[13px] text-on-surface/95 leading-relaxed min-w-0">
            <span className="font-semibold tracking-tight">Edit Conflict Detected:</span>{' '}
            <span className="conflict-banner__who font-semibold">{formatUsers(primary.users)}</span> are
            simultaneously modifying{' '}
            <code className="font-mono text-[12px] text-primary bg-surface-deep/70 px-1.5 py-0.5 rounded-md border border-outline-soft">
              {primary.file_path}
            </code>{' '}
            <span className="text-on-muted">{windowLabel}.</span>
            {conflicts.length > 1 ? (
              <span className="text-on-muted ml-2 text-xs">
                (+{conflicts.length - 1} more)
              </span>
            ) : null}
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            className="conflict-banner__primary rounded-lg px-4 py-1.5 text-[10px] font-bold uppercase tracking-[0.1em] transition-colors shadow-sm"
            onClick={() => onResolve(primary.file_path)}
          >
            Resolve
          </button>
          <button
            type="button"
            className="conflict-banner__ghost rounded-lg px-4 py-1.5 text-[10px] font-bold uppercase tracking-[0.1em] transition-colors"
            onClick={() => onViewOverlap(primary.file_path)}
          >
            View Overlap
          </button>
        </div>
      </div>
    </div>
  );
}
