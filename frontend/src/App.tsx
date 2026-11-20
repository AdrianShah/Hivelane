import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import E2EBanner from './components/E2EBanner';
import FilterBar from './components/FilterBar';
import HeatMapView from './components/HeatMapView';
import JoinRequestsBanner from './components/JoinRequestsBanner';
import Lane from './components/Lane';
import MobileNav from './components/MobileNav';
import NotificationsSettings from './components/NotificationsSettings';
import { ShareCardDialog } from './components/PreventedCounter';
import ReplayView from './components/ReplayView';
import ShortcutHelp from './components/ShortcutHelp';
import Sidebar, { type AppView } from './components/Sidebar';
import TasksView from './components/TasksView';
import TimelineView from './components/TimelineView';
import { closeAction } from './closeCodes';
import { copyText as copyToClipboard } from './clipboard';
import { isCommandMenuKey } from './dashboard/commands';
import { fileMatches, isFiltering, matchesLane } from './dashboard/filters';
import { deriveLane } from './dashboard/laneFacts';
import {
  applyLaneEvent,
  conflictKey,
  isActionableConflict,
  replaceConflict,
} from './dashboard/laneReducer';
import { laneState } from './dashboard/laneState';
import { personColor } from './dashboard/people';
import { lastDecisionFor } from './dashboard/sessionLog';
import { applyTaskEvent, claimedBy } from './dashboard/taskReducer';
import { useDashboardState, viewFromParam } from './dashboard/useDashboardState';
import { useThemePref } from './dashboard/useTheme';
import { readViewParam } from './dashboard/useUrlState';
import {
  applyFeedbackUpdate,
  decisionScope,
  type FeedbackMap,
  type Vote,
} from './detection';
import { E2EDecoder, forgetRoomKey, rememberRoomKey, takeRoomKey, type E2EState } from './e2e';
import { notifyFeedEvent } from './notifications';
import { normalizeRoomCode, relayHttpOrigin, roomLink, roomPath, roomWsUrl } from './relayUrl';
import { publishHistoryChunk } from './replay';
import { createRoom, isApprovalRefusal, ownerToken, spectatorJoin } from './roomAccess';
import { hasSafeKeys, withEntry } from './safeKey';
import { observeServerTimestamp, serverNowSeconds } from './serverClock';
import type {
  ConflictAlert,
  DecisionFeedback,
  DecisionScope,
  FeedEvent,
  HoldRelease,
  JoinDecision,
  JoinRequest,
  LaneState as WireLaneState,
  Nudge,
  PlanUpdate,
  QueueOrder,
  RoomPolicy,
  RoomSettingsUpdate,
  ServerMessage,
  Task,
} from './types';
import DashboardView from './v3/DashboardView';
import Header from './v3/Header';
import {
  CONN_LABEL,
  type ActivityItem,
  type ConnStatus,
  type HeldEdit,
  type LaneView,
  type V3Actions,
  type V3Model,
} from './v3/model';
import { buildCommands } from './v3/menuCommands';
import { CommandMenu, InviteContent, InviteSheet, Toasts } from './v3/Overlays';
import { useToasts } from './v3/useToasts';
import SetupGuide from './v3/SetupGuide';
import { AnalyticsView, ConflictsView, HistoryView, PageHead, SettingsView } from './v3/Views';

/** Matches server/room.py CONFLICT_WINDOW_SECONDS. */
const CONFLICT_TTL_SECONDS = 120;
const CONFLICT_PRUNE_INTERVAL_MS = 5_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
/** How long a departed agent's greyed-out lane stays up before it is dropped. */
const OFFLINE_LANE_TTL_MS = 10 * 60 * 1000;
/** How often derived "x ago" labels and lane statuses refresh with no traffic. */
const CLOCK_TICK_MS = 5_000;
/** Per-decision bookkeeping kept for this session (scopes, vote tallies). */
const MAX_TRACKED_DECISIONS = 500;
/** With no non-roster frame after it, the catch-up roster ends this long after room_settings. */
const ROSTER_SETTLE_MS = 2_000;
/** Relay round trip (GET /readyz) for the Relay number and the sidebar footer. */
const RTT_INTERVAL_MS = 15_000;
const RTT_SAMPLES = 24;
/** Room events kept for the activity rail. */
const MAX_ACTIVITY = 40;
/** Paths remembered for e2e raw-path and repo lookups. */
const MAX_PATHS = 500;
const DEFAULT_PING = 'Check the Hivelane dashboard: a teammate needs a word about your edit.';

const BLANK_LANE: WireLaneState = {
  caption: null,
  changes: [],
  prompt: null,
  intent: null,
  decisions: [],
  agent: 'unknown',
  timestamp: null,
};

/**
 * A lane plus when its agent left the room (ms epoch, local clock, user_left
 * carries no timestamp), or null while connected. Kept here rather than in
 * LaneState because it is dashboard bookkeeping, not part of the wire format.
 */
type LaneEntry = WireLaneState & {
  offlineSince: number | null;
  /** Latest declared plan (dropped once expires_at passes). */
  plan: PlanUpdate | null;
  /** Branch from the latest activity_update; empty when unknown. */
  branch: string;
};

const BLANK_ENTRY: LaneEntry = {
  ...BLANK_LANE,
  offlineSince: null,
  plan: null,
  branch: '',
};

function isExpired(event: { timestamp: number }, nowSeconds: number): boolean {
  return nowSeconds - event.timestamp > CONFLICT_TTL_SECONDS;
}

/** The newest `max` keys of a record (insertion order), or `prev` when within bounds. */
function capRecord<T>(prev: Record<string, T>, max: number): Record<string, T> {
  const keys = Object.keys(prev);
  if (keys.length <= max) return prev;
  return Object.fromEntries(keys.slice(keys.length - max).map((k) => [k, prev[k]]));
}

