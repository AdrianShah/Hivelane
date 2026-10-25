import { serverNowSeconds } from '../serverClock';
import type { ConflictAlert } from '../types';

interface ConflictBannerProps {
  conflicts: ConflictAlert[];
  /** Hide this alert here only (the relay keeps it; a new alert brings it back). */
  onDismiss: (conflict: ConflictAlert) => void;
  onViewOverlap: (filePath: string) => void;
  /** Open the full list of active conflicts. */
  onShowAll: () => void;
}

function formatUsers(users: string[]): string {
  if (users.length === 0) return 'Two agents';
  if (users.length === 1) return users[0];
  if (users.length === 2) return `${users[0]} and ${users[1]}`;
  return `${users.slice(0, -1).join(', ')}, and ${users[users.length - 1]}`;
}

function secondsAgo(timestamp: number): number {
  return Math.max(0, Math.floor(serverNowSeconds() - timestamp));
}

/** What the verdict means, said plainly: "related" is not "the same lines". */
function verb(conflict: ConflictAlert): string {
  const plural = conflict.users.length !== 1;
  if (conflict.verdict === 'related') {
    return plural ? 'are making related changes in' : 'is making related changes in';
  }
  return plural ? 'are both editing' : 'is editing';
}

export default function ConflictBanner({
  conflicts,
  onDismiss,
  onViewOverlap,
  onShowAll,
}: ConflictBannerProps) {
  if (conflicts.length === 0) return null;

  const primary = conflicts[0];
  const ago = secondsAgo(primary.timestamp);
  const windowLabel = ago <= 60 ? 'in the last minute' : `${Math.floor(ago / 60)} min ago`;

  return (
    <div className="conflict-banner px-3 sm:px-5 py-2.5 z-40 animate-fade-up">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-[min(100%,18rem)] flex-1">
          <span
            className="material-symbols-outlined conflict-banner__icon text-[18px] shrink-0"
            style={{ fontVariationSettings: "'FILL' 1" }}
            aria-hidden="true"
          >
            warning
          </span>
          <p className="text-[13px] text-on-surface leading-relaxed min-w-0 m-0">
            {/* Only this stable sentence is live: a ticking time inside an
                assertive region was re-announced on every render. */}
            <span role="alert">
              <span className="font-medium">Overlap:</span>{' '}
              <span className="conflict-banner__who font-medium">{formatUsers(primary.users)}</span>{' '}
              {verb(primary)}{' '}
              <code className="font-mono text-[12px] text-on-surface bg-surface-deep/70 px-1.5 py-0.5 rounded border border-outline-soft">
                {primary.file_path}
              </code>
            </span>{' '}
            <span className="text-on-muted">{windowLabel}.</span>
            {conflicts.length > 1 ? (
              <button
                type="button"
                className="ml-2 text-[12px] font-medium text-on-muted underline underline-offset-2 hover:text-on-surface"
                onClick={onShowAll}
              >
                +{conflicts.length - 1} more
              </button>
            ) : null}
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            className="conflict-banner__primary rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors"
            onClick={() => onViewOverlap(primary.file_path)}
          >
            View overlap
          </button>
          <button
            type="button"
            className="conflict-banner__ghost rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors"
            onClick={() => onDismiss(primary)}
            title="Hide this alert on this dashboard; a new alert for the file shows it again"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
