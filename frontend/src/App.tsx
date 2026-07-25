import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import ConflictBanner from './components/ConflictBanner';
import Lane from './components/Lane';
import Sidebar, { type AppView } from './components/Sidebar';
import { normalizeRoomCode, roomWsUrl } from './relayUrl';
import type {
  ConflictAlert,
  JoinMessage,
  LaneState,
  ServerMessage,
} from './types';

/** Matches server/room.py CONFLICT_WINDOW_SECONDS. */
const CONFLICT_TTL_SECONDS = 120;
const CONFLICT_PRUNE_INTERVAL_MS = 5_000;

const SPECTATOR_JOIN: JoinMessage = {
  type: 'join',
  name: null,
  role: 'spectator',
};

/** Mockup preview when no live watchers are connected. */
const DEMO_USERS: Record<string, LaneState> = {
  Adrian: {
    prompt: 'Refactor FastAPI WebSocket handler to broadcast conflict alerts',
    caption:
      'Updated main.py: Added in-memory file timestamp tracking to detect concurrent mutations across distributed nodes.',
    timestamp: Date.now() / 1000 - 120,
    agent: 'cursor',
    changes: [
      {
        file_path: 'server/main.py',
        diff: `@@ -40,7 +40,12 @@
 def broadcast(room, message):
-    for ws in room.connections:
-        await ws.send_json(message)
+    now = time.time()
+    room.file_activity[path] = now
+    for ws in room.connections:
+        await ws.send_json(message)
+    if conflict_detected(room, path):
+        raise ConflictError(path)
`,
      },
    ],
  },
  Hossein: {
    prompt: 'Implement session-based locking for file writes',
    caption:
      'Modified main.py: Adding write-lock logic to prevent race conditions during high-concurrency WebSocket updates.',
    timestamp: Date.now() / 1000 - 90,
    agent: 'claude-code',
    changes: [
      {
        file_path: 'server/main.py',
        diff: `@@ -12,6 +12,14 @@
 class Room:
     def __init__(self):
         self.connections = {}
+        self.write_locks = {}
+
+    async def acquire_lock(self, path, user_id):
+        if path in self.write_locks:
+            return False
+        self.write_locks[path] = user_id
+        return True
`,
      },
    ],
  },
};

