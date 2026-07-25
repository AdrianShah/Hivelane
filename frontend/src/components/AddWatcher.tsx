import { useCallback, useEffect, useRef, useState } from 'react';
import { relayWsBase } from '../relayUrl';
import type {
  CreateWatcherRequest,
  CreateWatcherResponse,
  DetectResponse,
  DetectedTool,
  WatcherInfo,
} from '../types';

/**
 * Control panel for the local companion service. The companion runs on the
 * viewer's own machine (127.0.0.1 only) and owns the watcher subprocesses —
 * this component is a thin client over its REST API.
 */

const COMPANION_URL = 'http://localhost:4500';
const WATCHER_POLL_MS = 3000;
const DETECT_TIMEOUT_MS = 2500;

const NAME_STORAGE_KEY = 'relay.displayName';

type CompanionState = 'checking' | 'online' | 'offline';

async function fetchWithTimeout(
  url: string,
  init: RequestInit = {},
  timeoutMs = DETECT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    // Non-JSON error body; fall through to the status text.
  }
  return `${response.status} ${response.statusText}`;
}

function manualCommand(
  roomId: string,
  name: string,
  agent: string,
  path: string,
): string {
  const safeName = name.trim() || '<your-name>';
  const safePath = path.trim() || '<path/to/project>';
  return `python watcher/watcher.py --path ${safePath} --name ${safeName} --room ${roomId || '<ROOM>'} --server ${relayWsBase()} --agent ${agent}`;
}

function statusColor(status: WatcherInfo['status']): string {
  if (status === 'running') return 'text-emerald';
  if (status === 'errored') return 'text-error';
  return 'text-on-dim';
}

interface AddWatcherProps {
  roomId: string;
}

