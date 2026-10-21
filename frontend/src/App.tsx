import { Mark, Mascot } from './brand/BrandArt';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import AddTeammate from './components/AddTeammate';
import ConflictBanner from './components/ConflictBanner';
import DeployModal from './components/DeployModal';
import DetectionQuality from './components/DetectionQuality';
import E2EBanner from './components/E2EBanner';
import JoinRequestsBanner from './components/JoinRequestsBanner';
import Lane from './components/Lane';
import RoomAccessPanel from './components/RoomAccessPanel';
import SharePanel from './components/SharePanel';
import Sidebar, { type AppView } from './components/Sidebar';
// Phase 5 dashboard views (heat map, timeline, filters, theme, shortcuts).
import FilterBar from './components/FilterBar';
import HeatMapView from './components/HeatMapView';
import MobileNav from './components/MobileNav';
import { PreventedCounter, ShareCardDialog } from './components/PreventedCounter';
import ShortcutHelp from './components/ShortcutHelp';
import ThemeToggle from './components/ThemeToggle';
import TimelineView from './components/TimelineView';
import { fileMatches, isFiltering, matchesLane } from './dashboard/filters';
import { deriveLane } from './dashboard/laneFacts';
import { useDashboardState, viewFromParam } from './dashboard/useDashboardState';
import { readViewParam } from './dashboard/useUrlState';
// Phase 5 notifications and replay (notifications.ts, replay.ts).
import NotificationsSettings from './components/NotificationsSettings';
import ReplayView from './components/ReplayView';
import { notifyFeedEvent } from './notifications';
import { publishHistoryChunk } from './replay';
import { relativeTime } from './dashboard/people';
import { observeServerTimestamp, serverNowSeconds } from './serverClock';
import { copyText } from './clipboard';
import { closeAction } from './closeCodes';
import {
  E2EDecoder,
  forgetRoomKey,
  rememberRoomKey,
  takeRoomKey,
  type E2EState,
} from './e2e';
import {
  aggregateQuality,
  applyFeedbackUpdate,
  decisionScope,
  type FeedbackMap,
  type Vote,
} from './detection';
import {
  normalizeRoomCode,
  relayHttpOrigin,
  roomLink,
  roomPath,
  roomWsUrl,
  upCommand,
} from './relayUrl';
import {
  createRoom,
  isApprovalRefusal,
  ownerToken,
  spectatorJoin,
} from './roomAccess';
import {
  DEFAULT_ROOM_SETTINGS,
  describeSettingsSource,
  describeWindows,
} from './roomSettings';
import type {
  ConflictAlert,
  DecisionFeedback,
  DecisionScope,
  FeedEvent,
  JoinDecision,
  JoinRequest,
  LaneState,
  PlanUpdate,
  RoomPolicy,
  RoomSettingsUpdate,
  ServerMessage,
} from './types';

/** Matches server/room.py CONFLICT_WINDOW_SECONDS. */
const CONFLICT_TTL_SECONDS = 120;
const CONFLICT_PRUNE_INTERVAL_MS = 5_000;
const MAX_LANE_DECISIONS = 3;
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

const BLANK_LANE: LaneState = {
  caption: null,
  changes: [],
  prompt: null,
  intent: null,
  decisions: [],
  agent: 'unknown',
  timestamp: null,
};

/**
 * A lane plus when its agent left the room (ms epoch, local clock — user_left
 * carries no timestamp), or null while connected. Kept here rather than in
 * LaneState because it is dashboard bookkeeping, not part of the wire format.
 */
type LaneEntry = LaneState & {
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

function isActionableConflict(alert: ConflictAlert): boolean {
  return alert.verdict === 'overlapping' || alert.verdict === 'related';
}

function isExpired(event: { timestamp: number }, nowSeconds: number): boolean {
  return nowSeconds - event.timestamp > CONFLICT_TTL_SECONDS;
}

/** One alert per file *and* pair: the relay judges each pair separately. */
function conflictKey(alert: ConflictAlert): string {
  return `${alert.file_path}\u0000${[...alert.users].sort().join('\u0000')}`;
}

/** The newest `max` keys of a record (insertion order), or `prev` when within bounds. */
function capRecord<T>(prev: Record<string, T>, max: number): Record<string, T> {
  const keys = Object.keys(prev);
  if (keys.length <= max) return prev;
  return Object.fromEntries(keys.slice(keys.length - max).map((k) => [k, prev[k]]));
}

/** A relay default that means "not known" ("" branch, "unknown" agent) reads as absent. */
function known<T extends string>(value: T | null | undefined, unknown: string): T | undefined {
  return value && value !== unknown ? value : undefined;
}

function pruneExpired(prev: ConflictAlert[]): ConflictAlert[] {
  const now = serverNowSeconds();
  const next = prev.filter((c) => !isExpired(c, now));
  return next.length === prev.length ? prev : next;
}

function pruneLaneDecisions(
  prev: Record<string, LaneEntry>,
): Record<string, LaneEntry> {
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
      next[id] = {
        ...lane,
        decisions: kept,
        plan: planExpired ? null : lane.plan,
      };
    }
  }
  return changed ? next : prev;
}