function remember<K, V>(map: Map<K, V>, key: K, value: V): void {
  map.delete(key);
  map.set(key, value);
  if (map.size > MAX_PATHS) map.delete(map.keys().next().value as K);
}

function pruneExpired(prev: ConflictAlert[]): ConflictAlert[] {
  const now = serverNowSeconds();
  const next = prev.filter((c) => !isExpired(c, now));
  return next.length === prev.length ? prev : next;
}

function pruneLaneDecisions(prev: Record<string, LaneEntry>): Record<string, LaneEntry> {
  const now = serverNowSeconds();
  let changed = false;
  const next: Record<string, LaneEntry> = {};
  for (const [id, lane] of Object.entries(prev)) {
    const kept = lane.decisions.filter((d) => !isExpired(d, now));
    const planExpired = lane.plan != null && lane.plan.expires_at < now;
    if (kept.length === lane.decisions.length && !planExpired) {
      next[id] = lane;
    } else {
      changed = true;
      next[id] = { ...lane, decisions: kept, plan: planExpired ? null : lane.plan };
    }
  }
  return changed ? next : prev;
}

function pruneOfflineLanes(prev: Record<string, LaneEntry>): Record<string, LaneEntry> {
  const now = Date.now();
  let changed = false;
  const next: Record<string, LaneEntry> = {};
  for (const [id, lane] of Object.entries(prev)) {
    if (lane.offlineSince != null && now - lane.offlineSince > OFFLINE_LANE_TTL_MS) {
      changed = true;
    } else {
      next[id] = lane;
    }
  }
  return changed ? next : prev;
}

function upsertConflict(prev: ConflictAlert[], next: ConflictAlert): ConflictAlert[] {
  if (isExpired(next, serverNowSeconds())) {
    // Too old to show: it only clears this pair's earlier alert.
    const key = conflictKey(next);
    const without = prev.filter((c) => conflictKey(c) !== key);
    return without.length === prev.length ? prev : without;
  }
  const replaced = replaceConflict(prev, next);
  return replaced === prev ? prev : [...replaced].sort((a, b) => b.timestamp - a.timestamp);
}