export default function AddWatcher({ roomId }: AddWatcherProps) {
  const [companion, setCompanion] = useState<CompanionState>('checking');
  const [tools, setTools] = useState<DetectedTool[]>([]);
  const [watchers, setWatchers] = useState<WatcherInfo[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [displayName, setDisplayName] = useState(
    () => localStorage.getItem(NAME_STORAGE_KEY) ?? '',
  );
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState<string | null>(null);
  const [openLogs, setOpenLogs] = useState<string | null>(null);
  // Avoids re-expanding a row the user deliberately collapsed after detect.
  const autoExpanded = useRef(false);

  useEffect(() => {
    localStorage.setItem(NAME_STORAGE_KEY, displayName);
  }, [displayName]);

  const loadWatchers = useCallback(async () => {
    try {
      const response = await fetchWithTimeout(`${COMPANION_URL}/watchers`);
      if (!response.ok) return;
      setWatchers((await response.json()) as WatcherInfo[]);
    } catch {
      // The poll doubles as a liveness check; detect() owns the offline state.
    }
  }, []);

  const detect = useCallback(async () => {
    try {
      const response = await fetchWithTimeout(`${COMPANION_URL}/detect`);
      if (!response.ok) throw new Error(await errorDetail(response));
      const data = (await response.json()) as DetectResponse;
      setTools(data.tools);
      setCompanion('online');
      if (!autoExpanded.current) {
        autoExpanded.current = true;
        const active = data.tools.find((t) => t.running === true);
        if (active) setExpanded(active.id);
      }
      void loadWatchers();
    } catch {
      setCompanion('offline');
    }
  }, [loadWatchers]);

  useEffect(() => {
    void detect();
  }, [detect]);

  useEffect(() => {
    if (companion !== 'online') return;
    const id = setInterval(() => void loadWatchers(), WATCHER_POLL_MS);
    return () => clearInterval(id);
  }, [companion, loadWatchers]);

  const startWatcher = async (tool: DetectedTool) => {
    const path = (paths[tool.id] ?? '').trim();
    const name = displayName.trim();
    setError(null);

    if (!name) {
      setError('Enter a display name first.');
      return;
    }
    if (!path) {
      setError('Enter the folder path to watch.');
      return;
    }
    if (!roomId) {
      setError('No room code available.');
      return;
    }

    const body: CreateWatcherRequest = {
      path,
      room_id: roomId,
      name,
      agent: tool.id,
      server_url: relayWsBase(),
    };

    setStarting(tool.id);
    try {
      const response = await fetchWithTimeout(
        `${COMPANION_URL}/watchers`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
        8000,
      );
      if (!response.ok) {
        setError(await errorDetail(response));
        return;
      }
      (await response.json()) as CreateWatcherResponse;
      setPaths((prev) => ({ ...prev, [tool.id]: '' }));
      setExpanded(null);
      await loadWatchers();
    } catch {
      setError('Could not reach the companion service.');
      setCompanion('offline');
    } finally {
      setStarting(null);
    }
  };

  const stopWatcher = async (watcherId: string) => {
    setError(null);
    try {
      const response = await fetchWithTimeout(
        `${COMPANION_URL}/watchers/${watcherId}`,
        { method: 'DELETE' },
        8000,
      );
      if (!response.ok) {
        setError(await errorDetail(response));
        return;
      }
      await loadWatchers();
    } catch {
      setError('Could not reach the companion service.');
      setCompanion('offline');
    }
  };

  return (
    <div className="p-6 lg:p-8 max-w-[900px] animate-fade-up">
      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-on-surface m-0">
            Add Watcher
          </h1>
          <p className="text-[13px] text-on-muted mt-1 mb-0">
            Start a local file watcher for room{' '}
            <span className="font-mono text-primary">{roomId || '—'}</span> without
            touching a terminal.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void detect()}
          className="shrink-0 flex items-center gap-1.5 rounded-lg border border-outline-soft bg-surface-raised/40 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.1em] text-on-muted hover:text-on-surface hover:border-lavender/35 transition-all"
        >
          <span className="material-symbols-outlined text-[16px]">refresh</span>
          Rescan
        </button>
      </div>

      {companion === 'offline' ? (
        <div className="lane-shell rounded-2xl p-5 border-amber/30 bg-amber/[0.04] mb-6">
          <div className="flex items-start gap-3">
            <span className="material-symbols-outlined text-amber text-[20px] mt-0.5">
              warning
            </span>
            <div className="min-w-0">
              <p className="text-[13px] font-semibold text-amber m-0">
                Companion service not detected — start it locally to add a watcher
                automatically
              </p>
              <p className="text-[12px] text-on-muted mt-2 mb-3">
                Run <code className="font-mono text-on-surface">./setup.sh</code> once,
                then{' '}
                <code className="font-mono text-on-surface">
                  companion/scripts/start_mac.sh
                </code>{' '}
                (or{' '}
                <code className="font-mono text-on-surface">
                  start_windows.ps1
                </code>
                ). Or start the watcher by hand:
              </p>
              <pre className="font-mono text-[11px] text-primary bg-black/30 border border-outline-soft rounded-lg p-3 overflow-x-auto m-0 whitespace-pre-wrap break-all">
                {manualCommand(roomId, displayName, 'cursor', '')}
              </pre>
            </div>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="rounded-xl border border-error/30 bg-error/[0.06] px-4 py-3 mb-5 flex items-center justify-between gap-3">
          <span className="text-[12px] text-error">{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="text-[10px] font-bold uppercase tracking-[0.1em] text-error/80 hover:text-error"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="lane-shell rounded-2xl p-5 mb-6">
        <label className="section-label" htmlFor="watcher-name">
          Display name
        </label>
        <input
          id="watcher-name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="adrian"
          maxLength={20}
          className="mt-2 w-full rounded-lg border border-outline-soft bg-black/25 px-3 py-2 font-mono text-[13px] text-on-surface placeholder:text-on-dim focus:outline-none focus:border-lavender/50"
        />
        <p className="text-[11px] text-on-dim mt-2 mb-0">
          1–20 characters: letters, numbers, - or _. Shown as your lane name.
        </p>
      </div>

      <h2 className="section-label mb-3">Detected tools</h2>
      <div className="lane-shell rounded-2xl overflow-hidden mb-8">
        {companion === 'checking' ? (
          <p className="px-5 py-8 text-center text-[13px] text-on-muted m-0">
            Checking for the companion service…
          </p>
        ) : tools.length === 0 ? (
          <p className="px-5 py-8 text-center text-[13px] text-on-muted m-0">
            No tools detected on this machine.
          </p>
        ) : (
          <ul className="m-0 p-0 list-none divide-y divide-outline-soft/50">
            {tools.map((tool) => {
              const isOpen = expanded === tool.id;
              return (
                <li key={tool.id}>
                  <button
                    type="button"
                    onClick={() => setExpanded(isOpen ? null : tool.id)}
                    className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-white/[0.025] transition-colors"
                  >
                    <span
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                        tool.running === true
                          ? 'bg-emerald shadow-live'
                          : tool.installed
                            ? 'bg-on-dim'
                            : 'bg-outline-soft'
                      }`}
                    />
                    <span className="text-[13px] font-medium text-on-surface">
                      {tool.label}
                    </span>
                    <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-on-dim">
                      {tool.running === true
                        ? 'Running'
                        : tool.running === null
                          ? 'Unknown'
                          : tool.installed
                            ? 'Installed'
                            : 'Not found'}
                    </span>
                    <span className="material-symbols-outlined text-[18px] text-on-dim ml-auto">
                      {isOpen ? 'expand_less' : 'expand_more'}
                    </span>
                  </button>

                  {isOpen ? (
                    <div className="px-5 pb-5 -mt-1">
                      {tool.install_path ? (
                        <p className="font-mono text-[11px] text-on-dim mb-3 mt-0 break-all">
                          {tool.install_path}
                        </p>
                      ) : null}
                      <div className="flex flex-col sm:flex-row gap-2">
                        <input
                          value={paths[tool.id] ?? ''}
                          onChange={(e) =>
                            setPaths((prev) => ({
                              ...prev,
                              [tool.id]: e.target.value,
                            }))
                          }
                          placeholder="/Users/you/projects/my-app"
                          className="flex-1 rounded-lg border border-outline-soft bg-black/25 px-3 py-2 font-mono text-[12px] text-on-surface placeholder:text-on-dim focus:outline-none focus:border-lavender/50"
                        />
                        <button
                          type="button"
                          disabled={companion !== 'online' || starting === tool.id}
                          onClick={() => void startWatcher(tool)}
                          className="deploy-btn rounded-lg px-4 py-2 text-[11px] font-bold tracking-wide disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          {starting === tool.id ? 'Starting…' : 'Start'}
                        </button>
                      </div>
                      <p className="text-[11px] text-on-dim mt-2 mb-0">
                        Equivalent CLI:{' '}
                        <code className="font-mono break-all">
                          {manualCommand(
                            roomId,
                            displayName,
                            tool.id,
                            paths[tool.id] ?? '',
                          )}
                        </code>
                      </p>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <h2 className="section-label mb-3">Active watchers</h2>
      <div className="lane-shell rounded-2xl overflow-hidden">
        {watchers.length === 0 ? (
          <p className="px-5 py-8 text-center text-[13px] text-on-muted m-0">
            No watchers started from this dashboard.
          </p>
        ) : (
          <ul className="m-0 p-0 list-none divide-y divide-outline-soft/50">
            {watchers.map((watcher) => (
              <li key={watcher.watcher_id} className="px-5 py-4">
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="font-mono text-[12px] text-on-surface m-0 truncate">
                      {watcher.path}
                    </p>
                    <p className="text-[11px] text-on-dim mt-1 mb-0">
                      {watcher.agent} · {watcher.name} · room {watcher.room_id} ·{' '}
                      <span className={statusColor(watcher.status)}>
                        {watcher.status}
                      </span>
                      {watcher.exit_code !== null
                        ? ` (exit ${watcher.exit_code})`
                        : ''}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      setOpenLogs(
                        openLogs === watcher.watcher_id ? null : watcher.watcher_id,
                      )
                    }
                    className="text-[10px] font-bold uppercase tracking-[0.1em] text-on-muted hover:text-on-surface transition-colors"
                  >
                    Logs
                  </button>
                  <button
                    type="button"
                    onClick={() => void stopWatcher(watcher.watcher_id)}
                    className="text-[10px] font-bold uppercase tracking-[0.1em] text-error hover:text-error/80 transition-colors"
                  >
                    Stop
                  </button>
                </div>
                {openLogs === watcher.watcher_id ? (
                  <pre className="mt-3 max-h-56 overflow-auto custom-scrollbar font-mono text-[11px] text-on-muted bg-black/30 border border-outline-soft rounded-lg p-3 m-0 whitespace-pre-wrap break-all">
                    {watcher.logs.length > 0
                      ? watcher.logs.join('\n')
                      : 'No output yet.'}
                  </pre>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
