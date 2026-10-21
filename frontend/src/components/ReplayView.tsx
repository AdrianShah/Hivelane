import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import {
  SPEEDS,
  advance,
  clockLabel,
  eventTime,
  foldUntil,
  onHistoryChunk,
  sortEvents,
  spanLabel,
  timeBounds,
  type ReplayLane,
  type Speed,
} from '../replay';
import type { FeedEvent, HistoryRequest } from '../types';

/**
 * Replay: scrub through the room's kept history (server/history.py) and see
 * the board as it was at any moment — lanes, decisions, conflicts — or play
 * it back at 1×, 10× or 60×. History chunks arrive on App's spectator socket
 * and are decoded there like live frames (replay.ts publishHistoryChunk), so
 * an encrypted room replays readable with the room key.
 */

interface ReplayViewProps {
  roomId: string;
  /** The room's history_hours (room_settings); 0 means the relay keeps none. */
  historyHours: number;
  /** False until the relay's room_settings arrived. */
  settingsKnown: boolean;
  connected: boolean;
  /** App's spectator socket; the request goes out on it. */
  socketRef: RefObject<WebSocket | null>;
}

/** The relay answers one history_request per socket per 10 s. */
const REQUEST_INTERVAL_MS = 10_000;
/** No answer this long after asking: the relay probably predates history. */
const ANSWER_TIMEOUT_MS = 8_000;
const TICK_MS = 200;
const MAX_MARKERS = 400;

type LoadState = 'idle' | 'loading' | 'loaded' | 'no-answer';

