import { useEffect, useMemo, useRef, useState } from 'react';
import ConflictBanner from './components/ConflictBanner';
import Lane from './components/Lane';
import type {
  ConflictAlert,
  JoinMessage,
  LaneState,
  ServerMessage,
} from './types';

const WS_URL = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws';

const CONFLICT_TTL_SECONDS = 120;
const CONFLICT_PRUNE_INTERVAL_MS = 5_000;

const SPECTATOR_JOIN: JoinMessage = {
  type: 'join',
  name: null,
  role: 'spectator',
};

function isActionableConflict(alert: ConflictAlert): boolean {
  return alert.verdict === 'overlapping' || alert.verdict === 'related';
}

function isExpired(alert: ConflictAlert, nowSeconds: number): boolean {
  return nowSeconds - alert.timestamp > CONFLICT_TTL_SECONDS;
}

function pruneExpired(prev: ConflictAlert[]): ConflictAlert[] {
  const now = Date.now() / 1000;
  const next = prev.filter((c) => !isExpired(c, now));
  return next.length === prev.length ? prev : next;
}

function upsertConflict(
  prev: ConflictAlert[],
  next: ConflictAlert,
): ConflictAlert[] {
  if (!isActionableConflict(next) || isExpired(next, Date.now() / 1000)) {
    return prev.filter((c) => c.file_path !== next.file_path);
  }
  const without = prev.filter((c) => c.file_path !== next.file_path);
  return [...without, next].sort((a, b) => b.timestamp - a.timestamp);
}

