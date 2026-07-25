import { useNavigate } from 'react-router-dom';
import { relayHttpOrigin } from '../relayUrl';

export type AppView =
  | 'dashboard'
  | 'agents'
  | 'watchers'
  | 'conflicts'
  | 'history'
  | 'analytics'
  | 'settings'
  | 'docs';

interface SidebarProps {
  view: AppView;
  onNavigate: (view: AppView) => void;
  conflictCount: number;
  elapsedSeconds: number;
  roomId?: string;
  onCopyRoom?: () => void;
  copied?: boolean;
  onDeployAgent?: () => void;
  onNewRoom?: () => void;
  creatingRoom?: boolean;
}

function formatElapsed(seconds: number): string {
  const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
  const s = String(seconds % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

const NAV: { id: AppView; label: string; icon: string }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
  { id: 'agents', label: 'Agents', icon: 'smart_toy' },
  { id: 'watchers', label: 'Watchers', icon: 'radar' },
  { id: 'conflicts', label: 'Conflicts', icon: 'warning' },
  { id: 'history', label: 'History', icon: 'history' },
  { id: 'analytics', label: 'Analytics', icon: 'monitoring' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
];

export default function Sidebar({
  view,
  onNavigate,
  conflictCount,
  elapsedSeconds,
  roomId,
  onCopyRoom,
  copied = false,
  onDeployAgent,
  onNewRoom,
  creatingRoom = false,
}: SidebarProps) {
  const navigate = useNavigate();

  return (
    <aside className="hidden md:flex flex-col w-[232px] shrink-0 glass-sidebar h-full py-6 px-3">
      <div className="flex items-center gap-3 px-2.5 mb-8">
        <button
          type="button"
          className="w-9 h-9 rounded-xl bg-gradient-to-br from-lavender/20 to-primary/10 border border-lavender/20 flex items-center justify-center shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] hover:border-lavender/40 transition-colors"
          title="Back to home"
          onClick={() => navigate('/')}
        >
          <span className="material-symbols-outlined text-lavender text-[20px]">
            hub
          </span>
        </button>
        <div className="min-w-0">
          <button
            type="button"
            className="text-[13px] font-semibold text-on-surface truncate leading-tight tracking-tight hover:text-primary transition-colors text-left max-w-full"
            onClick={onCopyRoom}
            title="Copy shareable link"
          >
            {roomId || 'Synergo'}
          </button>
          <p className="text-[10px] text-on-dim font-mono mt-1 tracking-wide tabular-nums">
            {copied ? 'Link copied' : `Session ${formatElapsed(elapsedSeconds)}`}
          </p>
        </div>
      </div>

      <nav className="flex-1 space-y-1">
        {NAV.map((item) => {
          const active = view === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onNavigate(item.id)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-all duration-150 ${
                active
                  ? 'nav-active'
                  : 'text-on-muted hover:bg-white/[0.035] hover:text-on-surface'
              }`}
            >
              <span className="material-symbols-outlined text-[18px] opacity-90">
                {item.icon}
              </span>
              <span className="text-[11px] font-semibold uppercase tracking-[0.1em]">
                {item.label}
              </span>
              {item.id === 'conflicts' && conflictCount > 0 ? (
                <span className="ml-auto min-w-5 h-5 px-1.5 rounded-full bg-error/15 text-error text-[10px] font-bold flex items-center justify-center border border-error/25">
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
          className="w-full flex items-center justify-center gap-2 rounded-lg border border-outline-soft bg-surface-raised/40 px-3 py-2.5 text-[11px] font-semibold uppercase tracking-[0.1em] text-on-muted hover:text-lavender-bright hover:border-lavender/35 hover:bg-lavender/5 transition-all duration-150 disabled:opacity-50"
          onClick={onNewRoom}
          disabled={creatingRoom}
        >
          <span className="material-symbols-outlined text-base">account_tree</span>
          {creatingRoom ? 'Creating…' : 'New Room'}
        </button>
        {onDeployAgent ? (
          <button
            type="button"
            className="w-full flex items-center justify-center gap-2 rounded-lg border border-primary/25 bg-primary/5 px-3 py-2.5 text-[11px] font-semibold uppercase tracking-[0.1em] text-primary hover:bg-primary/10 transition-all duration-150"
            onClick={onDeployAgent}
          >
            <span className="material-symbols-outlined text-base">rocket_launch</span>
            Deploy Agent
          </button>
        ) : null}
        <div className="flex items-center justify-between px-1.5">
          <button
            type="button"
            className="text-[10px] font-semibold uppercase tracking-[0.12em] text-on-dim hover:text-on-muted transition-colors"
            onClick={() => onNavigate('docs')}
          >
            Docs
          </button>
          <button
            type="button"
            className="text-[10px] font-semibold uppercase tracking-[0.12em] text-on-dim hover:text-on-muted transition-colors"
            onClick={() =>
              window.open(
                `${relayHttpOrigin()}/docs`,
                '_blank',
                'noopener,noreferrer',
              )
            }
            title="Open relay API docs"
          >
            Support
          </button>
        </div>
      </div>
    </aside>
  );
}
