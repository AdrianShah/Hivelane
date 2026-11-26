import type { JoinRequest, RoomAccess } from '../types';
import JoinRequestRow from './JoinRequestRow';

/**
 * Who may join this room, in the Settings view. Owners (the dashboard that
 * created the room, or any tab given its owner token) see the invite link and
 * can rotate it, switch approval on and off, answer requests and remove
 * people the room remembers; everyone else sees the policy in force.
 */

interface RoomAccessPanelProps {
  isOwner: boolean;
  /** null until the relay's catch-up says. */
  requireApproval: boolean | null;
  requests: JoinRequest[];
  connected: boolean;
  onSetRequireApproval: (value: boolean) => void;
  onDecide: (requestId: string, approve: boolean) => void;
  /** Owners: the relay's room_access (null until it arrives, or not an owner). */
  access?: RoomAccess | null;
  /** Owners: the invite link to show (built by relayUrl.ts shareLink). */
  shareUrl?: string;
  onCopy?: (value: string) => void;
  onRotateInvite?: () => void;
  onRevokeMember?: (memberId: string) => void;
}

function issuedLabel(seconds: number): string {
  if (!seconds) return '';
  try {
    return new Date(seconds * 1000).toLocaleDateString();
  } catch {
    return '';
  }
}

export default function RoomAccessPanel({
  isOwner,
  requireApproval,
  requests,
  connected,
  onSetRequireApproval,
  onDecide,
  access = null,
  shareUrl,
  onCopy,
  onRotateInvite,
  onRevokeMember,
}: RoomAccessPanelProps) {
  const on = requireApproval === true;
  const invites = access?.invite_required === true;
  const policy =
    requireApproval == null
      ? 'Waiting for the relay…'
      : on
        ? 'New members and dashboards wait until an owner approves them, invite link or not.'
        : invites
          ? 'People with the invite link come straight in. Anyone with only the room code waits for an owner.'
          : 'Anyone with the room code or link can join.';
  const members = access?.members ?? [];

  return (
    <div>
      {isOwner && access ? (
        <div className="mb-5">
          <span className="section-label">Invite link</span>
          {access.invite_secret && shareUrl ? (
            <>
              <div className="flex items-center gap-2 mt-2">
                <code className="v3-mono text-[12px] truncate min-w-0 flex-1" title="Send this to teammates">
                  {shareUrl}
                </code>
                {onCopy ? (
                  <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm" onClick={() => onCopy(shareUrl)}>
                    Copy
                  </button>
                ) : null}
              </div>
              <p className="text-[12px] text-on-muted mt-1 mb-0">
                Whoever opens this link is let in without asking. Share it with your team only.
              </p>
            </>
          ) : (
            <p className="text-[12px] text-on-muted mt-2 mb-0">
              This room has no invite link yet: the code alone lets people in. Make one to require it.
            </p>
          )}
          {onRotateInvite ? (
            <button
              type="button"
              className="v3-btn v3-btn--ghost v3-btn--sm mt-2"
              disabled={!connected}
              onClick={onRotateInvite}
            >
              {access.invite_secret ? 'Make a new link (old links stop working)' : 'Make an invite link'}
            </button>
          ) : null}
        </div>
      ) : null}
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
      {isOwner && (on || invites || requests.length > 0) ? (
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
      {isOwner && members.length > 0 ? (
        <div className="mt-5">
          <span className="section-label">Remembered people</span>
          <p className="text-[12px] text-on-muted mt-1 mb-0">
            Let in before (approved or by link), so they are not asked again. Removing someone disconnects them;
            make a new link too if they still have the old one.
          </p>
          <ul className="m-0 mt-2 p-0 list-none divide-y divide-outline-soft/50">
            {members.map((member) => (
              <li key={member.member_id} className="flex items-center justify-between gap-3 py-2">
                <span className="min-w-0 text-[13px] truncate">
                  <span className={`v3-dot ${member.online ? 'is-ok' : ''}`} aria-hidden="true" /> {member.name}
                  <span className="text-on-muted text-[12px]">
                    {' '}
                    {member.kind === 'dashboard' ? 'dashboard' : 'agent'}
                    {issuedLabel(member.issued_at) ? ` · since ${issuedLabel(member.issued_at)}` : ''}
                    {member.online ? ' · online' : ''}
                  </span>
                </span>
                {onRevokeMember ? (
                  <button
                    type="button"
                    className="v3-btn v3-btn--ghost v3-btn--sm"
                    disabled={!connected}
                    aria-label={`Remove ${member.name}`}
                    onClick={() => onRevokeMember(member.member_id)}
                  >
                    Remove
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
