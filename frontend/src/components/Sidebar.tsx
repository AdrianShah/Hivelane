import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { relayHttpOrigin } from '../relayUrl';
import { Mark } from '../brand/BrandArt';

export type AppView =
  | 'dashboard'
  | 'agents'
  | 'timeline'
  | 'heatmap'
  | 'invite'
  | 'conflicts'
  | 'history'
  | 'analytics'
  | 'settings'
  | 'docs'
  // Phase 5: ReplayView.tsx and NotificationsSettings.tsx.
  | 'replay'
  | 'notifications';

interface SidebarProps {
  view: AppView;
  onNavigate: (view: AppView) => void;
  conflictCount: number;
  roomId?: string;
  onCopyRoom?: () => void;
  copied?: boolean;
  onInvite?: () => void;
  onNewRoom?: () => void;
  creatingRoom?: boolean;
}

/**
 * The session clock ticks here, not in App: a 1 s state update at the root
 * re-rendered every lane, the timeline and the heat map each second.
 */
function SessionClock() {
  const [startedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return <>{formatElapsed(Math.max(0, Math.floor((now - startedAt) / 1000)))}</>;
}

function formatElapsed(seconds: number): string {
  const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
  const s = String(seconds % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

const NAV: { id: AppView; label: string; icon: string }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
  { id: 'agents', label: 'Hivelane feed', icon: 'dynamic_feed' },
  { id: 'timeline', label: 'Timeline', icon: 'timeline' },
  { id: 'heatmap', label: 'Heat map', icon: 'local_fire_department' },
  { id: 'invite', label: 'Invite', icon: 'group_add' },
  { id: 'conflicts', label: 'Conflicts', icon: 'warning' },
  { id: 'history', label: 'History', icon: 'history' },
  { id: 'replay', label: 'Replay', icon: 'replay' },
  { id: 'analytics', label: 'Analytics', icon: 'monitoring' },
  { id: 'notifications', label: 'Notifications', icon: 'notifications' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
];

export default function Sidebar({
  view,
  onNavigate,
  conflictCount,
  roomId,
  onCopyRoom,
  copied = false,
  onInvite,
  onNewRoom,
  creatingRoom = false,
}: SidebarProps) {
  const navigate = useNavigate();

  return (
    <aside className="hidden md:flex flex-col w-[220px] shrink-0 glass-sidebar h-full min-h-0 overflow-y-auto custom-scrollbar py-4 px-2">
      <div className="flex items-center gap-2.5 px-2 mb-6">
        <button
          type="button"
          className="w-7 h-7 flex items-center justify-center"
          title="Back to home"
          aria-label="Back to Hivelane home"
          onClick={() => navigate('/')}
        >
          <Mark size={34} />
        </button>
        <div className="min-w-0">
          <button
            type="button"
            className="text-[13px] font-medium text-on-surface truncate leading-tight tracking-tight hover:text-primary transition-colors text-left max-w-full"
            onClick={onCopyRoom}
            title="Copy shareable link"
          >
            {roomId || 'Hivelane'}
          </button>
          <p className="text-[11px] text-on-dim font-mono mt-0.5 tabular-nums">
            {copied ? 'Link copied' : <SessionClock />}
          </p>
        </div>
      </div>

      <nav className="flex-1 space-y-1" aria-label="Dashboard views">
        {NAV.map((item) => {
          const active = view === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onNavigate(item.id)}
              aria-current={active ? 'page' : undefined}
              className={`w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-left transition-colors duration-150 ${
                active
                  ? 'nav-active'
                  : 'text-on-muted hover:bg-ink/[0.04] hover:text-on-surface'
              }`}
            >
              <span className="material-symbols-outlined text-[16px] opacity-80" aria-hidden="true">
                {item.icon}
              </span>
              <span className="text-[13px] font-medium">
                {item.label}
              </span>
              {item.id === 'conflicts' && conflictCount > 0 ? (
                <span className="ml-auto min-w-5 h-5 px-1.5 rounded-full bg-error/15 text-error text-[10px] font-bold flex items-center justify-center border border-error/25">
                  <span className="sr-only">: </span>
                  {conflictCount}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto space-y-3 pt-5 border-t border-outline-soft/70 px-1">
        <button
          type="button"
          className="w-full flex items-center justify-center gap-2 rounded-md border border-outline-soft px-3 py-2 text-[13px] font-medium text-on-muted hover:text-on-surface hover:bg-ink/[0.04] transition-colors duration-150 disabled:opacity-50"
          onClick={onNewRoom}
          disabled={creatingRoom}
        >
          <span className="material-symbols-outlined text-base" aria-hidden="true">add</span>
          {creatingRoom ? 'Creating…' : 'New room'}
        </button>
        {onInvite ? (
          <button
            type="button"
            className="w-full flex items-center justify-center gap-2 rounded-md px-3 py-2 text-[13px] font-medium text-primary hover:bg-primary/10 transition-colors duration-150"
            onClick={onInvite}
          >
            <span className="material-symbols-outlined text-base" aria-hidden="true">person_add</span>
            Invite teammates
          </button>
        ) : null}
        <div className="flex items-center justify-between px-1.5">
          <button
            type="button"
            className="text-[12px] font-medium text-on-dim hover:text-on-muted transition-colors"
            onClick={() => onNavigate('docs')}
          >
            Docs
          </button>
          <button
            type="button"
            className="text-[12px] font-medium text-on-dim hover:text-on-muted transition-colors"
            onClick={() =>
              window.open(
                `${relayHttpOrigin()}/docs`,
                '_blank',
                'noopener,noreferrer',
              )
            }
            title="Open the relay's API reference"
          >
            API reference
          </button>
        </div>
      </div>
    </aside>
  );
}
