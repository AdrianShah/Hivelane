import Icon from './Icon';
import { NAV_GROUPS } from '../v3/nav';
import { expiresSoon, expiryLabel, rttLabel } from '../dashboard/roomLife';

export type AppView =
  | 'dashboard'
  | 'agents'
  | 'tasks'
  | 'timeline'
  | 'heatmap'
  | 'invite'
  | 'conflicts'
  | 'history'
  | 'analytics'
  | 'settings'
  | 'docs'
  | 'replay'
  | 'notifications';

interface SidebarProps {
  view: AppView;
  onNavigate: (view: AppView) => void;
  conflictCount: number;
  /** Relay connection, for the footer. */
  relayLive: boolean;
  relayLabel: string;
  rttMs: number | null;
  /** Room expiry (seconds), from room_settings; null when unknown. */
  expiresAt: number | null;
  nowSeconds: number;
}

/**
 * v3 sidebar: two groups of views split by a hairline, the open view as a
 * raised white pill, a conflict count on Conflicts, and a footer box with
 * the relay's status and when the room code expires.
 */
export default function Sidebar({
  view,
  onNavigate,
  conflictCount,
  relayLive,
  relayLabel,
  rttMs,
  expiresAt,
  nowSeconds,
}: SidebarProps) {
  const current = view === 'docs' ? 'invite' : view;
  const expiry = expiryLabel(expiresAt, nowSeconds);
  const rtt = rttLabel(rttMs);
  return (
    <aside className="v3-sidebar hidden md:flex" aria-label="Views">
      {NAV_GROUPS.map((group, index) => (
        <nav key={index} aria-label={index === 0 ? 'Room' : 'Record and settings'}>
          {index > 0 ? <div className="v3-nav-sep" role="separator" /> : null}
          {group.map((item) => (
            <button
              key={item.id}
              type="button"
              className="v3-nav-item"
              aria-current={current === item.id ? 'page' : undefined}
              onClick={() => onNavigate(item.id)}
            >
              <Icon name={item.icon} size={16} />
              {item.label}
              {item.id === 'conflicts' && conflictCount > 0 ? (
                <span className="v3-nav-badge">
                  <span className="v3-dot is-warn" aria-hidden="true" />
                  <span className="sr-only">: </span>
                  {conflictCount}
                </span>
              ) : null}
            </button>
          ))}
        </nav>
      ))}

      <div className="v3-side-foot" aria-label="Relay and room">
        <div className="v3-side-foot__row">
          <span className={`v3-dot ${relayLive ? 'is-live' : 'is-warn is-pulse'}`} aria-hidden="true" />
          <strong>Relay</strong> {relayLabel.toLowerCase()}
          {rtt ? <span className="v3-mono">{rtt}</span> : null}
        </div>
        <div className="v3-side-foot__row">
          <span
            className={`v3-dot ${expiresAt && expiresSoon(expiresAt, nowSeconds) ? 'is-warn' : 'is-idle'}`}
            aria-hidden="true"
          />
          {expiry ?? 'Expiry unknown'}
        </div>
      </div>
    </aside>
  );
}
