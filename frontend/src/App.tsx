import { useEffect, useMemo, useRef, useState } from 'react';
import ConflictBanner from './components/ConflictBanner';
import Lane from './components/Lane';
import type {
  ConflictAlert,
  JoinMessage,
  LaneState,
  ServerMessage,
} from './types';
import './App.css';

const WS_URL = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws';

const SPECTATOR_JOIN: JoinMessage = {
  type: 'join',
  name: null,
  role: 'spectator',
};

/** Keep actionable alerts; drop "unrelated" noise from the banner. */
function isActionableConflict(alert: ConflictAlert): boolean {
  return alert.verdict === 'overlapping' || alert.verdict === 'related';
}

/**
 * Upsert by file path so the banner shows the latest verdict per contested file.
 */
function upsertConflict(
  prev: ConflictAlert[],
  next: ConflictAlert,
): ConflictAlert[] {
  if (!isActionableConflict(next)) {
    return prev.filter((c) => c.file_path !== next.file_path);
  }
  const without = prev.filter((c) => c.file_path !== next.file_path);
  return [...without, next].sort((a, b) => b.timestamp - a.timestamp);
}

type ConnStatus = 'connecting' | 'connected' | 'disconnected';

export default function App() {
  const [users, setUsers] = useState<Record<string, LaneState>>({});
  const [conflicts, setConflicts] = useState<ConflictAlert[]>([]);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const wsRef = useRef<WebSocket | null>(null);

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

  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__title">Agent Viewer</h1>
        <span className={`app__status app__status--${status}`} title={WS_URL}>
          {status}
        </span>
      </header>

      <ConflictBanner conflicts={conflicts} />

      {userIds.length === 0 ? (
        <p className="app__empty">
          No agents connected yet. Waiting for watchers to join…
        </p>
      ) : (
        <main className="app__grid">
          {userIds.map((id) => (
            <Lane
              key={id}
              userId={id}
              state={users[id]}
              inConflict={conflictedUsers.has(id)}
            />
          ))}
        </main>
      )}
    </div>
  );
}