export default function App() {
  const navigate = useNavigate();
  const { code: rawCode } = useParams<{ code: string }>();
  const roomId = normalizeRoomCode(rawCode ?? '');
  const wsUrl = useMemo(() => roomWsUrl(roomId), [roomId]);

  const [users, setUsers] = useState<Record<string, LaneEntry>>({});
  const [conflicts, setConflicts] = useState<ConflictAlert[]>([]);
  const [conflictHistory, setConflictHistory] = useState<ConflictAlert[]>([]);
  // The room's task list (task_update frames; read-only here).
  const [tasks, setTasks] = useState<Record<string, Task>>({});
  // Conflict windows the relay enforces here; null until its catch-up arrives.
  const [roomSettings, setRoomSettings] = useState<RoomSettingsUpdate | null>(null);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  // Refreshes time-derived labels when no frames arrive. Deliberately coarse:
  // a 1 s tick here re-rendered the whole dashboard every second.
  const [, setClockMs] = useState(() => Date.now());
  // The open view also lives in ?view= so a filtered view is a shareable link.
  const [view, setView] = useState<AppView>(
    () => (viewFromParam(readViewParam(window.location.search)) ?? 'dashboard') as AppView,
  );
  const [highlightFile, setHighlightFile] = useState<string | null>(null);
  const [creatingRoom, setCreatingRoom] = useState(false);
  const [toasts, showToast, dismissToast] = useToasts();
  const wsRef = useRef<WebSocket | null>(null);
  // Detection-quality bookkeeping for decisions seen this session.
  const [decisionScopes, setDecisionScopes] = useState<Record<string, DecisionScope>>({});
  const [feedback, setFeedback] = useState<FeedbackMap>({});
  const [myVotes, setMyVotes] = useState<Record<string, Vote>>({});
  // End-to-end encrypted rooms: the passphrase (from #k= or pasted; never
  // sent to the relay) and what the decoder could make of the room's frames.
  // Read during the first render (main.tsx remounts App per room), so the
  // first connection already has it: no second connect, never the old room's key.
  const [e2eKey, setE2EKey] = useState<string | null>(() => takeRoomKey(roomId));
  const [e2eState, setE2EState] = useState<E2EState>('off');
  // Room access: this tab owns the room when it holds the owner token that
  // POST /rooms returned (see roomAccess.ts).
  const [isOwner, setIsOwner] = useState(false);
  const [requireApproval, setRequireApproval] = useState<boolean | null>(null);
  const [joinRequests, setJoinRequests] = useState<JoinRequest[]>([]);
  // The relay's words while waiting for approval, or after being refused.
  const [accessNotice, setAccessNotice] = useState<string | null>(null);
  // Bumped by "Ask again" to reconnect after a refusal.
  const [joinAttempt, setJoinAttempt] = useState(0);
  // v3: relay round trip, owner resolve actions, room activity, overlays.
  const [rttMs, setRttMs] = useState<number | null>(null);
  const [rttHistory, setRttHistory] = useState<number[]>([]);
  const [releasedAt, setReleasedAt] = useState<Record<string, number>>({});
  const [queuedAt, setQueuedAt] = useState<Record<string, number>>({});
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [commandOpen, setCommandOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  // Displayed path -> the relay's (a hash in e2e rooms), and relay path ->
  // repo_id: a go-first must name the file exactly as the relay keys it.
  const rawPaths = useRef(new Map<string, string>());
  const repoOf = useRef(new Map<string, string>());
  const [, setThemePref] = useThemePref();
  // Session log, URL filters, prevented-collision stats, overlays, shortcuts.
  const nowSeconds = serverNowSeconds();
  const dash = useDashboardState({
    onNavigate: (next) => navigateTo(next),
    feedback,
    nowSeconds,
  });
  // useReducer's dispatch: stable, so the connection effect never reconnects for it.
  const recordFeedEvent = dash.recordEvent;

  const addActivity = useCallback((kind: ActivityItem['kind'], text: string, timestamp: number) => {
    setActivity((prev) =>
      [{ id: `${kind}:${timestamp}:${text}`, kind, text, timestamp }, ...prev].slice(0, MAX_ACTIVITY),
    );
  }, []);

  const liveSocket = (): WebSocket | null => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showToast('Not connected, try again');
      return null;
    }
    return ws;
  };

  const sendAccessFrame = (frame: JoinDecision | RoomPolicy): boolean => {
    const ws = liveSocket();
    if (!ws) return false;
    ws.send(JSON.stringify(frame));
    return true;
  };

  /** Owner resolve actions (hold_release, queue_order, nudge). */
  const sendOwnerFrame = (frame: HoldRelease | QueueOrder | Nudge, done: string): boolean => {
    if (!isOwner) {
      showToast('Only the room owner can do that');
      return false;
    }
    if (status !== 'connected') {
      showToast('Not connected, try again');
      return false;
    }
    const ws = liveSocket();
    if (!ws) return false;
    ws.send(JSON.stringify(frame));
    showToast(done);
    return true;
  };

  const decideJoin = (requestId: string, approve: boolean) => {
    if (!sendAccessFrame({ type: 'join_decision', request_id: requestId, approve })) return;
    // The relay confirms with join_resolved; drop it here at once anyway.
    setJoinRequests((prev) => prev.filter((r) => r.request_id !== requestId));
  };

  const setApprovalPolicy = (value: boolean) => {
    if (sendAccessFrame({ type: 'room_policy', require_approval: value })) {
      showToast(value ? 'Joining now needs your approval' : 'Anyone with the code can join');
    }
  };

  const sendVoteNow = (decisionId: string, vote: Vote) => {
    if (myVotes[decisionId]) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN || status !== 'connected') {
      // While a dashboard awaits approval the relay drops its frames: say so.
      showToast(status === 'pending' ? 'Waiting for approval, vote not sent' : 'Not connected, vote not sent');
      return;
    }
    const frame: DecisionFeedback = { type: 'decision_feedback', decision_id: decisionId, useful: vote === 'useful' };
    ws.send(JSON.stringify(frame));
    setMyVotes((prev) => capRecord({ ...prev, [decisionId]: vote }, MAX_TRACKED_DECISIONS));
  };
  // A stable identity for memoized lanes and timeline rows; calls the latest closure.
  const sendVoteRef = useRef(sendVoteNow);
  useEffect(() => {
    sendVoteRef.current = sendVoteNow;
  });
  const sendVote = useCallback((decisionId: string, vote: Vote) => sendVoteRef.current(decisionId, vote), []);

  const highlightTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(highlightTimer.current), []);

  function navigateTo(next: AppView) {
    setView(next);
    dash.syncViewParam(next);
  }

  useEffect(() => {
    // Back/Forward (or a pasted link) changed ?view=: follow it. Views this
    // build does not list in URL_VIEWS are left alone.
    const fromUrl = viewFromParam(dash.viewParam) as AppView | null;
    if (!fromUrl) return;
    setView(fromUrl);
  }, [dash.viewParam]);

  const displayUserIds = useMemo(() => Object.keys(users), [users]);
  // Offline lanes stay on the board but are not "agents" in any count.
  const onlineUserIds = useMemo(
    () => displayUserIds.filter((id) => users[id].offlineSince == null),
    [displayUserIds, users],
  );

  useEffect(() => {
    // /r/abcd-2345 and friends settle on one canonical link, so the address
    // bar always matches what Share copies.
    if (roomId && rawCode !== roomId) {
      // Keep ?view= and filters: a shared filtered link survives the redirect.
      navigate({ pathname: roomPath(roomId), search: window.location.search }, { replace: true });
    }
  }, [rawCode, roomId, navigate]);

  useEffect(() => {
    const id = setInterval(() => setClockMs(Date.now()), CLOCK_TICK_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      setConflicts(pruneExpired);
      setUsers((prev) => pruneOfflineLanes(pruneLaneDecisions(prev)));
    }, CONFLICT_PRUNE_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  // Relay round trip: time GET /readyz (cheap, unauthenticated, no room code).
  useEffect(() => {
    let cancelled = false;
    const sample = async () => {
      const started = performance.now();
      try {
        const response = await fetch(`${relayHttpOrigin()}/readyz`, { cache: 'no-store' });
        if (!response.ok) throw new Error(String(response.status));
        await response.text();
        if (cancelled) return;
        const ms = performance.now() - started;
        setRttMs(ms);
        setRttHistory((prev) => [...prev, ms].slice(-RTT_SAMPLES));
      } catch {
        if (!cancelled) setRttMs(null);
      }
    };
    void sample();
    const id = window.setInterval(() => void sample(), RTT_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [wsUrl]);

  // ⌘K / Ctrl+K opens the command menu from anywhere.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!isCommandMenuKey(event)) return;
      event.preventDefault();
      setCommandOpen((open) => !open);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    // A new room means the previous room's lanes/conflicts no longer apply.
    setUsers({});
    setConflicts([]);
    setConflictHistory([]);
    setTasks({});
    recordFeedEvent({ type: 'reset' });
    setRoomSettings(null);
    setStatus('connecting');
    setIsOwner(ownerToken(roomId) != null);
    setRequireApproval(null);
    setJoinRequests([]);
    setAccessNotice(null);
    // Per-room detection bookkeeping (votes, scopes) must not leak across rooms.
    setDecisionScopes({});
    setFeedback({});
    setMyVotes({});
    setReleasedAt({});
    setQueuedAt({});
    setActivity([]);
    rawPaths.current.clear();
    repoOf.current.clear();

    let cancelled = false;
    let roomMissing = false;
    // Approval-required rooms: held at the door, and then refused (no retry:
    // reconnecting would just queue another request for the owners).
    let awaitingApproval = false;
    let refused = false;
    setE2EState('off');
    // A new key reconnects: the relay's catch-up is then decrypted with it.
    const e2e = new E2EDecoder(e2eKey, roomId, (state) => {
      if (!cancelled) setE2EState(state);
    });
    // Decryption is async; one chain keeps frames applied in arrival order.
    let e2eChain: Promise<void> = Promise.resolve();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    // The relay rate-limits connection attempts. A flat retry sits at that
    // budget forever and locks the dashboard out of its own room, so back off
    // and let the server's retry_after win when it sends one.
    let retryAfterMs = 0;
    // Roster reconciliation: the catch-up re-announces every current member
    // (user_joined) right after room_settings and room_policy_update. A lane
    // still shown online but not re-announced left while this socket was
    // down, and no user_left will ever come for it.
    let rosterPhase: 'waiting' | 'collecting' | 'done' = 'waiting';
    let announced = new Set<string>();
    let rosterTimer: ReturnType<typeof setTimeout> | undefined;
    const finishRoster = () => {
      if (rosterPhase !== 'collecting') return;
      rosterPhase = 'done';
      if (rosterTimer) clearTimeout(rosterTimer);
      const present = announced;
      setUsers((prev) => {
        let changed = false;
        const next: Record<string, LaneEntry> = {};
        for (const [id, lane] of Object.entries(prev)) {
          if (lane.offlineSince == null && !present.has(id)) {
            changed = true;
            next[id] = { ...lane, offlineSince: Date.now() };
          } else {
            next[id] = lane;
          }
        }
        return changed ? next : prev;
      });
      setConflicts((prev) => {
        const kept = prev.filter((c) => c.users.every((u) => present.has(u)));
        return kept.length === prev.length ? prev : kept;
      });
    };

    const nextDelay = () => {
      if (retryAfterMs > 0) {
        const wait = retryAfterMs;
        retryAfterMs = 0;
        return wait;
      }
      const capped = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
      // Jitter keeps several dashboards from retrying in lockstep.
      return capped * (0.5 + Math.random() * 0.5);
    };

    /** Remember how the relay names a path (and its repo) before e2e decoding. */
    const notePaths = (raw: FeedEvent, decoded: FeedEvent) => {
      const pairs: [string, string, string][] = [];
      const repo = (raw as { repo_id?: string }).repo_id ?? '';
      if ('file_path' in raw && 'file_path' in decoded) {
        pairs.push([String(decoded.file_path), String(raw.file_path), repo]);
      }
      if (raw.type === 'activity_update' && decoded.type === 'activity_update') {
        raw.changes.forEach((change, i) => {
          const shown = decoded.changes[i]?.file_path;
          if (shown) pairs.push([shown, change.file_path, repo]);
        });
      }
      for (const [shown, relay, repoId] of pairs) {
        remember(rawPaths.current, shown, relay);
        if (repoId || !repoOf.current.has(relay)) remember(repoOf.current, relay, repoId);
      }
    };

    const connect = () => {
      if (cancelled || roomMissing || refused) return;

      setStatus('connecting');
      // Pending requests and the policy come back in the catch-up.
      setJoinRequests([]);
      rosterPhase = 'waiting';
      announced = new Set<string>();
      if (rosterTimer) clearTimeout(rosterTimer);
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => {
        if (cancelled) {
          ws.close();
          return;
        }
        // Not "connected" yet: the relay accepts a socket before it may
        // reject it (rate limit, approval). The backoff resets and the status
        // turns live on the first catch-up frame (room_settings).
        awaitingApproval = false;
        ws.send(JSON.stringify(spectatorJoin(roomId)));
      };

      ws.onmessage = (event) => {
        let msg: ServerMessage;
        try {
          msg = JSON.parse(event.data as string) as ServerMessage;
        } catch {
          // Length only: the frame is untrusted and may be huge or carry control characters.
          console.warn('Invalid WS message', `(${String(event.data).length} chars)`);
          return;
        }

        const applyDecoded = (item: FeedEvent) => {
          e2eChain = e2eChain
            .then(() => e2e.decode(item))
            .then((decoded) => {
              if (cancelled) return;
              notePaths(item, decoded);
              applyFeedEvent(decoded);
            })
            .catch((err: unknown) => console.warn('e2e: could not read a frame', err));
        };

        const applyFeedEvent = (item: FeedEvent) => {
          if (!hasSafeKeys(item as { user_id?: unknown; decision_id?: unknown })) return;
          const live = rosterPhase === 'done';
          if (rosterPhase === 'collecting') {
            if (item.type === 'user_joined') announced.add(item.user_id);
            else if (item.type !== 'room_settings' && item.type !== 'room_policy_update') finishRoster();
          } else if (live && 'timestamp' in item) {
            // Live traffic, not stored catch-up: calibrates the relay clock.
            observeServerTimestamp((item as { timestamp?: unknown }).timestamp);
          }
          if (item.type === 'room_settings' && rosterPhase === 'waiting') {
            rosterPhase = 'collecting';
            rosterTimer = setTimeout(finishRoster, ROSTER_SETTLE_MS);
          }
          recordFeedEvent(item);
          notifyFeedEvent(roomId, item); // browser notifications (notifications.ts)
          switch (item.type) {
            case 'user_joined':
              if (live) addActivity('join', `${item.user_id} joined`, serverNowSeconds());
              setUsers((prev) => {
                const prior = prev[item.user_id];
                if (prior) {
                  // A rejoin restores the greyed-out lane, history intact.
                  const agent = item.agent && item.agent !== prior.agent ? item.agent : prior.agent;
                  if (agent === prior.agent && prior.offlineSince == null) return prev;
                  return withEntry(prev, item.user_id, { ...prior, agent, offlineSince: null });
                }
                return withEntry(prev, item.user_id, { ...BLANK_ENTRY, agent: item.agent ?? 'unknown' });
              });
              break;

            case 'user_left':
              addActivity('leave', `${item.user_id} left`, serverNowSeconds());
              // Keep the lane, greyed out: a lane that silently vanishes reads
              // as "nobody to coordinate with" rather than "their coordination
              // just stopped". pruneOfflineLanes drops it after the TTL.
              setUsers((prev) => {
                const prior = prev[item.user_id];
                if (!prior || prior.offlineSince != null) return prev;
                return withEntry(prev, item.user_id, { ...prior, offlineSince: Date.now() });
              });
              setConflicts((prev) => prev.filter((c) => !c.users.includes(item.user_id)));
              break;

            case 'decision_feedback_update':
              setFeedback((prev) => capRecord(applyFeedbackUpdate(prev, item), MAX_TRACKED_DECISIONS));
              break;

            case 'activity_update':
            case 'intent_update':
            case 'plan_update':
            case 'edit_decision':
              if (item.type === 'edit_decision' && item.decision_id) {
                const id = item.decision_id;
                const scope = decisionScope(item);
                setDecisionScopes((prev) =>
                  prev[id] === scope ? prev : capRecord(withEntry(prev, id, scope), MAX_TRACKED_DECISIONS),
                );
              }
              // dashboard/laneReducer.ts: the same rules the replay fold uses.
              setUsers((prev) => {
                const next = applyLaneEvent(prev[item.user_id] ?? BLANK_ENTRY, item);
                return withEntry(
                  prev,
                  item.user_id,
                  item.type === 'activity_update' ? { ...next, timestamp: item.timestamp } : next,
                );
              });
              break;

            case 'room_settings':
              setRoomSettings(item);
              break;

            case 'room_policy_update':
              setRequireApproval(item.require_approval);
              break;

            case 'task_update':
              setTasks((prev) => applyTaskEvent(prev, item));
              break;

            case 'conflict_alert':
              setConflicts((prev) => upsertConflict(prev, item));
              if (isActionableConflict(item)) {
                setConflictHistory((prev) => {
                  const without = prev.filter((c) => conflictKey(c) !== conflictKey(item));
                  return [item, ...without].slice(0, 20);
                });
              }
              break;

            case 'hold_released':
              setReleasedAt((prev) => withEntry(prev, item.user_id, item.timestamp));
              addActivity('release', `${item.user_id}'s hold on ${item.file_path} was released`, item.timestamp);
              break;

            case 'queue_reordered':
              setReleasedAt((prev) => withEntry(prev, item.first_user_id, item.timestamp));
              setQueuedAt((prev) => {
                let next = prev;
                for (const waiting of item.waiting) next = withEntry(next, waiting, item.timestamp);
                return next;
              });
              addActivity(
                'reorder',
                `${item.first_user_id} goes first on ${item.file_path}${item.waiting.length ? `; ${item.waiting.join(', ')} waits` : ''}`,
                item.timestamp,
              );
              break;

            case 'nudged':
              addActivity(
                'ping',
                `${item.user_id} was pinged${item.delivered ? '' : ' (not connected)'}${item.text ? `: ${item.text}` : ''}`,
                item.timestamp,
              );
              break;
          }
        };

        switch (msg.type) {
          case 'room_rejected':
            if (isApprovalRefusal(msg.reason, awaitingApproval)) {
              refused = true;
              awaitingApproval = false;
              setStatus('refused');
              setAccessNotice(msg.reason);
              ws.close();
              break;
            }
            roomMissing = true;
            setStatus('room_missing');
            showToast(msg.reason);
            ws.close();
            break;

          case 'hivelane_feed':
            for (const item of msg.events) applyDecoded(item);
            break;

          case 'history_chunk':
            // Room history for the replay view, decoded like live frames.
            void publishHistoryChunk(msg, (item) => e2e.decode(item));
            break;

          case 'user_joined':
          case 'room_settings':
            // The first catch-up frame: the relay admitted this socket.
            if (rosterPhase === 'waiting') {
              attempt = 0;
              if (!awaitingApproval) setStatus('connected');
            }
            applyDecoded(msg);
            break;

          case 'user_left':
          case 'activity_update':
          case 'conflict_alert':
          case 'intent_update':
          case 'edit_decision':
          case 'plan_update':
          case 'decision_feedback_update':
          case 'room_policy_update':
          case 'task_update':
          case 'nudged':
          case 'hold_released':
          case 'queue_reordered':
            // Catch-up frames still arrive as individual messages via send().
            applyDecoded(msg);
            break;

          case 'join_pending':
            awaitingApproval = true;
            setStatus('pending');
            setAccessNotice(msg.reason);
            break;

          case 'join_resolved':
            if (awaitingApproval) {
              // Our own request: an owner let this dashboard in.
              awaitingApproval = false;
              setStatus('connected');
              setAccessNotice(null);
            }
            setJoinRequests((prev) => prev.filter((r) => r.request_id !== msg.request_id));
            break;

          case 'join_request':
            setJoinRequests((prev) => (prev.some((r) => r.request_id === msg.request_id) ? prev : [...prev, msg]));
            break;

          case 'rate_limited':
            console.warn('rate_limited:', String(msg.reason).slice(0, 200));
            retryAfterMs = Math.max(0, (msg.retry_after ?? 0) * 1000);
            break;

          case 'name_rejected':
            console.warn('name_rejected:', String(msg.reason).slice(0, 200));
            break;
        }
      };

      ws.onclose = (event) => {
        if (cancelled) return;
        if (rosterTimer) clearTimeout(rosterTimer);
        wsRef.current = null;
        // closeCodes.ts: 1013 (slow socket dropped) and 4408 (join timeout)
        // are transient and fall through to a backed-off reconnect.
        const action = closeAction(event.code);
        if (refused || action === 'refused') {
          refused = true;
          setStatus('refused');
          return;
        }
        if (roomMissing || action === 'room_missing') {
          roomMissing = true;
          setStatus('room_missing');
          return;
        }
        setStatus('disconnected');
        const delay = nextDelay();
        attempt += 1;
        retryTimer = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (rosterTimer) clearTimeout(rosterTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [wsUrl, e2eKey, roomId, joinAttempt, recordFeedEvent, showToast, addActivity]);

  const copyText = (label: string, text: string) => {
    void copyToClipboard(text).then((ok) => showToast(ok ? `${label} copied` : `Could not copy ${label.toLowerCase()}`));
  };

  const createNewRoom = async () => {
    setCreatingRoom(true);
    try {
      const code = await createRoom();
      showToast(`Room ${code} created; teammates need the new code`);
      navigate(roomPath(code));
    } catch {
      showToast('Could not create a room');
    } finally {
      setCreatingRoom(false);
    }
  };

  const showLane = (userId: string) => {
    navigateTo('agents');
    const file = users[userId]?.changes[0]?.file_path ?? null;
    setHighlightFile(file);
    window.setTimeout(() => {
      document.getElementById(`lane-${userId}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 50);
    window.clearTimeout(highlightTimer.current);
    highlightTimer.current = window.setTimeout(() => setHighlightFile(null), 8000);
  };

  const conflictedUsers = useMemo(() => {
    const ids = new Set<string>();
    for (const c of conflicts) for (const u of c.users) ids.add(u);
    return ids;
  }, [conflicts]);

  const totalDiffs = useMemo(() => Object.values(users).reduce((sum, u) => sum + u.changes.length, 0), [users]);

  // Per-lane status and history from the session log, then filters (shared
  // with the timeline and heat map, kept in the URL). Recomputed when the log,
  // the lanes or the coarse clock move. laneNow stands for "now".
  const laneNow = Math.floor(nowSeconds / (CLOCK_TICK_MS / 1000)) * (CLOCK_TICK_MS / 1000);
  // Tasks each member holds, for the claimed-task line on its lane card.
  const tasksByClaimer = useMemo(() => {
    const out: Record<string, Task[]> = {};
    for (const task of Object.values(tasks)) {
      if (task.status === 'claimed' && task.claimer && !out[task.claimer]) {
        out[task.claimer] = claimedBy(tasks, task.claimer);
      }
    }
    return out;
  }, [tasks]);
  const laneDerived = useMemo(
    () => Object.fromEntries(displayUserIds.map((id) => [id, deriveLane(dash.log, id, users[id], laneNow)])),
    [dash.log, users, displayUserIds, laneNow],
  );
  const visibleLaneIds = useMemo(
    () => displayUserIds.filter((id) => matchesLane(laneDerived[id].facts, dash.filters)),
    [displayUserIds, laneDerived, dash.filters],
  );
  const fileFilterText = dash.filters.file;
  const laneFileFilter = useMemo(
    () => (fileFilterText.trim() ? (path: string) => fileMatches(fileFilterText, path) : undefined),
    [fileFilterText],
  );

  // v3 lane views and the edits Hivelane is holding.
  const { lanes, held } = useMemo(() => {
    const touchTimes = new Map<string, number[]>();
    const lastFile = new Map<string, string>();
    for (const touch of dash.log.touches) {
      const list = touchTimes.get(touch.user_id) ?? [];
      list.push(touch.timestamp);
      touchTimes.set(touch.user_id, list);
      lastFile.set(touch.user_id, touch.file_path);
    }
    const heldOut: HeldEdit[] = [];
    const out: LaneView[] = displayUserIds.map((id) => {
      const entry = users[id];
      const derived = laneDerived[id];
      const decision = lastDecisionFor(dash.log, id) ?? entry.decisions[0] ?? null;
      const state = laneState({
        status: derived.status,
        lastDecision: decision,
        releasedAt: releasedAt[id] ?? null,
        queuedAt: queuedAt[id] ?? null,
        inConflict: conflictedUsers.has(id),
        nowSeconds: laneNow,
      });
      if ((state === 'blocked' || state === 'waiting') && decision?.decision === 'block' && decision.decision_id) {
        heldOut.push({
          decision_id: decision.decision_id,
          user_id: id,
          file_path: decision.file_path,
          others: decision.others,
          timestamp: decision.timestamp,
          queue_position: decision.queue_position ?? 0,
          reason: decision.reason,
        });
      }
      return {
        id,
        agent: entry.agent,
        state,
        color: personColor(id),
        intent: entry.intent,
        caption: entry.caption,
        prompt: entry.prompt,
        changes: entry.changes,
        decisions: entry.decisions,
        plan: entry.plan,
        branch: entry.branch,
        offlineSince: entry.offlineSince,
        derived,
        editTimes: touchTimes.get(id) ?? [],
        currentFile: lastFile.get(id) ?? entry.changes[0]?.file_path ?? null,
        inConflict: conflictedUsers.has(id),
        claimedTasks: tasksByClaimer[id] ?? [],
      };
    });
    return { lanes: out, held: heldOut.sort((a, b) => b.timestamp - a.timestamp) };
  }, [dash.log, displayUserIds, users, laneDerived, releasedAt, queuedAt, conflictedUsers, laneNow, tasksByClaimer]);

  const actions: V3Actions = {
    navigate: navigateTo,
    copyLink: () => copyText('Room link', roomLink(roomId)),
    copyText,
    newRoom: () => {
      if (!creatingRoom) void createNewRoom();
    },
    switchRoom: (code) => navigate(roomPath(code)),
    leaveRoom: () => navigate('/start'),
    openInvite: () => setInviteOpen(true),
    openCommand: () => setCommandOpen(true),
    openShortcuts: () => dash.setHelpOpen(true),
    openShare: () => dash.setShareOpen(true),
    releaseHold: (h) => {
      const sent = sendOwnerFrame(
        { type: 'hold_release', decision_id: h.decision_id },
        `Released ${h.user_id}'s hold on ${h.file_path}`,
      );
      if (sent) setReleasedAt((prev) => withEntry(prev, h.user_id, serverNowSeconds()));
      return sent;
    },
    goFirst: (filePath, userId) => {
      const relayPath = rawPaths.current.get(filePath) ?? filePath;
      return sendOwnerFrame(
        { type: 'queue_order', file_path: relayPath, repo_id: repoOf.current.get(relayPath) ?? '', first_user_id: userId },
        `${userId} goes first on ${filePath}`,
      );
    },
    ping: (userId, text) =>
      sendOwnerFrame({ type: 'nudge', to_user_id: userId, text: text ?? DEFAULT_PING }, `Pinged ${userId}`),
    dismissConflict: (alert) => {
      const key = conflictKey(alert);
      setConflicts((prev) => prev.filter((c) => conflictKey(c) !== key));
      showToast('Alert hidden on this dashboard');
    },
    toast: showToast,
    showLane,
    vote: sendVote,
    retryJoin: () => setJoinAttempt((n) => n + 1),
  };

  const model: V3Model = {
    roomId,
    wsUrl,
    view,
    status,
    rttMs,
    rttHistory,
    isOwner,
    settings: roomSettings,
    requireApproval,
    accessNotice,
    lanes,
    onlineIds: onlineUserIds,
    conflicts,
    conflictHistory,
    held,
    log: dash.log,
    stats: dash.stats,
    prevention: dash.prevention,
    feedback,
    votes: myVotes,
    activity,
    totalDiffs,
    nowSeconds,
    laneNow,
    tasks,
    highlightFile,
    actions,
  };

  const commands = buildCommands(actions, {
    isOwner,
    toggleTheme: () => {
      const dark = document.documentElement.dataset.theme === 'dark';
      setThemePref(dark ? 'light' : 'dark');
      showToast(dark ? 'Light theme' : 'Dark theme');
    },
  });

  const filterBar = (
    <FilterBar
      filters={dash.filters}
      onChange={dash.setFilters}
      people={dash.people}
      agentKinds={dash.agentKinds}
      searchRef={dash.searchRef}
      resultLabel={
        view === 'agents' && isFiltering(dash.filters)
          ? `${visibleLaneIds.length} of ${displayUserIds.length} lanes match`
          : undefined
      }
    />
  );

  const banners = (
    <>
      {roomSettings?.e2e || e2eState !== 'off' ? (
        <E2EBanner
          state={e2eState}
          hasKey={e2eKey != null}
          onKey={(key) => {
            rememberRoomKey(roomId, key);
            setE2EKey(key);
          }}
          onForget={() => {
            forgetRoomKey(roomId);
            setE2EKey(null);
          }}
        />
      ) : null}
      {isOwner ? <JoinRequestsBanner requests={joinRequests} onDecide={decideJoin} /> : null}
      {status === 'pending' || status === 'refused' || status === 'room_missing' ? (
        <section className="v3-section" role="status" aria-live="polite" style={{ borderBottom: '1px solid var(--v3-hairline)' }}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="m-0 text-[13.5px] flex items-center gap-2.5">
              <span className={`v3-dot ${status === 'pending' ? 'is-wait is-pulse' : 'is-block'}`} aria-hidden="true" />
              {status === 'pending'
                ? (accessNotice ?? "This room requires an owner's approval. Waiting for an owner to let this dashboard in…")
                : status === 'refused'
                  ? `This dashboard was not let into room ${roomId}: ${accessNotice ?? 'an owner did not approve it'}.`
                  : `Room ${roomId} is gone on the relay (expired or redeployed). Create a new room, then run hivelane up --room <new code> in each repo.`}
            </p>
            {status === 'refused' ? (
              <button type="button" className="v3-btn v3-btn--primary v3-btn--sm" onClick={actions.retryJoin}>
                Ask again
              </button>
            ) : status === 'room_missing' ? (
              <button type="button" className="v3-btn v3-btn--primary v3-btn--sm" onClick={actions.newRoom} disabled={creatingRoom}>
                {creatingRoom ? 'Creating…' : 'New room'}
              </button>
            ) : null}
          </div>
        </section>
      ) : null}
    </>
  );

  const feedView = (
    <div className="v3-grid v3-grid--single">
      <div className="v3-main">
        <PageHead
          title={
            lanes.length
              ? `${onlineUserIds.length} of ${lanes.length} ${lanes.length === 1 ? 'lane' : 'lanes'} live.`
              : 'No lanes yet.'
          }
          sub="One lane per agent: status, current intent, plan checklist and latest diffs, live."
        />
        {lanes.length ? (
          <section className="v3-section">
            {filterBar}
            {visibleLaneIds.length === 0 ? (
              <div className="v3-empty">
                <p className="v3-title">No lanes match these filters</p>
                <p className="v3-label">Clear a filter or widen the search to see everyone again.</p>
              </div>
            ) : (
              <div className="lane-grid">
                {visibleLaneIds.map((id, index) => (
                  <Lane
                    key={id}
                    userId={id}
                    state={users[id]}
                    inConflict={conflictedUsers.has(id)}
                    highlightFile={highlightFile}
                    index={index}
                    offlineSince={users[id].offlineSince}
                    plan={users[id].plan}
                    branch={users[id].branch}
                    feedback={feedback}
                    votes={myVotes}
                    onVote={sendVote}
                    status={laneDerived[id].status}
                    recentEdits={laneDerived[id].recentEdits}
                    editedPlanFiles={laneDerived[id].editedPlanFiles}
                    lastActivity={laneDerived[id].lastActivity}
                    nowSeconds={laneNow}
                    fileFilter={laneFileFilter}
                    claimedTasks={tasksByClaimer[id]}
                  />
                ))}
              </div>
            )}
          </section>
        ) : (
          <SetupGuide
            roomId={roomId}
            connected={status === 'connected'}
            anyAgent={false}
            e2e={roomSettings?.e2e ?? false}
            actions={actions}
          />
        )}
      </div>
    </div>
  );

  const invitePage = (
    <div className="v3-grid v3-grid--single">
      <div className="v3-main">
        <PageHead
          title="Bring your team into this room."
          sub="Each teammate joins once from their repo; their agent then shows up as a lane."
        />
        <section className="v3-section" style={{ maxWidth: 720 }}>
          <InviteContent roomId={roomId} settings={roomSettings} requireApproval={requireApproval} onCopy={copyText} />
        </section>
      </div>
    </div>
  );

  let page: React.ReactNode;
  switch (view) {
    case 'dashboard':
      page = <DashboardView model={model} />;
      break;
    case 'agents':
      page = feedView;
      break;
    case 'tasks':
      page = <TasksView tasks={tasks} nowSeconds={laneNow} />;
      break;
    case 'timeline':
      page = (
        <TimelineView
          log={dash.log}
          filters={dash.filters}
          prevention={dash.prevention}
          feedback={feedback}
          votes={myVotes}
          onVote={sendVote}
          nowSeconds={nowSeconds}
          filterBar={filterBar}
          counter={null}
        />
      );
      break;
    case 'heatmap':
      page = (
        <HeatMapView
          log={dash.log}
          filters={dash.filters}
          nowSeconds={nowSeconds}
          filterBar={filterBar}
          onOpenFile={dash.showFileInTimeline}
        />
      );
      break;
    case 'conflicts':
      page = <ConflictsView model={model} />;
      break;
    case 'history':
      page = <HistoryView model={model} />;
      break;
    case 'replay':
      page = (
        <ReplayView
          roomId={roomId}
          historyHours={roomSettings?.history_hours ?? 0}
          settingsKnown={roomSettings != null}
          connected={status === 'connected'}
          socketRef={wsRef}
        />
      );
      break;
    case 'analytics':
      page = <AnalyticsView model={model} scopes={decisionScopes} />;
      break;
    case 'notifications':
      page = <NotificationsSettings roomId={roomId} e2e={roomSettings?.e2e ?? false} />;
      break;
    case 'settings':
      page = (
        <SettingsView
          model={model}
          joinRequests={joinRequests}
          onSetRequireApproval={setApprovalPolicy}
          onDecide={decideJoin}
        />
      );
      break;
    case 'invite':
    case 'docs':
      page = invitePage;
      break;
  }

  return (
    <div className="v3-app">
      <Header model={model} />
      <div className="v3-body">
        <Sidebar
          view={view}
          onNavigate={navigateTo}
          conflictCount={conflicts.length + held.length}
          relayLive={status === 'connected'}
          relayLabel={CONN_LABEL[status]}
          rttMs={rttMs}
          expiresAt={roomSettings?.expires_at ?? null}
          nowSeconds={nowSeconds}
        />
        <main className="v3-sheet custom-scrollbar" id="main" key={view}>
          {banners}
          {page}
        </main>
      </div>

      <MobileNav view={view} onNavigate={navigateTo} conflictCount={conflicts.length + held.length} />
      <CommandMenu open={commandOpen} commands={commands} onClose={() => setCommandOpen(false)} />
      <InviteSheet open={inviteOpen} onClose={() => setInviteOpen(false)}>
        <InviteContent roomId={roomId} settings={roomSettings} requireApproval={requireApproval} onCopy={copyText} />
      </InviteSheet>
      <ShareCardDialog open={dash.shareOpen} stats={dash.stats} onClose={() => dash.setShareOpen(false)} onNotice={showToast} />
      <ShortcutHelp open={dash.helpOpen} onClose={() => dash.setHelpOpen(false)} />
      <Toasts toasts={toasts} onDismiss={dismissToast} />
      {/* Kept for screen readers: what the notch shows visually. */}
      <span className="sr-only" aria-live="polite">
        {held.length ? `${held.length} edits held` : conflicts.length ? `${conflicts.length} overlaps` : ''}
      </span>
    </div>
  );
}