function pruneOfflineLanes(
  prev: Record<string, LaneEntry>,
): Record<string, LaneEntry> {
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

function upsertConflict(
  prev: ConflictAlert[],
  next: ConflictAlert,
): ConflictAlert[] {
  // Only this pair's earlier alert is replaced (or cleared by a non-actionable verdict).
  const key = conflictKey(next);
  const without = prev.filter((c) => conflictKey(c) !== key);
  if (!isActionableConflict(next) || isExpired(next, serverNowSeconds())) {
    return without.length === prev.length ? prev : without;
  }
  return [...without, next].sort((a, b) => b.timestamp - a.timestamp);
}

const STATUS_LABEL: Record<ConnStatus, string> = {
  connecting: 'Connecting',
  connected: 'Live',
  disconnected: 'Offline',
  room_missing: 'Room gone',
  pending: 'Awaiting approval',
  refused: 'Not admitted',
};

/** pending: held for an owner's approval; refused: an owner (or the clock) said no. */
type ConnStatus =
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'room_missing'
  | 'pending'
  | 'refused';

const TOP_NAV = ['Cluster', 'Logs', 'Analytics', 'Config'] as const;

const TOP_NAV_TO_VIEW: Record<(typeof TOP_NAV)[number], AppView> = {
  Cluster: 'dashboard',
  Logs: 'history',
  Analytics: 'analytics',
  Config: 'settings',
};

const VIEW_TO_TOP_NAV: Partial<Record<AppView, (typeof TOP_NAV)[number]>> = {
  dashboard: 'Cluster',
  agents: 'Cluster',
  timeline: 'Logs',
  heatmap: 'Analytics',
  invite: 'Cluster',
  history: 'Logs',
  conflicts: 'Logs',
  analytics: 'Analytics',
  settings: 'Config',
  docs: 'Config',
};

export default function App() {
  const navigate = useNavigate();
  const { code: rawCode } = useParams<{ code: string }>();
  const roomId = normalizeRoomCode(rawCode ?? '');
  const wsUrl = useMemo(() => roomWsUrl(roomId), [roomId]);

  const [users, setUsers] = useState<Record<string, LaneEntry>>({});
  const [conflicts, setConflicts] = useState<ConflictAlert[]>([]);
  const [conflictHistory, setConflictHistory] = useState<ConflictAlert[]>([]);
  // Conflict windows the relay enforces here; null until its catch-up arrives.
  const [roomSettings, setRoomSettings] = useState<RoomSettingsUpdate | null>(
    null,
  );
  const [status, setStatus] = useState<ConnStatus>('connecting');
  // Refreshes time-derived labels when no frames arrive. Deliberately coarse:
  // a 1 s tick here re-rendered the whole dashboard every second.
  const [, setClockMs] = useState(() => Date.now());
  // The open view also lives in ?view= so a filtered view is a shareable link.
  const [view, setView] = useState<AppView>(
    () => (viewFromParam(readViewParam(window.location.search)) ?? 'agents') as AppView,
  );
  // Derived, so views with no top-nav entry (replay, notifications) clear the underline.
  const topNav = VIEW_TO_TOP_NAV[view] ?? null;
  const [highlightFile, setHighlightFile] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [deployOpen, setDeployOpen] = useState(false);
  const [creatingRoom, setCreatingRoom] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // Detection-quality bookkeeping for decisions seen this session.
  const [decisionScopes, setDecisionScopes] = useState<
    Record<string, DecisionScope>
  >({});
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
  // Session log, URL filters, prevented-collision stats, overlays, shortcuts.
  const nowSeconds = serverNowSeconds();
  const dash = useDashboardState({
    onNavigate: (next) => navigateTo(next),
    feedback,
    nowSeconds,
  });
  // useReducer's dispatch: stable, so the connection effect never reconnects for it.
  const recordFeedEvent = dash.recordEvent;

  const sendAccessFrame = (frame: JoinDecision | RoomPolicy): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showToast('Not connected — try again');
      return false;
    }
    ws.send(JSON.stringify(frame));
    return true;
  };

  const decideJoin = (requestId: string, approve: boolean) => {
    if (!sendAccessFrame({ type: 'join_decision', request_id: requestId, approve })) return;
    // The relay confirms with join_resolved; drop it here at once anyway.
    setJoinRequests((prev) => prev.filter((r) => r.request_id !== requestId));
  };

  const setApprovalPolicy = (value: boolean) => {
    sendAccessFrame({ type: 'room_policy', require_approval: value });
  };

  const sendVoteNow = (decisionId: string, vote: Vote) => {
    if (myVotes[decisionId]) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN || status !== 'connected') {
      // While a dashboard awaits approval the relay drops its frames: say so.
      showToast(status === 'pending' ? 'Waiting for approval — vote not sent' : 'Not connected — vote not sent');
      return;
    }
    const frame: DecisionFeedback = {
      type: 'decision_feedback',
      decision_id: decisionId,
      useful: vote === 'useful',
    };
    ws.send(JSON.stringify(frame));
    setMyVotes((prev) => capRecord({ ...prev, [decisionId]: vote }, MAX_TRACKED_DECISIONS));
  };
  // A stable identity for memoized lanes and timeline rows; calls the latest closure.
  const sendVoteRef = useRef(sendVoteNow);
  useEffect(() => {
    sendVoteRef.current = sendVoteNow;
  });
  const sendVote = useCallback((decisionId: string, vote: Vote) => sendVoteRef.current(decisionId, vote), []);

  // One timer per purpose, cleared before it is re-armed (a second toast or
  // highlight was cut short by the first one's timer).
  const toastTimer = useRef<number | undefined>(undefined);
  const copiedTimer = useRef<number | undefined>(undefined);
  const highlightTimer = useRef<number | undefined>(undefined);
  useEffect(
    () => () => {
      window.clearTimeout(toastTimer.current);
      window.clearTimeout(copiedTimer.current);
      window.clearTimeout(highlightTimer.current);
    },
    [],
  );

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 1800);
  }, []);

  const navigateTo = (next: AppView) => {
    setView(next);
    dash.syncViewParam(next);
  };

  useEffect(() => {
    // Back/Forward (or a pasted link) changed ?view=: follow it. Views this
    // build does not list in URL_VIEWS are left alone.
    const fromUrl = viewFromParam(dash.viewParam) as AppView | null;
    if (!fromUrl) return;
    setView(fromUrl);
  }, [dash.viewParam]);

  const displayUsers = users;
  const displayConflicts = conflicts;
  const displayUserIds = useMemo(() => Object.keys(displayUsers), [displayUsers]);
  // Offline lanes stay on the board but are not "agents" in any count.
  const onlineUserIds = displayUserIds.filter(
    (id) => displayUsers[id].offlineSince == null,
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

  useEffect(() => {
    // A new room means the previous room's lanes/conflicts no longer apply.
    setUsers({});
    setConflicts([]);
    setConflictHistory([]);
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
          console.warn('Invalid WS message', event.data);
          return;
        }

        const applyDecoded = (item: FeedEvent) => {
          e2eChain = e2eChain
            .then(() => e2e.decode(item))
            .then((decoded) => {
              if (!cancelled) applyFeedEvent(decoded);
            })
            .catch((err: unknown) => console.warn('e2e: could not read a frame', err));
        };

        const applyFeedEvent = (item: FeedEvent) => {
          if (rosterPhase === 'collecting') {
            if (item.type === 'user_joined') announced.add(item.user_id);
            else if (item.type !== 'room_settings' && item.type !== 'room_policy_update') finishRoster();
          } else if (rosterPhase === 'done' && 'timestamp' in item) {
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
              setUsers((prev) => {
                const prior = prev[item.user_id];
                if (prior) {
                  // A rejoin restores the greyed-out lane, history intact.
                  const agent =
                    item.agent && item.agent !== prior.agent
                      ? item.agent
                      : prior.agent;
                  if (agent === prior.agent && prior.offlineSince == null) {
                    return prev;
                  }
                  return {
                    ...prev,
                    [item.user_id]: { ...prior, agent, offlineSince: null },
                  };
                }
                return {
                  ...prev,
                  [item.user_id]: {
                    ...BLANK_ENTRY,
                    agent: item.agent ?? 'unknown',
                  },
                };
              });
              break;

            case 'user_left':
              // Keep the lane, greyed out: a lane that silently vanishes reads
              // as "nobody to coordinate with" rather than "their coordination
              // just stopped". pruneOfflineLanes drops it after the TTL.
              setUsers((prev) => {
                const prior = prev[item.user_id];
                if (!prior || prior.offlineSince != null) return prev;
                return {
                  ...prev,
                  [item.user_id]: { ...prior, offlineSince: Date.now() },
                };
              });
              setConflicts((prev) =>
                prev.filter((c) => !c.users.includes(item.user_id)),
              );
              break;

            case 'activity_update':
              setUsers((prev) => {
                const prior = prev[item.user_id];
                // Prompt-only updates send changes: [] — keep the last diffs so
                // the lane doesn't blank out when a new Cursor prompt arrives.
                const changes =
                  item.changes.length > 0
                    ? item.changes
                    : (prior?.changes ?? []);
                return {
                  ...prev,
                  [item.user_id]: {
                    ...(prior ?? BLANK_ENTRY),
                    caption: item.caption ?? prior?.caption ?? null,
                    changes,
                    prompt: item.prompt,
                    timestamp: item.timestamp,
                    // The relay sends branch "" and agent "unknown" when it does
                    // not know them: those must not wipe what the lane knows.
                    agent: known(item.agent, 'unknown') ?? prior?.agent ?? 'unknown',
                    branch: known(item.branch, '') ?? prior?.branch ?? '',
                  },
                };
              });
              break;

            case 'intent_update':
              setUsers((prev) => {
                const prior = prev[item.user_id];
                return {
                  ...prev,
                  [item.user_id]: {
                    ...(prior ?? BLANK_ENTRY),
                    intent: item.prompt,
                    agent: known(item.agent, 'unknown') ?? prior?.agent ?? 'unknown',
                  },
                };
              });
              break;

            case 'plan_update':
              setUsers((prev) => {
                const prior = prev[item.user_id] ?? BLANK_ENTRY;
                return {
                  ...prev,
                  [item.user_id]: {
                    ...prior,
                    plan: item,
                    agent: known(item.agent, 'unknown') ?? prior.agent,
                  },
                };
              });
              break;

            case 'decision_feedback_update':
              setFeedback((prev) => capRecord(applyFeedbackUpdate(prev, item), MAX_TRACKED_DECISIONS));
              break;

            case 'edit_decision':
              if (item.decision_id) {
                const id = item.decision_id;
                const scope = decisionScope(item);
                setDecisionScopes((prev) =>
                  prev[id] === scope ? prev : capRecord({ ...prev, [id]: scope }, MAX_TRACKED_DECISIONS),
                );
              }
              setUsers((prev) => {
                const prior = prev[item.user_id] ?? BLANK_ENTRY;
                const decisions = [
                  item,
                  ...prior.decisions.filter(
                    (d) => d.file_path !== item.file_path,
                  ),
                ].slice(0, MAX_LANE_DECISIONS);
                return {
                  ...prev,
                  [item.user_id]: {
                    ...prior,
                    decisions,
                    agent: known(item.agent, 'unknown') ?? prior.agent,
                  },
                };
              });
              break;

            case 'room_settings':
              setRoomSettings(item);
              break;

            case 'room_policy_update':
              setRequireApproval(item.require_approval);
              break;

            case 'conflict_alert':
              setConflicts((prev) => upsertConflict(prev, item));
              if (isActionableConflict(item)) {
                setConflictHistory((prev) => {
                  const without = prev.filter(
                    (c) => conflictKey(c) !== conflictKey(item),
                  );
                  return [item, ...without].slice(0, 20);
                });
              }
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
            for (const item of msg.events) {
              applyDecoded(item);
            }
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
            setJoinRequests((prev) =>
              prev.some((r) => r.request_id === msg.request_id) ? prev : [...prev, msg],
            );
            break;

          case 'rate_limited':
            console.warn('rate_limited:', msg.reason);
            retryAfterMs = Math.max(0, (msg.retry_after ?? 0) * 1000);
            break;

          case 'name_rejected':
            console.warn('name_rejected:', msg.reason);
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
  }, [wsUrl, e2eKey, roomId, joinAttempt, recordFeedEvent, showToast]);

  const copyShareUrl = async () => {
    if (!(await copyText(roomLink(roomId)))) {
      showToast('Could not copy link');
      return;
    }
    setCopied(true);
    showToast('Room link copied');
    window.clearTimeout(copiedTimer.current);
    copiedTimer.current = window.setTimeout(() => setCopied(false), 1500);
  };

  const createNewRoom = async () => {
    if (
      !window.confirm(
        'Create a new room and leave this session? Teammates will need the new code.',
      )
    ) {
      return;
    }
    setCreatingRoom(true);
    try {
      navigate(roomPath(await createRoom()));
    } catch {
      showToast('Could not create room');
      setCreatingRoom(false);
    }
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

  const hasLanes = displayUserIds.length > 0;

  const serverLabel = `Relay: ${STATUS_LABEL[status].toLowerCase()}`;

  const handleViewOverlap = (filePath: string) => {
    navigateTo('agents');
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
    window.clearTimeout(highlightTimer.current);
    highlightTimer.current = window.setTimeout(() => setHighlightFile(null), 8000);
  };

  const handleDismiss = (alert: ConflictAlert) => {
    const key = conflictKey(alert);
    setConflicts((prev) => prev.filter((c) => conflictKey(c) !== key));
    showToast('Alert hidden on this dashboard');
  };

  const historyRows = conflictHistory;

  const showAgentGrid = view === 'agents';
  const activeConflictCount = displayConflicts.length;
  const historyCount = historyRows.length;

  // Phase 5: per-lane status and history from the session log, then filters
  // (shared with the timeline and heat map, kept in the URL).
  // Recomputed when the log, the lanes or the coarse clock move, not on every
  // unrelated render (toasts, copy state, dialogs). laneNow stands for "now".
  const laneNow = Math.floor(nowSeconds / (CLOCK_TICK_MS / 1000)) * (CLOCK_TICK_MS / 1000);
  const laneDerived = useMemo(
    () =>
      Object.fromEntries(
        displayUserIds.map((id) => [id, deriveLane(dash.log, id, displayUsers[id], laneNow)]),
      ),
    [dash.log, displayUsers, displayUserIds, laneNow],
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
  const preventedCounter = (
    <PreventedCounter
      prevention={dash.prevention}
      stats={dash.stats}
      onShare={() => dash.setShareOpen(true)}
    />
  );

  return (
    <div className="app-shell overflow-hidden flex flex-col bg-background">
      <header className="glass-header shrink-0 z-50">
        <div className="flex items-center justify-between gap-2 sm:gap-4 px-3 sm:px-4 h-12">
          <div className="flex items-center gap-3 min-w-0">
            <button
              type="button"
              className="inline-flex items-center gap-2 font-medium text-[13px] tracking-[-0.02em] text-on-surface whitespace-nowrap hover:text-primary transition-colors"
              onClick={() => navigate('/')}
              title="Back to Hivelane home"
            >
              <Mark size={20} />
              Hivelane
            </button>
            <div
              className="inline-flex items-center gap-2 status-pill rounded-full px-2.5 py-1 min-h-[22px]"
              title={wsUrl}
            >
              <span
                aria-hidden="true"
                className={`w-1.5 h-1.5 rounded-full ${
                  status === 'connected'
                    ? 'bg-emerald shadow-live'
                    : status === 'connecting' || status === 'pending'
                      ? 'bg-amber animate-pulse-dot'
                      : 'bg-error'
                }`}
              />
              <span className="sr-only sm:not-sr-only text-[12px] font-medium text-on-muted">
                {serverLabel}
              </span>
              {roomSettings?.e2e ? (
                <span
                  className="text-[11px]"
                  role="img"
                  title="End-to-end encrypted room: the relay sees only hashes and ciphertext"
                  aria-label="End-to-end encrypted"
                >
                  🔒
                </span>
              ) : null}
            </div>
          </div>

          <nav className="hidden lg:flex items-center gap-1">
            {TOP_NAV.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => navigateTo(TOP_NAV_TO_VIEW[item])}
                aria-current={topNav === item ? 'page' : undefined}
                className={`relative text-[12px] font-medium transition-colors px-3 py-1.5 rounded-md ${
                  topNav === item
                    ? 'text-on-surface'
                    : 'text-on-muted hover:text-on-surface hover:bg-ink/[0.04]'
                }`}
              >
                {item}
                {topNav === item ? (
                  <span className="absolute left-3 right-3 -bottom-[9px] h-px bg-on-surface" />
                ) : null}
              </button>
            ))}
          </nav>

          <div className="flex items-center gap-2 sm:gap-2.5 shrink-0">
            <div className="hidden md:flex items-center gap-2">
              <div className="stat-chip px-2 py-1 flex items-baseline gap-1.5">
                <span className="text-[12px] text-on-dim">Agents</span>
                <span className="font-mono text-[12px] text-on-surface tabular-nums">
                  {onlineUserIds.length}
                </span>
              </div>
              <div className="stat-chip px-2 py-1 flex items-baseline gap-1.5">
                <span className="text-[12px] text-on-dim">Diffs</span>
                <span className="font-mono text-[12px] text-on-surface tabular-nums">
                  {totalDiffs}
                </span>
              </div>
            </div>

            <div className="hidden sm:flex items-center gap-1">
              <ThemeToggle />
              <button
                type="button"
                className="icon-btn p-1.5 hidden md:inline-flex"
                title="Keyboard shortcuts (?)"
                aria-label="Keyboard shortcuts"
                onClick={() => dash.setHelpOpen(true)}
              >
                <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
                  keyboard
                </span>
              </button>
            </div>

            <div className="hidden sm:flex items-center gap-0.5 ml-0.5">
              <button
                type="button"
                className="icon-btn p-1.5"
                title={copied ? 'Link copied' : 'Share session'}
                aria-label={copied ? 'Room link copied' : 'Copy room link'}
                onClick={() => void copyShareUrl()}
              >
                <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
                  {copied ? 'check' : 'link'}
                </span>
              </button>
              <button
                type="button"
                className="icon-btn p-1.5 relative"
                title="Active conflicts"
                aria-label={
                  displayConflicts.length > 0
                    ? `Active conflicts (${displayConflicts.length})`
                    : 'Active conflicts'
                }
                onClick={() => navigateTo('conflicts')}
              >
                <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
                  warning
                </span>
                {displayConflicts.length > 0 ? (
                  <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 bg-error rounded-full ring-2 ring-surface" />
                ) : null}
              </button>
              <button
                type="button"
                className="icon-btn p-1.5"
                title="Settings"
                aria-label="Settings"
                onClick={() => navigateTo('settings')}
              >
                <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
                  settings
                </span>
              </button>
            </div>

            <button
              type="button"
              className="deploy-btn mobile-readonly px-3 py-1.5 text-[12px] font-medium ml-1"
              onClick={() => navigateTo('invite')}
            >
              Invite teammates
            </button>
          </div>
        </div>
      </header>

      <ConflictBanner
        conflicts={displayConflicts}
        onDismiss={handleDismiss}
        onViewOverlap={handleViewOverlap}
        onShowAll={() => navigateTo('conflicts')}
      />

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

      {isOwner ? (
        <JoinRequestsBanner requests={joinRequests} onDecide={decideJoin} />
      ) : null}

      {status === 'pending' || status === 'refused' ? (
        <div
          className="px-5 py-3 bg-amber/10 border-b border-amber/30 z-40"
          role="status"
          aria-live="polite"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 max-w-[1600px]">
            <p className="text-[13px] text-on-surface m-0">
              {status === 'pending'
                ? (accessNotice ??
                  "This room requires an owner's approval. Waiting for an owner to let this dashboard in…")
                : `This dashboard was not let into room ${roomId}: ${
                    accessNotice ?? 'an owner did not approve it'
                  }.`}
            </p>
            {status === 'refused' ? (
              <button
                type="button"
                className="deploy-btn rounded-xl px-4 py-1.5 text-[11px] font-bold shrink-0"
                onClick={() => setJoinAttempt((n) => n + 1)}
              >
                Ask again
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {status === 'room_missing' ? (
        <div className="px-5 py-3 bg-amber/10 border-b border-amber/30 z-40">
          <div className="flex flex-wrap items-center justify-between gap-3 max-w-[1600px]">
            <p className="text-[13px] text-on-surface m-0">
              Room <span className="font-mono text-primary">{roomId}</span> is
              gone on the relay (expired or redeployed). Create a new room,
              then run{' '}
              <code className="font-mono">hivelane up --room &lt;new code&gt;</code>{' '}
              in each repo.
            </p>
            <button
              type="button"
              className="deploy-btn rounded-xl px-4 py-1.5 text-[11px] font-bold shrink-0"
              onClick={() => void createNewRoom()}
              disabled={creatingRoom}
            >
              {creatingRoom ? 'Creating…' : 'New room'}
            </button>
          </div>
        </div>
      ) : null}

      <div className="flex-1 flex overflow-hidden">
        <Sidebar
          view={view}
          onNavigate={navigateTo}
          conflictCount={displayConflicts.length}
          roomId={roomId}
          onCopyRoom={() => void copyShareUrl()}
          copied={copied}
          onInvite={() => navigateTo('invite')}
          onNewRoom={() => void createNewRoom()}
          creatingRoom={creatingRoom}
        />

        <main className="flex-1 min-w-0 overflow-y-auto overflow-x-hidden custom-scrollbar relative workspace-canvas pb-20 md:pb-0">
          {view === 'dashboard' ? (
            <div className="p-5 sm:p-8 lg:p-10 max-w-[1100px]">
              <h1 className="text-[28px] font-medium tracking-[-0.03em] text-on-surface mb-1">
                Dashboard
              </h1>
              <p className="text-[14px] text-on-muted mb-8">
                Room overview for{' '}
                <span className="font-mono text-primary">{roomId}</span>
              </p>
              {preventedCounter}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-px mb-8 bg-outline rounded-lg overflow-hidden border border-outline">
                {[
                  {
                    label: 'Active agents',
                    value: String(onlineUserIds.length).padStart(2, '0'),
                    action: () => navigateTo('agents'),
                    cta: 'View agents',
                  },
                  {
                    label: 'Total diffs',
                    value: String(totalDiffs).padStart(2, '0'),
                    action: () => navigateTo('agents'),
                    cta: 'Open lanes',
                  },
                  {
                    label: 'Active conflicts',
                    value: String(activeConflictCount).padStart(2, '0'),
                    action: () => navigateTo('conflicts'),
                    cta: 'Review',
                  },
                  {
                    label: 'Relay',
                    value: STATUS_LABEL[status],
                    action: () => navigateTo('settings'),
                    cta: 'Settings',
                  },
                ].map((card) => (
                  <button
                    key={card.label}
                    type="button"
                    onClick={card.action}
                    className="bg-surface p-5 text-left hover:bg-surface-raised transition-colors"
                  >
                    <p className="section-label m-0">{card.label}</p>
                    <p className="font-mono text-[28px] font-medium tracking-tight text-on-surface mt-3 mb-2 tabular-nums">
                      {card.value}
                    </p>
                    <span className="text-[13px] text-primary">
                      {card.cta}
                    </span>
                  </button>
                ))}
              </div>
              <div className="flex flex-wrap gap-3">
                <button
                  type="button"
                  className="deploy-btn px-3.5 py-2 text-[13px] font-medium"
                  onClick={() => navigateTo('invite')}
                >
                  Invite teammates
                </button>
                <button
                  type="button"
                  className="rounded-md border border-outline-soft px-3.5 py-2 text-[13px] font-medium text-on-muted hover:text-on-surface hover:bg-ink/[0.04] transition-colors"
                  onClick={() => void copyShareUrl()}
                >
                  Copy room link
                </button>
                <button
                  type="button"
                  className="rounded-md border border-outline-soft px-3.5 py-2 text-[13px] font-medium text-on-muted hover:text-on-surface hover:bg-ink/[0.04] transition-colors"
                  onClick={() => navigateTo('agents')}
                >
                  Open Hivelane feed
                </button>
              </div>
            </div>
          ) : null}

          {showAgentGrid ? (
            <div className="p-4 sm:p-6 lg:p-8">
              <div className="flex items-end justify-between gap-4 mb-5 max-w-[1400px]">
                <div>
                  <h1 className="text-xl font-bold tracking-tight text-on-surface m-0">
                    Hivelane feed
                  </h1>
                  <p className="text-[13px] text-on-muted mt-1 mb-0">
                    One lane per person: status, current intent, plan checklist and
                    latest diffs (live, batched every 500 ms).
                  </p>
                </div>
              </div>
              {hasLanes ? (
                <>
                  {filterBar}
                  {visibleLaneIds.length === 0 ? (
                    <div className="lane-shell rounded-2xl p-6 max-w-[46rem]">
                      <p className="text-[14px] text-on-surface font-medium m-0">
                        No lanes match these filters
                      </p>
                      <p className="text-[13px] text-on-muted mt-2 mb-0">
                        Clear a filter or widen the search to see everyone again.
                      </p>
                    </div>
                  ) : (
                    <div className="lane-grid">
                      {visibleLaneIds.map((id, index) => (
                        <Lane
                          key={id}
                          userId={id}
                          state={displayUsers[id]}
                          inConflict={conflictedUsers.has(id)}
                          highlightFile={
                            conflictedUsers.has(id) ? highlightFile : null
                          }
                          index={index}
                          offlineSince={displayUsers[id].offlineSince}
                          plan={displayUsers[id].plan}
                          branch={displayUsers[id].branch}
                          feedback={feedback}
                          votes={myVotes}
                          onVote={sendVote}
                          status={laneDerived[id].status}
                          recentEdits={laneDerived[id].recentEdits}
                          editedPlanFiles={laneDerived[id].editedPlanFiles}
                          lastActivity={laneDerived[id].lastActivity}
                          nowSeconds={laneNow}
                          fileFilter={laneFileFilter}
                        />
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <div className="max-w-[46rem] space-y-4">
                  <div className="lane-shell rounded-2xl p-6">
                    <Mascot pose="clear" size={72} className="mb-3" />
                    <p className="text-[14px] text-on-surface font-medium m-0">
                      No agents connected yet
                    </p>
                    <p className="text-[13px] text-on-muted mt-2 mb-0">
                      Share this room. Each teammate runs the setup line in their
                      repo, and their agent appears here as a lane.{' '}
                      <button
                        type="button"
                        className="text-primary font-semibold hover:text-primary/80 transition-colors"
                        onClick={() => navigateTo('invite')}
                      >
                        Step-by-step setup →
                      </button>
                    </p>
                  </div>
                  <SharePanel roomId={roomId} settings={roomSettings} requireApproval={requireApproval} />
                </div>
              )}
            </div>
          ) : null}

          {view === 'timeline' ? (
            <TimelineView
              log={dash.log}
              filters={dash.filters}
              prevention={dash.prevention}
              feedback={feedback}
              votes={myVotes}
              onVote={sendVote}
              nowSeconds={nowSeconds}
              filterBar={filterBar}
              counter={preventedCounter}
            />
          ) : null}

          {view === 'heatmap' ? (
            <HeatMapView
              log={dash.log}
              filters={dash.filters}
              nowSeconds={nowSeconds}
              filterBar={filterBar}
              onOpenFile={dash.showFileInTimeline}
            />
          ) : null}

          {view === 'analytics' ? (
            <div className="p-6 lg:p-8 max-w-[1400px] animate-fade-up">
              <h1 className="text-xl font-bold tracking-tight mb-1">Analytics</h1>
              <p className="text-[13px] text-on-muted mb-6">
                Session metrics for this room.
              </p>
              <DetectionQuality
                rows={aggregateQuality(decisionScopes, feedback)}
              />
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div className="lane-shell rounded-2xl p-5">
                  <p className="section-label m-0">Agents online</p>
                  <p className="font-mono text-3xl font-bold mt-3 mb-0 text-primary">
                    {onlineUserIds.length}
                  </p>
                </div>
                <div className="lane-shell rounded-2xl p-5">
                  <p className="section-label m-0">Files changed</p>
                  <p className="font-mono text-3xl font-bold mt-3 mb-0">
                    {totalDiffs}
                  </p>
                </div>
                <div className="lane-shell rounded-2xl p-5">
                  <p className="section-label m-0">Conflicts logged</p>
                  <p className="font-mono text-3xl font-bold mt-3 mb-0 text-error">
                    {historyCount}
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="rounded-xl border border-outline-soft px-4 py-2.5 text-[13px] font-semibold text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                onClick={() => navigateTo('history')}
              >
                Open conflict history
              </button>
            </div>
          ) : null}

          {view === 'docs' ? (
            <div className="p-6 lg:p-8 max-w-2xl animate-fade-up">
              <h1 className="text-xl font-bold tracking-tight mb-1">Docs</h1>
              <p className="text-[13px] text-on-muted mb-6">
                How to connect agents to Hivelane.
              </p>
              <div className="lane-shell rounded-2xl p-5 space-y-4 text-[13px] text-on-muted leading-relaxed">
                <p className="m-0">
                  1. Share this room&apos;s link or code{' '}
                  <code className="text-primary font-mono">{roomId}</code> with
                  teammates.
                </p>
                <p className="m-0">
                  2. On each machine, install once with{' '}
                  <code className="text-primary font-mono">pipx install git+https://github.com/AdrianShah/Hivelane</code>
                  , then run{' '}
                  <code className="text-primary font-mono">{upCommand(roomId)}</code>{' '}
                  in the repo the agent works in. It sets up the agent hooks and the
                  Hivelane daemon.
                </p>
                <p className="m-0">
                  3. Let the agents work — activity and conflicts appear on this
                  board. {describeWindows(roomSettings ?? DEFAULT_ROOM_SETTINGS)}.
                </p>
                <div className="flex flex-wrap gap-2 pt-2">
                  <button
                    type="button"
                    className="deploy-btn rounded-xl px-4 py-2 text-[11px] font-bold"
                    onClick={() => navigateTo('invite')}
                  >
                    Invite teammates
                  </button>
                  <button
                    type="button"
                    className="rounded-xl border border-outline-soft px-4 py-2 text-[13px] font-semibold text-on-muted hover:text-on-surface transition-colors"
                    onClick={() => setDeployOpen(true)}
                  >
                    Setup commands
                  </button>
                  <button
                    type="button"
                    className="rounded-xl border border-outline-soft px-4 py-2 text-[13px] font-semibold text-on-muted hover:text-on-surface transition-colors"
                    onClick={() =>
                      window.open(
                        `${relayHttpOrigin()}/docs`,
                        '_blank',
                        'noopener,noreferrer',
                      )
                    }
                  >
                    API reference
                  </button>
                </div>
              </div>
            </div>
          ) : null}

          {view === 'invite' ? (
            <AddTeammate roomId={roomId} settings={roomSettings} requireApproval={requireApproval} />
          ) : null}

          {view === 'replay' ? (
            <ReplayView
              roomId={roomId}
              historyHours={roomSettings?.history_hours ?? 0}
              settingsKnown={roomSettings != null}
              connected={status === 'connected'}
              socketRef={wsRef}
            />
          ) : null}

          {view === 'notifications' ? (
            <NotificationsSettings roomId={roomId} e2e={roomSettings?.e2e ?? false} />
          ) : null}

          {view === 'conflicts' || view === 'history' ? (
            <div className="p-6 lg:p-8 max-w-[1400px] animate-fade-up">
              <h1 className="text-xl font-bold tracking-tight text-on-surface mb-1">
                {view === 'conflicts' ? 'Active Conflicts' : 'Conflict History'}
              </h1>
              <p className="text-[13px] text-on-muted mb-6">
                Overlaps across the agent cluster for this session.
              </p>
              <div className="lane-shell rounded-2xl overflow-x-auto custom-scrollbar">
                <table className="w-full min-w-[36rem] text-left border-collapse">
                  <thead>
                    <tr className="border-b border-outline-soft bg-shade/15">
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
                      (view === 'conflicts'
                        ? displayConflicts
                        : historyRows
                      ).map((c) => (
                        <tr
                          key={`${conflictKey(c)}-${c.timestamp}`}
                          className="hover:bg-ink/[0.03] transition-colors"
                        >
                          <td className="px-5 py-4 text-[13px] text-on-muted tabular-nums">
                            {relativeTime(c.timestamp, nowSeconds)}
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
                      ))
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
                  <p className="text-[13px] text-on-surface mt-2 mb-0">
                    {serverLabel}
                  </p>
                </div>
                <div className="h-px bg-outline-soft" />
                <div>
                  <label className="section-label">Conflict windows</label>
                  <p className="text-[13px] text-on-surface mt-2 mb-0">
                    {describeWindows(roomSettings ?? DEFAULT_ROOM_SETTINGS)}
                  </p>
                  <p className="text-[12px] text-on-muted mt-1 mb-0">
                    {describeSettingsSource(roomSettings)}
                  </p>
                </div>
                <div className="h-px bg-outline-soft" />
                <div>
                  <span className="section-label" id="appearance-label">
                    Appearance
                  </span>
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <ThemeToggle showLabels />
                    <span className="hidden md:inline-flex">
                      <button
                        type="button"
                        className="filter-chip"
                        onClick={() => dash.setHelpOpen(true)}
                      >
                        <span className="material-symbols-outlined text-[15px]" aria-hidden="true">
                          keyboard
                        </span>
                        Keyboard shortcuts <kbd>?</kbd>
                      </button>
                    </span>
                  </div>
                </div>
                <div className="h-px bg-outline-soft md:hidden" />
                {/* Phones: the bottom bar's "More" lands here, so list the rest. */}
                <nav className="md:hidden" aria-label="More views">
                  <span className="section-label">More views</span>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {(
                      [
                        ['dashboard', 'Dashboard'],
                        ['analytics', 'Analytics'],
                        ['history', 'History'],
                        ['replay', 'Replay'],
                        ['notifications', 'Notifications'],
                        ['invite', 'Invite'],
                        ['docs', 'Docs'],
                      ] as const
                    ).map(([id, label]) => (
                      <button
                        key={id}
                        type="button"
                        className="filter-chip"
                        onClick={() => navigateTo(id)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </nav>
                <div className="h-px bg-outline-soft" />
                <RoomAccessPanel
                  isOwner={isOwner}
                  requireApproval={requireApproval}
                  requests={joinRequests}
                  connected={status === 'connected'}
                  onSetRequireApproval={setApprovalPolicy}
                  onDecide={decideJoin}
                />
                <div className="h-px bg-outline-soft" />
                <div className="flex flex-wrap gap-2 pt-1">
                  <button
                    type="button"
                    className="rounded-lg border border-outline-soft px-3 py-2 text-[13px] font-semibold text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                    onClick={() => void copyShareUrl()}
                  >
                    Copy room link
                  </button>
                  <button
                    type="button"
                    className="rounded-lg border border-outline-soft px-3 py-2 text-[13px] font-semibold text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                    onClick={() => navigateTo('invite')}
                  >
                    Invite teammates
                  </button>
                  <button
                    type="button"
                    className="rounded-lg border border-outline-soft px-3 py-2 text-[13px] font-semibold text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                    onClick={() => void createNewRoom()}
                    disabled={creatingRoom}
                  >
                    {creatingRoom ? 'Creating…' : 'New room'}
                  </button>
                  <button
                    type="button"
                    className="rounded-lg border border-outline-soft px-3 py-2 text-[13px] font-semibold text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                    onClick={() => navigate('/')}
                  >
                    Leave room
                  </button>
                </div>
              </div>
            </div>
          ) : null}

          <button
            type="button"
            className="fab fixed bottom-6 right-6 w-12 h-12 rounded-full hidden md:flex items-center justify-center z-50"
            title="Invite teammates"
            aria-label="Invite teammates"
            onClick={() => navigateTo('invite')}
          >
            <span className="material-symbols-outlined text-[26px]" aria-hidden="true">
              person_add
            </span>
          </button>
        </main>
      </div>

      <MobileNav view={view} onNavigate={navigateTo} conflictCount={displayConflicts.length} />
      <ShareCardDialog
        open={dash.shareOpen}
        stats={dash.stats}
        onClose={() => dash.setShareOpen(false)}
        onNotice={showToast}
      />
      <ShortcutHelp open={dash.helpOpen} onClose={() => dash.setHelpOpen(false)} />

      <DeployModal
        open={deployOpen}
        roomId={roomId}
        onClose={() => setDeployOpen(false)}
        onCopied={(label) => showToast(`${label} copied`)}
      />

      {toast ? (
        <div
          role="status"
          className="fixed bottom-20 md:bottom-6 left-1/2 -translate-x-1/2 z-[110] rounded-full bg-surface-raised border border-outline-soft px-4 py-2 text-[12px] font-semibold text-on-surface shadow-card animate-fade-up">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