function formatElapsed(seconds: number): string {
  const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
  const s = String(seconds % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

function relativeTime(ts: number): string {
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)} mins ago`;
  return `${Math.floor(delta / 3600)} hours ago`;
}

type ConnStatus = 'connecting' | 'connected' | 'disconnected';

export default function App() {
  const [users, setUsers] = useState<Record<string, LaneState>>({});
  const [conflicts, setConflicts] = useState<ConflictAlert[]>([]);
  const [conflictHistory, setConflictHistory] = useState<ConflictAlert[]>([]);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [elapsed, setElapsed] = useState(0);
  const [view, setView] = useState<'agents' | 'history'>('agents');
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const id = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      setConflicts(pruneExpired);
    }, CONFLICT_PRUNE_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (cancelled) return;

      setStatus('connecting');
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled) {
          ws.close();
          return;
        }
        setStatus('connected');
        ws.send(JSON.stringify(SPECTATOR_JOIN));
      };

      ws.onmessage = (event) => {
        let msg: ServerMessage;
        try {
          msg = JSON.parse(event.data as string) as ServerMessage;
        } catch {
          console.warn('Invalid WS message', event.data);
          return;
        }

        switch (msg.type) {
          case 'user_joined':
            setUsers((prev) => {
              if (prev[msg.user_id]) return prev;
              return {
                ...prev,
                [msg.user_id]: { caption: null, changes: [], prompt: null },
              };
            });
            break;

          case 'user_left':
            setUsers((prev) => {
              const next = { ...prev };
              delete next[msg.user_id];
              return next;
            });
            setConflicts((prev) =>
              prev.filter((c) => !c.users.includes(msg.user_id)),
            );
            break;

          case 'activity_update':
            setUsers((prev) => ({
              ...prev,
              [msg.user_id]: {
                caption: msg.caption,
                changes: msg.changes,
                prompt: msg.prompt,
              },
            }));
            break;

          case 'conflict_alert':
            setConflicts((prev) => upsertConflict(prev, msg));
            if (isActionableConflict(msg)) {
              setConflictHistory((prev) => {
                const without = prev.filter((c) => c.file_path !== msg.file_path);
                return [msg, ...without].slice(0, 20);
              });
            }
            break;

          case 'name_rejected':
            console.warn('name_rejected:', msg.reason);
            break;
        }
      };

      ws.onclose = () => {
        if (cancelled) return;
        setStatus('disconnected');
        wsRef.current = null;
        retryTimer = setTimeout(connect, 2000);
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  const conflictedUsers = useMemo(() => {
    const ids = new Set<string>();
    for (const c of conflicts) {
      for (const u of c.users) ids.add(u);
    }
    return ids;
  }, [conflicts]);

  const userIds = Object.keys(users);
  const totalDiffs = useMemo(
    () => Object.values(users).reduce((sum, u) => sum + u.changes.length, 0),
    [users],
  );

  const statusLabel =
    status === 'connected'
      ? 'Cluster Live'
      : status === 'connecting'
        ? 'Connecting'
        : 'Offline';

  return (
    <div className="h-screen overflow-hidden flex flex-col font-body text-on-surface">
      <header className="glass-panel flex justify-between items-center w-full px-8 h-16 shrink-0 z-50 sticky top-0 border-b border-outline-variant/30">
        <div className="flex items-center gap-6">
          <span className="font-headline text-xl font-black text-primary tracking-tighter uppercase italic">
            AGI_HACK_24
          </span>
          <div className="h-6 w-px bg-outline-variant/50" />
          <div className="flex items-center gap-2.5" title={WS_URL}>
            <div
              className={`w-1.5 h-1.5 rounded-full ${
                status === 'connected'
                  ? 'bg-tertiary animate-pulse status-glow'
                  : status === 'connecting'
                    ? 'bg-secondary animate-pulse'
                    : 'bg-error'
              }`}
            />
            <span className="text-[11px] font-semibold text-on-surface-variant tracking-[0.1em] uppercase">
              {statusLabel}
            </span>
          </div>
        </div>

        <nav className="hidden md:flex items-center gap-10">
          <div className="flex gap-8">
            <button
              type="button"
              className={
                view === 'agents'
                  ? 'text-primary text-sm font-bold border-b-2 border-primary pb-1'
                  : 'text-on-surface-variant/80 text-sm font-medium hover:text-primary transition-all duration-300'
              }
              onClick={() => setView('agents')}
            >
              Agents
            </button>
            <button
              type="button"
              className={
                view === 'history'
                  ? 'text-primary text-sm font-bold border-b-2 border-primary pb-1'
                  : 'text-on-surface-variant/80 text-sm font-medium hover:text-primary transition-all duration-300'
              }
              onClick={() => setView('history')}
            >
              History
            </button>
          </div>
          <div className="flex items-center gap-4 px-4 py-2 bg-surface-container-low/40 rounded-lg border border-outline-variant/20">
            <div className="flex flex-col items-center">
              <span className="text-[9px] text-on-surface-variant leading-none uppercase font-bold tracking-wider mb-0.5">
                Active
              </span>
              <span className="font-mono text-sm text-secondary font-bold">
                {String(userIds.length).padStart(2, '0')}
              </span>
            </div>
            <div className="w-px h-4 bg-outline-variant/30" />
            <div className="flex flex-col items-center">
              <span className="text-[9px] text-on-surface-variant leading-none uppercase font-bold tracking-wider mb-0.5">
                Diffs
              </span>
              <span className="font-mono text-sm text-primary font-bold">
                {String(totalDiffs).padStart(2, '0')}
              </span>
            </div>
          </div>
        </nav>

        <div className="flex items-center gap-6">
          <div className="hidden sm:flex items-center gap-4">
            <button
              type="button"
              className="p-1.5 text-on-surface-variant hover:text-primary hover:scale-110 transition-all duration-200"
              title="Terminal"
            >
              <span className="material-symbols-outlined text-xl">terminal</span>
            </button>
            <button
              type="button"
              className="p-1.5 text-on-surface-variant hover:text-primary hover:scale-110 transition-all duration-200 relative"
              title="Conflicts"
              onClick={() => setView('history')}
            >
              <span className="material-symbols-outlined text-xl">notifications</span>
              {conflicts.length > 0 ? (
                <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 bg-error rounded-full ring-2 ring-surface" />
              ) : null}
            </button>
          </div>
          <span className="bg-primary/90 text-on-primary font-bold px-5 py-2 rounded-full shadow-[0_0_20px_rgba(125,211,252,0.2)] text-xs uppercase tracking-widest">
            Spectator
          </span>
        </div>
      </header>

      <ConflictBanner
        conflicts={conflicts}
        onDismiss={(filePath) =>
          setConflicts((prev) => prev.filter((c) => c.file_path !== filePath))
        }
        onDismissAll={() => setConflicts([])}
      />

      <main className="flex-1 flex overflow-hidden">
        <aside className="hidden md:flex flex-col h-full w-64 glass-sidebar px-4 py-8 shrink-0">
          <div className="flex items-center gap-3 px-2 mb-10">
            <div className="w-10 h-10 rounded-xl bg-primary-container/40 flex items-center justify-center border border-primary/20">
              <span className="material-symbols-outlined text-primary text-2xl">
                grid_view
              </span>
            </div>
            <div>
              <h2 className="text-sm font-bold text-on-surface leading-tight tracking-tight">
                Hackathon_Alpha
              </h2>
              <p className="text-[10px] text-on-surface-variant font-mono uppercase tracking-widest mt-0.5">
                {formatElapsed(elapsed)} Active
              </p>
            </div>
          </div>

          <nav className="flex-1 space-y-1">
            <button
              type="button"
              className={`w-full flex items-center gap-4 px-4 py-3 rounded-xl transition-all ${
                view === 'agents'
                  ? 'text-primary active-nav-glow'
                  : 'text-on-surface-variant ethereal-hover'
              }`}
              onClick={() => setView('agents')}
            >
              <span className="material-symbols-outlined text-lg">smart_toy</span>
              <span className="text-[11px] font-bold tracking-widest uppercase">
                Agents
              </span>
            </button>
            <button
              type="button"
              className={`w-full flex items-center gap-4 px-4 py-3 rounded-xl transition-all ${
                view === 'history'
                  ? 'text-primary active-nav-glow'
                  : 'text-on-surface-variant ethereal-hover'
              }`}
              onClick={() => setView('history')}
            >
              <span className="material-symbols-outlined text-lg">warning</span>
              <span className="text-[11px] font-bold tracking-widest uppercase">
                Conflicts
              </span>
              {conflicts.length > 0 ? (
                <span className="ml-auto bg-error/20 text-error text-[10px] px-2 py-0.5 rounded-full font-bold border border-error/30">
                  {conflicts.length}
                </span>
              ) : null}
            </button>
            <button
              type="button"
              className="w-full flex items-center gap-4 px-4 py-3 text-on-surface-variant ethereal-hover rounded-xl transition-all"
              onClick={() => setView('history')}
            >
              <span className="material-symbols-outlined text-lg opacity-70">history</span>
              <span className="text-[11px] font-bold tracking-widest uppercase">
                History
              </span>
            </button>
          </nav>
        </aside>

        <section className="flex-1 flex flex-col overflow-hidden">
          {view === 'agents' ? (
            <>
              <div className="px-10 py-8 border-b border-outline-variant/10 glass-panel">
                <h1 className="text-3xl font-black text-on-surface tracking-tight mb-2">
                  Live Agents
                </h1>
                <p className="text-on-surface-variant text-sm font-medium max-w-2xl">
                  Real-time lanes for every connected watcher — captions, prompts, and
                  expandable file diffs.
                </p>
              </div>
              <div className="flex-1 overflow-y-auto p-10 custom-scrollbar">
                {userIds.length === 0 ? (
                  <div className="glass-panel rounded-2xl px-8 py-16 text-center">
                    <span className="material-symbols-outlined text-4xl text-primary/40 mb-3">
                      hourglass_empty
                    </span>
                    <p className="text-on-surface-variant text-sm">
                      No agents connected yet. Waiting for watchers to join…
                    </p>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {userIds.map((id) => (
                      <Lane
                        key={id}
                        userId={id}
                        state={users[id]}
                        inConflict={conflictedUsers.has(id)}
                      />
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : (
            <>
              <div className="px-10 py-8 border-b border-outline-variant/10 glass-panel">
                <h1 className="text-3xl font-black text-on-surface tracking-tight mb-2">
                  Conflict History
                </h1>
                <p className="text-on-surface-variant text-sm font-medium max-w-2xl">
                  Global synchronization ledger. Review operational conflicts across the
                  distributed cluster.
                </p>
              </div>
              <div className="flex-1 overflow-y-auto p-10 custom-scrollbar">
                <div className="glass-panel rounded-2xl overflow-hidden shadow-2xl">
                  <table className="w-full text-left border-collapse">
                    <thead>
                      <tr className="bg-surface-container-high/40 border-b border-outline-variant/30">
                        <th className="px-8 py-5 text-[10px] font-black text-on-surface-variant uppercase tracking-[0.2em]">
                          Timestamp
                        </th>
                        <th className="px-8 py-5 text-[10px] font-black text-on-surface-variant uppercase tracking-[0.2em]">
                          Participants
                        </th>
                        <th className="px-8 py-5 text-[10px] font-black text-on-surface-variant uppercase tracking-[0.2em]">
                          Resource
                        </th>
                        <th className="px-8 py-5 text-[10px] font-black text-on-surface-variant uppercase tracking-[0.2em]">
                          Status
                        </th>
                        <th className="px-8 py-5 text-[10px] font-black text-on-surface-variant uppercase tracking-[0.2em] text-right">
                          Verdict
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-outline-variant/10">
                      {conflictHistory.length === 0 ? (
                        <tr>
                          <td
                            colSpan={5}
                            className="px-8 py-12 text-center text-sm text-on-surface-variant"
                          >
                            No conflicts recorded yet.
                          </td>
                        </tr>
                      ) : (
                        conflictHistory.map((c) => {
                          const active = conflicts.some(
                            (a) => a.file_path === c.file_path,
                          );
                          return (
                            <tr
                              key={`${c.file_path}-${c.timestamp}`}
                              className="hover:bg-primary/5 transition-colors group"
                            >
                              <td className="px-8 py-6 text-sm font-medium text-on-surface-variant/80 group-hover:text-on-surface transition-colors">
                                {relativeTime(c.timestamp)}
                              </td>
                              <td className="px-8 py-6">
                                <div className="flex items-center gap-2 flex-wrap">
                                  {c.users.map((u, i) => (
                                    <span key={u} className="flex items-center gap-2">
                                      {i > 0 ? (
                                        <span className="material-symbols-outlined text-primary/40 text-sm">
                                          swap_horiz
                                        </span>
                                      ) : null}
                                      <span className="text-on-surface font-bold text-sm">
                                        {u}
                                      </span>
                                    </span>
                                  ))}
                                </div>
                              </td>
                              <td className="px-8 py-6">
                                <code className="font-mono text-xs text-primary bg-primary/5 px-2.5 py-1 rounded-md border border-primary/10">
                                  {c.file_path}
                                </code>
                              </td>
                              <td className="px-8 py-6">
                                <span
                                  className={`px-3 py-1 rounded-full text-[9px] font-black uppercase tracking-widest border ${
                                    active
                                      ? 'bg-error/10 text-error border-error/20'
                                      : 'bg-tertiary/10 text-tertiary border-tertiary/20'
                                  }`}
                                >
                                  {active ? 'Active' : 'Resolved'}
                                </span>
                              </td>
                              <td className="px-8 py-6 text-right">
                                <span className="text-primary font-black text-[10px] uppercase tracking-widest">
                                  {c.verdict}
                                </span>
                              </td>
                            </tr>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  );
}