function LaneCard({ lane }: { lane: ReplayLane }) {
  const files = lane.changes.slice(0, 6);
  return (
    <div className={`lane-shell rounded-2xl p-4 ${lane.online ? '' : 'opacity-60'}`}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-[14px] font-semibold text-on-surface m-0 truncate">{lane.userId}</p>
        <span className="text-[10px] font-bold uppercase tracking-[0.1em] text-on-muted shrink-0">
          {lane.agent} · {lane.online ? 'online' : 'left'}
        </span>
      </div>
      {lane.intent ? (
        <p className="text-[12px] text-on-muted mt-2 mb-0">
          <span className="text-on-dim">Intent:</span> {lane.intent}
        </p>
      ) : null}
      {lane.caption || lane.prompt ? (
        <p className="text-[12px] text-on-surface mt-1.5 mb-0">{lane.caption ?? lane.prompt}</p>
      ) : null}
      {lane.plan ? (
        <p className="text-[12px] text-lavender mt-1.5 mb-0">
          Plan: {lane.plan.summary || lane.plan.files.join(', ')}
        </p>
      ) : null}
      {files.length > 0 ? (
        <ul className="m-0 mt-2 p-0 list-none space-y-0.5">
          {files.map((change) => (
            <li key={change.file_path}>
              <code className="font-mono text-[11px] text-primary break-all">{change.file_path}</code>
            </li>
          ))}
          {lane.changes.length > files.length ? (
            <li className="text-[11px] text-on-dim">+{lane.changes.length - files.length} more</li>
          ) : null}
        </ul>
      ) : null}
      {lane.decisions.length > 0 ? (
        <ul className="m-0 mt-2 p-0 list-none space-y-1">
          {lane.decisions.map((d) => (
            <li key={d.decision_id || `${d.file_path}-${d.timestamp}`} className="text-[11px]">
              <span className={d.decision === 'block' ? 'text-error font-bold' : 'text-amber font-bold'}>
                {d.decision.toUpperCase()}
              </span>{' '}
              <code className="font-mono text-on-muted break-all">{d.file_path}</code>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function HistoryOff({ settingsKnown }: { settingsKnown: boolean }) {
  return (
    <div className="lane-shell rounded-2xl p-6 max-w-[46rem] space-y-3">
      <p className="text-[14px] text-on-surface font-medium m-0">
        {settingsKnown ? 'This room keeps no history' : 'Waiting for the relay…'}
      </p>
      <p className="text-[13px] text-on-muted m-0">
        Replay needs the relay to remember the room’s events. Turn it on in the repo’s
        committed <code className="font-mono text-primary">.synergo.toml</code>:
      </p>
      <pre className="font-mono text-[12px] text-primary bg-black/25 border border-outline-soft rounded-lg px-3 py-2 m-0">
        history_hours = 4
      </pre>
      <p className="text-[12px] text-on-muted m-0">
        The next teammate to run <code className="font-mono">synergo up</code> (or reconnect)
        applies it to the room. The relay keeps up to 24 hours, at most 5,000 events, in memory
        only — never on disk — and forgets it when the room empties or the relay restarts. In an
        end-to-end encrypted room it keeps only what it relays: hashes and ciphertext.
      </p>
    </div>
  );
}

export default function ReplayView({
  roomId,
  historyHours,
  settingsKnown,
  connected,
  socketRef,
}: ReplayViewProps) {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [load, setLoad] = useState<LoadState>('idle');
  const [at, setAt] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(10);
  const [cooldown, setCooldown] = useState(false);
  const lastRequest = useRef(0);
  const incoming = useRef<FeedEvent[]>([]);
  const enabled = historyHours > 0;

  const request = useCallback(() => {
    const ws = socketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const now = Date.now();
    if (now - lastRequest.current < REQUEST_INTERVAL_MS) return;
    lastRequest.current = now;
    incoming.current = [];
    setLoad('loading');
    setCooldown(true);
    window.setTimeout(() => setCooldown(false), REQUEST_INTERVAL_MS);
    const frame: HistoryRequest = { type: 'history_request', since: 0 };
    ws.send(JSON.stringify(frame));
  }, [socketRef]);

  useEffect(
    () =>
      onHistoryChunk((chunk) => {
        incoming.current.push(...chunk.events);
        if (!chunk.done) return;
        const sorted = sortEvents(incoming.current);
        incoming.current = [];
        setEvents(sorted);
        setLoad('loaded');
        setPlaying(false);
        const bounds = timeBounds(sorted);
        setAt(bounds ? bounds.end : null);
      }),
    [],
  );

  // Ask on open and after every reconnect (a new room key decodes afresh).
  useEffect(() => {
    if (connected && enabled) request();
  }, [connected, enabled, roomId, request]);

  useEffect(() => {
    if (load !== 'loading') return;
    const id = window.setTimeout(() => setLoad('no-answer'), ANSWER_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, [load]);

  const bounds = useMemo(() => timeBounds(events), [events]);

  useEffect(() => {
    if (!playing || !bounds) return;
    let last = Date.now();
    const id = window.setInterval(() => {
      const now = Date.now();
      const elapsed = now - last;
      last = now;
      setAt((prev) => advance(prev ?? bounds.start, elapsed, speed, bounds.end));
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, [playing, speed, bounds]);

  useEffect(() => {
    if (playing && bounds && at != null && at >= bounds.end) setPlaying(false);
  }, [playing, bounds, at]);

  const state = useMemo(
    () => (at == null ? null : foldUntil(events, at)),
    [events, at],
  );

  const markers = useMemo(() => {
    if (!bounds || bounds.end <= bounds.start) return [];
    const span = bounds.end - bounds.start;
    const out: { left: number; kind: 'block' | 'warn' | 'conflict'; key: string }[] = [];
    for (const event of events) {
      if (out.length >= MAX_MARKERS) break;
      const ts = eventTime(event);
      if (ts == null) continue;
      const kind =
        event.type === 'edit_decision'
          ? event.decision
          : event.type === 'conflict_alert' && event.verdict !== 'unrelated'
            ? 'conflict'
            : null;
      if (!kind) continue;
      out.push({ left: ((ts - bounds.start) / span) * 100, kind, key: `${out.length}-${ts}` });
    }
    return out;
  }, [events, bounds]);

  const lanes = state ? Object.values(state.lanes) : [];

  return (
    <div className="p-6 lg:p-8 max-w-[1400px] animate-fade-up">
      <div className="flex flex-wrap items-end justify-between gap-4 mb-5">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-on-surface m-0">Replay</h1>
          <p className="text-[13px] text-on-muted mt-1 mb-0">
            The board for room <span className="font-mono text-primary">{roomId}</span> at any
            moment of the last {enabled ? spanLabel(historyHours * 3600) : 'few hours'}.
          </p>
        </div>
        {enabled ? (
          <button
            type="button"
            className="rounded-xl border border-outline-soft px-4 py-2 text-[11px] font-bold uppercase tracking-wider text-on-muted hover:text-primary hover:border-primary/40 transition-colors disabled:opacity-50"
            disabled={!connected || cooldown}
            onClick={request}
          >
            {load === 'loading' ? 'Loading…' : 'Reload history'}
          </button>
        ) : null}
      </div>

      {!enabled ? <HistoryOff settingsKnown={settingsKnown} /> : null}

      {enabled && !connected && load !== 'loaded' ? (
        <p className="text-[13px] text-on-muted">Connecting to the relay…</p>
      ) : null}
      {enabled && load === 'no-answer' ? (
        <p className="text-[13px] text-amber">
          The relay did not answer. It may be older than room history; try again in a moment.
        </p>
      ) : null}
      {enabled && load === 'loaded' && !bounds ? (
        <div className="lane-shell rounded-2xl p-6 max-w-[46rem]">
          <p className="text-[14px] text-on-surface m-0">Nothing kept yet.</p>
          <p className="text-[13px] text-on-muted mt-2 mb-0">
            History starts when it is switched on (or since the relay last restarted). Come back
            once the agents have done some work.
          </p>
        </div>
      ) : null}

      {enabled && bounds && state ? (
        <>
          <div className="lane-shell rounded-2xl p-5 mb-5 space-y-3">
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="deploy-btn rounded-xl px-4 py-1.5 text-[11px] font-bold"
                onClick={() => {
                  if (!playing && state.at >= bounds.end) setAt(bounds.start);
                  setPlaying((p) => !p);
                }}
                aria-label={playing ? 'Pause' : 'Play'}
              >
                {playing ? 'Pause' : 'Play'}
              </button>
              <div className="flex items-center gap-1" role="group" aria-label="Playback speed">
                {SPEEDS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    aria-pressed={speed === s}
                    onClick={() => setSpeed(s)}
                    className={`rounded-lg px-2.5 py-1 text-[11px] font-bold border transition-colors ${
                      speed === s
                        ? 'border-primary/50 text-primary bg-primary/10'
                        : 'border-outline-soft text-on-muted hover:text-on-surface'
                    }`}
                  >
                    {s}×
                  </button>
                ))}
              </div>
              <span className="font-mono text-[13px] text-on-surface tabular-nums">
                {clockLabel(state.at)}
              </span>
              <span className="text-[12px] text-on-muted">
                {state.applied} of {events.length} events · {state.blocks} blocked ·{' '}
                {state.warns} warned
              </span>
            </div>
            <div className="relative">
              <div className="relative h-2 mb-1" aria-hidden="true">
                {markers.map((m) => (
                  <span
                    key={m.key}
                    className={`absolute top-0 w-[2px] h-2 rounded-full ${
                      m.kind === 'block' ? 'bg-error' : m.kind === 'warn' ? 'bg-amber' : 'bg-lavender'
                    }`}
                    style={{ left: `${m.left}%` }}
                  />
                ))}
              </div>
              <input
                type="range"
                className="w-full"
                min={bounds.start}
                max={bounds.end}
                step={1}
                value={state.at}
                aria-label="Replay time"
                aria-valuetext={clockLabel(state.at)}
                onChange={(event) => {
                  setPlaying(false);
                  setAt(Number(event.target.value));
                }}
              />
              <div className="flex justify-between text-[11px] text-on-dim font-mono tabular-nums">
                <span>{clockLabel(bounds.start)}</span>
                <span>{spanLabel(bounds.end - bounds.start)} kept</span>
                <span>{clockLabel(bounds.end)}</span>
              </div>
            </div>
          </div>

          {state.conflicts.length > 0 ? (
            <div className="mb-5 rounded-2xl border border-error/30 bg-error/5 px-4 py-3">
              {state.conflicts.map((c) => (
                <p key={`${c.file_path}-${c.timestamp}`} className="text-[13px] text-on-surface m-0">
                  <span className="font-bold text-error uppercase text-[11px] tracking-wider">
                    {c.verdict}
                  </span>{' '}
                  <code className="font-mono">{c.file_path}</code> — {c.users.join(' · ')}
                </p>
              ))}
            </div>
          ) : null}

          {lanes.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 mb-6">
              {lanes.map((lane) => (
                <LaneCard key={lane.userId} lane={lane} />
              ))}
            </div>
          ) : (
            <p className="text-[13px] text-on-muted">Nobody had done anything yet at this point.</p>
          )}

          {state.decisions.length > 0 ? (
            <div className="lane-shell rounded-2xl p-5">
              <p className="section-label m-0 mb-2">Decisions up to {clockLabel(state.at)}</p>
              <ul className="m-0 p-0 list-none divide-y divide-outline-soft/50">
                {state.decisions.slice(0, 12).map((d) => (
                  <li key={d.decision_id || `${d.user_id}-${d.timestamp}`} className="py-2 text-[12px]">
                    <span className="font-mono text-on-dim tabular-nums">{clockLabel(d.timestamp)}</span>{' '}
                    <span className={d.decision === 'block' ? 'text-error font-bold' : 'text-amber font-bold'}>
                      {d.decision}
                    </span>{' '}
                    <span className="text-on-surface">{d.user_id}</span>{' '}
                    <code className="font-mono text-on-muted break-all">{d.file_path}</code>
                    {d.reason ? <span className="text-on-muted"> — {d.reason}</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
