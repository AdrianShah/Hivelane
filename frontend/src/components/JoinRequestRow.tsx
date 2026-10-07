import type { JoinRequest } from '../types';

/**
 * One person waiting to join an approval-required room, with the owner's
 * two answers. Shared by the under-header banner and the Settings panel.
 */

interface JoinRequestRowProps {
  request: JoinRequest;
  onDecide: (requestId: string, approve: boolean) => void;
}

function waitedFor(timestamp: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (seconds < 60) return 'just now';
  return `${Math.floor(seconds / 60)} min ago`;
}

export default function JoinRequestRow({ request, onDecide }: JoinRequestRowProps) {
  const agent = request.agent && request.agent !== 'unknown' ? request.agent : null;
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-2">
      <div className="min-w-0">
        <span className="font-mono text-[13px] font-semibold text-on-surface">
          {request.name}
        </span>
        {agent ? (
          <span className="ml-2 text-[10px] font-bold uppercase tracking-[0.1em] text-lavender bg-lavender/10 border border-lavender/20 px-1.5 py-0.5 rounded-md">
            {agent}
          </span>
        ) : null}
        <span className="ml-2 text-[12px] text-on-dim">{waitedFor(request.timestamp)}</span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button
          type="button"
          className="deploy-btn rounded-xl px-4 py-1.5 text-[11px] font-bold"
          onClick={() => onDecide(request.request_id, true)}
        >
          Approve
        </button>
        <button
          type="button"
          className="rounded-xl border border-outline-soft px-4 py-1.5 text-[11px] font-bold uppercase tracking-wider text-on-muted hover:text-error hover:border-error/40 transition-colors"
          onClick={() => onDecide(request.request_id, false)}
        >
          Deny
        </button>
      </div>
    </li>
  );
}
