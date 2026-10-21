import type { JoinRequest } from '../types';
import JoinRequestRow from './JoinRequestRow';

/**
 * Who may join this room, in the Settings view. Owners (the dashboard that
 * created the room, or any tab given its owner token) switch approval on and
 * off and answer requests; everyone else sees the policy in force.
 */

interface RoomAccessPanelProps {
  isOwner: boolean;
  /** null until the relay's catch-up says. */
  requireApproval: boolean | null;
  requests: JoinRequest[];
  connected: boolean;
  onSetRequireApproval: (value: boolean) => void;
  onDecide: (requestId: string, approve: boolean) => void;
}

export default function RoomAccessPanel({
  isOwner,
  requireApproval,
  requests,
  connected,
  onSetRequireApproval,
  onDecide,
}: RoomAccessPanelProps) {
  const on = requireApproval === true;
  const policy =
    requireApproval == null
      ? 'Waiting for the relay…'
      : on
        ? 'New members and dashboards wait until an owner approves them.'
        : 'Anyone with the room code or link can join.';

  return (
    <div>
      <label className="section-label" htmlFor={isOwner ? 'require-approval' : undefined}>
        Join approval
      </label>
      <div className="flex items-start justify-between gap-4 mt-2">
        <div className="min-w-0">
          <p className="text-[13px] text-on-surface m-0">{policy}</p>
          <p className="text-[12px] text-on-muted mt-1 mb-0">
            {isOwner
              ? 'You own this room. People already connected stay connected when you turn this on.'
              : 'Only the room owner can change this.'}
          </p>
        </div>
        {isOwner ? (
          <button
            id="require-approval"
            type="button"
            role="switch"
            aria-checked={on}
            aria-label="Require approval to join"
            disabled={!connected || requireApproval == null}
            onClick={() => onSetRequireApproval(!on)}
            className={`relative shrink-0 w-10 h-6 rounded-full border transition-colors disabled:opacity-50 ${
              on ? 'bg-primary/30 border-primary/50' : 'bg-ink/[0.04] border-outline-soft'
            }`}
          >
            <span
              className={`absolute top-[3px] w-4 h-4 rounded-full transition-all ${
                on ? 'left-[19px] bg-primary' : 'left-[3px] bg-on-dim'
              }`}
            />
          </button>
        ) : null}
      </div>
      {isOwner && on ? (
        <div className="mt-3">
          <p className="text-[12px] text-on-muted m-0">
            {requests.length === 0
              ? 'Nobody is waiting. Requests time out after 5 minutes.'
              : 'Requests to join:'}
          </p>
          {requests.length > 0 ? (
            <ul className="m-0 p-0 list-none divide-y divide-outline-soft/50">
              {requests.map((request) => (
                <JoinRequestRow key={request.request_id} request={request} onDecide={onDecide} />
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
