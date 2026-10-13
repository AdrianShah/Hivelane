import { STATUS_HINT, STATUS_LABEL, type LaneStatus } from '../dashboard/laneStatus';
import { agentLabel, initials, personColor } from '../dashboard/people';

/**
 * Small person/agent primitives shared by lanes, the timeline and the heat
 * map, so a person looks the same (colour, initials, agent mark) everywhere.
 */

const AGENT_MARK: Record<string, string> = {
  cursor: 'Cu',
  'claude-code': 'CC',
  codex: 'Cx',
  antigravity: 'AG',
  'gemini-cli': 'Ge',
  devin: 'Dv',
  grok: 'Gk',
  aider: 'Ai',
  human: 'Hu',
};

export function PersonAvatar({
  userId,
  size = 'md',
  dotClass,
  dotLabel,
}: {
  userId: string;
  size?: 'sm' | 'md';
  /** Presence dot colour class; omitted for no dot. */
  dotClass?: string;
  dotLabel?: string;
}) {
  // Inline, not utilities: .lane-avatar's own size would win over w-6/h-6.
  const dimension = size === 'sm' ? { width: '1.5rem', height: '1.5rem', fontSize: '9px' } : {};
  return (
    <span
      className="lane-avatar shrink-0"
      style={{ background: personColor(userId), ...dimension }}
      aria-hidden={dotLabel ? undefined : true}
    >
      {initials(userId)}
      {dotClass ? (
        <span className={`lane-avatar__dot ${dotClass}`} title={dotLabel}>
          {dotLabel ? <span className="sr-only">{dotLabel}</span> : null}
        </span>
      ) : null}
    </span>
  );
}

export function PersonName({ userId, className = '' }: { userId: string; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 min-w-0 ${className}`}>
      <span className="person-dot" style={{ background: personColor(userId) }} aria-hidden="true" />
      <span className="truncate">{userId}</span>
    </span>
  );
}

export function AgentBadge({ agent }: { agent: string | undefined }) {
  const kind = agent ?? 'unknown';
  const mark = AGENT_MARK[kind] ?? '?';
  const label = agentLabel(kind);
  return (
    <span className="agent-badge" title={`Agent: ${label}`}>
      <span className="agent-badge__mark" aria-hidden="true">
        {mark}
      </span>
      <span>
        <span className="sr-only">Agent: </span>
        {label}
      </span>
    </span>
  );
}

const STATUS_ICON: Record<LaneStatus, string> = {
  editing: 'edit',
  idle: 'bedtime',
  blocked: 'block',
  offline: 'cloud_off',
};

export function StatusPill({ status }: { status: LaneStatus }) {
  return (
    <span className={`agent-status-pill is-${status}`} title={STATUS_HINT[status]}>
      <span className="material-symbols-outlined" aria-hidden="true">
        {STATUS_ICON[status]}
      </span>
      <span>
        <span className="sr-only">Status: </span>
        {STATUS_LABEL[status]}
      </span>
    </span>
  );
}