const DEMO_CONFLICT: ConflictAlert = {
  type: 'conflict_alert',
  users: ['Adrian', 'Hossein'],
  file_path: 'server/main.py',
  verdict: 'overlapping',
  explanation:
    'Both agents are mutating the same WebSocket broadcast path within the conflict window.',
  timestamp: Date.now() / 1000 - 25,
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

function relativeTime(ts: number): string {
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)} mins ago`;
  return `${Math.floor(delta / 3600)} hours ago`;
}

type ConnStatus = 'connecting' | 'connected' | 'disconnected';

const TOP_NAV = ['Cluster', 'Logs', 'Analytics', 'Config'] as const;

export default function App() {
  const { roomId: rawRoomId } = useParams<{ roomId: string }>();
  const roomId = normalizeRoomCode(rawRoomId ?? '');
  const wsUrl = useMemo(() => roomWsUrl(roomId), [roomId]);

  const [users, setUsers] = useState<Record<string, LaneState>>({});
  const [conflicts, setConflicts] = useState<ConflictAlert[]>([]);
  const [conflictHistory, setConflictHistory] = useState<ConflictAlert[]>([]);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [elapsed, setElapsed] = useState(0);
  const [view, setView] = useState<AppView>('agents');
  const [topNav, setTopNav] = useState<(typeof TOP_NAV)[number]>('Logs');
  const [highlightFile, setHighlightFile] = useState<string | null>(null);
  const [demoDismissed, setDemoDismissed] = useState(false);
  const [copied, setCopied] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const liveUserIds = Object.keys(users);
  const usingDemo = liveUserIds.length === 0 && !demoDismissed;
  const displayUsers = usingDemo ? DEMO_USERS : users;
  const displayConflicts = usingDemo
    ? conflicts.length > 0
      ? conflicts
      : [DEMO_CONFLICT]
    : conflicts;
  const displayUserIds = Object.keys(displayUsers);

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
    // A new room means the previous room's lanes/conflicts no longer apply.
    setUsers({});
    setConflicts([]);
    setConflictHistory([]);

    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (cancelled) return;

      setStatus('connecting');
      const ws = new WebSocket(wsUrl);
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
              if (prev[msg.user_id]) {
                if (msg.agent && msg.agent !== prev[msg.user_id].agent) {
                  return {
                    ...prev,
                    [msg.user_id]: { ...prev[msg.user_id], agent: msg.agent },
                  };
                }
                return prev;
              }
              return {
                ...prev,
                [msg.user_id]: {
                  caption: null,
                  changes: [],
                  prompt: null,
                  timestamp: null,
                  agent: msg.agent ?? 'unknown',
                },
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
                timestamp: msg.timestamp,
                agent: msg.agent ?? prev[msg.user_id]?.agent ?? 'unknown',
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
  }, [wsUrl]);

  const copyShareUrl = () => {
    void navigator.clipboard.writeText(window.location.href);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const conflictedUsers = useMemo(() => {
    const ids = new Set<string>();
    for (const c of displayConflicts) {
      for (const u of c.users) ids.add(u);
    }
    return ids;
  }, [displayConflicts]);

  const totalDiffs = useMemo(
    () =>
      Object.values(displayUsers).reduce((sum, u) => sum + u.changes.length, 0),
    [displayUsers],
  );

  const serverLabel =
    status === 'connected'
      ? 'SERVER: CONNECTED'
      : status === 'connecting'
        ? 'SERVER: CONNECTING'
        : 'SERVER: OFFLINE';

  const handleViewOverlap = (filePath: string) => {
    setView('agents');
    setHighlightFile(filePath);
    const conflict = displayConflicts.find((c) => c.file_path === filePath);
    const firstUser = conflict?.users[0];
    window.setTimeout(() => {
      if (firstUser) {
        document.getElementById(`lane-${firstUser}`)?.scrollIntoView({
          behavior: 'smooth',
          block: 'nearest',
        });
      }
    }, 50);
    window.setTimeout(() => setHighlightFile(null), 8000);
  };

  const handleResolve = (filePath: string) => {
    if (usingDemo) {
      setDemoDismissed(true);
      return;
    }
    setConflicts((prev) => prev.filter((c) => c.file_path !== filePath));
  };

  const historyRows = usingDemo
    ? conflictHistory.length > 0
      ? conflictHistory
      : [DEMO_CONFLICT]
    : conflictHistory;

  const showAgentGrid = view === 'agents' || view === 'dashboard';

  return (
    <div className="h-screen overflow-hidden flex flex-col bg-background">
      <header className="glass-header shrink-0 z-50">
        <div className="flex items-center justify-between gap-4 px-5 h-[56px]">
          <div className="flex items-center gap-3.5 min-w-0">
            <span className="font-extrabold text-[15px] tracking-[-0.02em] text-on-surface whitespace-nowrap">
              AGI_HACK_2024
            </span>
            <div
              className="hidden sm:inline-flex items-center gap-2 status-pill rounded-full px-2.5 py-1"
              title={wsUrl}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  status === 'connected'
                    ? 'bg-emerald shadow-live'
                    : status === 'connecting'
                      ? 'bg-amber animate-pulse-dot'
                      : 'bg-error'
                }`}
              />
              <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-on-muted">
                {serverLabel}
              </span>
            </div>
          </div>

          <nav className="hidden lg:flex items-center gap-1">
            {TOP_NAV.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setTopNav(item)}
                className={`relative text-[12px] font-medium transition-colors px-3 py-1.5 rounded-md ${
                  topNav === item
                    ? 'text-on-surface'
                    : 'text-on-muted hover:text-on-surface hover:bg-white/[0.03]'
                }`}
              >
                {item}
                {topNav === item ? (
                  <span className="absolute left-3 right-3 -bottom-[11px] h-[2px] rounded-full bg-lavender" />
                ) : null}
              </button>
            ))}
          </nav>

          <div className="flex items-center gap-2 sm:gap-2.5 shrink-0">
            <div className="hidden md:flex items-center gap-2">
              <div className="stat-chip rounded-lg px-2.5 py-1.5 flex items-baseline gap-1.5">
                <span className="text-[9px] uppercase tracking-[0.1em] text-on-dim font-semibold">
                  Agents
                </span>
                <span className="font-mono text-[12px] font-bold text-primary tabular-nums">
                  {String(displayUserIds.length).padStart(2, '0')}
                </span>
              </div>
              <div className="stat-chip rounded-lg px-2.5 py-1.5 flex items-baseline gap-1.5">
                <span className="text-[9px] uppercase tracking-[0.1em] text-on-dim font-semibold">
                  Diffs
                </span>
                <span className="font-mono text-[12px] font-bold text-on-surface tabular-nums">
                  {String(totalDiffs).padStart(2, '0')}
                </span>
              </div>
            </div>

            <div className="hidden sm:flex items-center gap-0.5 ml-0.5">
              <button
                type="button"
                className="icon-btn p-1.5"
                title={copied ? 'Link copied' : 'Share session'}
                onClick={copyShareUrl}
              >
                <span className="material-symbols-outlined text-[20px]">
                  {copied ? 'check' : 'screen_share'}
                </span>
              </button>
              <button
                type="button"
                className="icon-btn p-1.5 relative"
                title="Notifications"
                onClick={() => setView('conflicts')}
              >
                <span className="material-symbols-outlined text-[20px]">notifications</span>
                {displayConflicts.length > 0 ? (
                  <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 bg-error rounded-full ring-2 ring-surface" />
                ) : null}
              </button>
              <button
                type="button"
                className="icon-btn p-1.5"
                title="Settings"
                onClick={() => setView('settings')}
              >
                <span className="material-symbols-outlined text-[20px]">settings</span>
              </button>
            </div>

            <button
              type="button"
              className="deploy-btn rounded-full px-3.5 py-1.5 text-[11px] font-bold tracking-wide ml-1"
            >
              Deploy Agent
            </button>
          </div>
        </div>
      </header>

      <ConflictBanner
        conflicts={displayConflicts}
        onResolve={handleResolve}
        onViewOverlap={handleViewOverlap}
      />

      <div className="flex-1 flex overflow-hidden">
        <Sidebar
          view={view}
          onNavigate={setView}
          conflictCount={displayConflicts.length}
          elapsedSeconds={elapsed}
          roomId={roomId}
          onCopyRoom={copyShareUrl}
          copied={copied}
        />

        <main className="flex-1 overflow-y-auto custom-scrollbar relative workspace-canvas">
          {showAgentGrid ? (
            <div className="p-6 lg:p-8">
              <div className="flex items-end justify-between gap-4 mb-5 max-w-[1400px]">
                <div>
                  <h1 className="text-xl font-bold tracking-tight text-on-surface m-0">
                    Live Agents
                  </h1>
                  <p className="text-[13px] text-on-muted mt-1 mb-0">
                    Real-time prompts, captions, and file diffs across the cluster.
                  </p>
                </div>
                {usingDemo ? (
                  <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-amber bg-amber/10 border border-amber/25 rounded-full px-2.5 py-1 shrink-0">
                    Preview
                  </span>
                ) : null}
              </div>
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 max-w-[1400px]">
                {displayUserIds.map((id, index) => (
                  <Lane
                    key={id}
                    userId={id}
                    state={displayUsers[id]}
                    inConflict={conflictedUsers.has(id)}
                    highlightFile={
                      conflictedUsers.has(id) ? highlightFile : null
                    }
                    showArchitectureMap={index === 1}
                    index={index}
                  />
                ))}
              </div>
            </div>
          ) : null}

          {view === 'conflicts' || view === 'history' ? (
            <div className="p-6 lg:p-8 max-w-[1400px] animate-fade-up">
              <h1 className="text-xl font-bold tracking-tight text-on-surface mb-1">
                {view === 'conflicts' ? 'Active Conflicts' : 'Conflict History'}
              </h1>
              <p className="text-[13px] text-on-muted mb-6">
                Overlaps across the agent cluster for this session.
              </p>
              <div className="lane-shell rounded-2xl overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="border-b border-outline-soft bg-black/25">
                      <th className="px-5 py-3.5 text-[10px] font-bold uppercase tracking-[0.12em] text-on-dim">
                        When
                      </th>
                      <th className="px-5 py-3.5 text-[10px] font-bold uppercase tracking-[0.12em] text-on-dim">
                        Agents
                      </th>
                      <th className="px-5 py-3.5 text-[10px] font-bold uppercase tracking-[0.12em] text-on-dim">
                        File
                      </th>
                      <th className="px-5 py-3.5 text-[10px] font-bold uppercase tracking-[0.12em] text-on-dim">
                        Verdict
                      </th>
                      <th className="px-5 py-3.5 text-[10px] font-bold uppercase tracking-[0.12em] text-on-dim text-right">
                        Action
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-outline-soft/50">
                    {(view === 'conflicts' ? displayConflicts : historyRows)
                      .length === 0 ? (
                      <tr>
                        <td
                          colSpan={5}
                          className="px-5 py-12 text-center text-sm text-on-muted"
                        >
                          No conflicts recorded yet.
                        </td>
                      </tr>
                    ) : (
                      (view === 'conflicts' ? displayConflicts : historyRows).map(
                        (c) => (
                          <tr
                            key={`${c.file_path}-${c.timestamp}`}
                            className="hover:bg-white/[0.025] transition-colors"
                          >
                            <td className="px-5 py-4 text-[13px] text-on-muted tabular-nums">
                              {relativeTime(c.timestamp)}
                            </td>
                            <td className="px-5 py-4 text-[13px] font-medium text-on-surface">
                              {c.users.join(' · ')}
                            </td>
                            <td className="px-5 py-4">
                              <code className="font-mono text-[12px] text-primary bg-primary/5 border border-primary/15 px-2 py-0.5 rounded-md">
                                {c.file_path}
                              </code>
                            </td>
                            <td className="px-5 py-4">
                              <span className="text-[10px] font-bold uppercase tracking-[0.1em] text-lavender bg-lavender/10 border border-lavender/20 px-2 py-0.5 rounded-md">
                                {c.verdict}
                              </span>
                            </td>
                            <td className="px-5 py-4 text-right">
                              <button
                                type="button"
                                className="text-[10px] font-bold uppercase tracking-[0.1em] text-primary hover:text-primary/80 transition-colors"
                                onClick={() => handleViewOverlap(c.file_path)}
                              >
                                View
                              </button>
                            </td>
                          </tr>
                        ),
                      )
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          {view === 'settings' ? (
            <div className="p-6 lg:p-8 max-w-xl animate-fade-up">
              <h1 className="text-xl font-bold tracking-tight mb-1">Settings</h1>
              <p className="text-[13px] text-on-muted mb-6">
                Spectator relay connection for this dashboard.
              </p>
              <div className="lane-shell rounded-2xl p-5 space-y-4">
                <div>
                  <label className="section-label">Room</label>
                  <p className="font-mono text-[13px] text-primary mt-2 mb-0">
                    {roomId || '—'}
                  </p>
                </div>
                <div className="h-px bg-outline-soft" />
                <div>
                  <label className="section-label">WebSocket URL</label>
                  <p className="font-mono text-[13px] text-primary mt-2 break-all mb-0">
                    {wsUrl}
                  </p>
                </div>
                <div className="h-px bg-outline-soft" />
                <div>
                  <label className="section-label">Status</label>
                  <p className="text-[13px] text-on-surface mt-2 mb-0">{serverLabel}</p>
                </div>
              </div>
            </div>
          ) : null}

          <button
            type="button"
            className="fab fixed bottom-6 right-6 w-12 h-12 rounded-full flex items-center justify-center text-indigo-950 z-50"
            title="Quick action"
            onClick={() => setView('agents')}
          >
            <span className="material-symbols-outlined text-[26px]">add</span>
          </button>
        </main>
      </div>
    </div>
  );
}
