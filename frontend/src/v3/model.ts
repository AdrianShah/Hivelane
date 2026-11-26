/**
 * What the v3 shell and its views read. App.tsx owns the relay connection
 * and every piece of state; it hands the views this one object, so views stay
 * presentational and the connection logic stays in one place.
 */
import type { AppView } from '../components/Sidebar';
import type { FeedbackMap, Vote } from '../detection';
import type { LaneDerived } from '../dashboard/laneFacts';
import type { LaneState } from '../dashboard/laneState';
import type { PreventionSummary } from '../dashboard/prevented';
import type { SessionLog } from '../dashboard/sessionLog';
import type { RoomStats } from '../dashboard/stats';
import type {
  AgentKind,
  ConflictAlert,
  EditDecision,
  FileChange,
  PendingFile,
  PlanUpdate,
  RoomSettingsUpdate,
  Task,
} from '../types';

export type ConnStatus = 'connecting' | 'connected' | 'disconnected' | 'room_missing' | 'pending' | 'refused';

export const CONN_LABEL: Record<ConnStatus, string> = {
  connecting: 'Connecting',
  connected: 'Live',
  disconnected: 'Offline',
  room_missing: 'Room gone',
  pending: 'Awaiting approval',
  refused: 'Not admitted',
};

/** One lane as the v3 views see it. */
export interface LaneView {
  id: string;
  agent: AgentKind;
  state: LaneState;
  color: string;
  intent: string | null;
  caption: string | null;
  prompt: string | null;
  changes: FileChange[];
  decisions: EditDecision[];
  plan: PlanUpdate | null;
  /** Files changed but not pushed yet, across the lane's repos, oldest first. */
  pendingFiles: PendingFile[];
  /** A report was cut at its cap: there are more than pendingFiles shows. */
  pendingTruncated: boolean;
  branch: string;
  offlineSince: number | null;
  derived: LaneDerived;
  /** Relay timestamps (seconds) of this lane's edits this session. */
  editTimes: number[];
  /** The file it touched last, if any. */
  currentFile: string | null;
  inConflict: boolean;
  claimedTasks: Task[];
  /** This lane's agent plan (its todo list as room tasks): the step it is on and how far it is. */
  planProgress: { current: Task | null; done: number; total: number } | null;
}

/** An edit Hivelane is holding right now (blocked, not released). */
export interface HeldEdit {
  decision_id: string;
  user_id: string;
  file_path: string;
  others: string[];
  timestamp: number;
  queue_position: number;
  reason: string;
}

export type ActivityKind = 'join' | 'leave' | 'ping' | 'release' | 'reorder';

/** Room events the rail shows besides the session log's collisions. */
export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  text: string;
  timestamp: number;
}

export interface V3Actions {
  navigate: (view: AppView) => void;
  copyLink: () => void;
  copyText: (label: string, text: string) => void;
  newRoom: () => void;
  switchRoom: (code: string) => void;
  leaveRoom: () => void;
  openInvite: () => void;
  openCommand: () => void;
  openShortcuts: () => void;
  openShare: () => void;
  /** Owner resolve actions (Phase 2.5); each returns false when not sent. */
  releaseHold: (held: HeldEdit) => boolean;
  goFirst: (filePath: string, userId: string) => boolean;
  ping: (userId: string, text?: string) => boolean;
  dismissConflict: (conflict: ConflictAlert) => void;
  toast: (message: string) => void;
  showLane: (userId: string) => void;
  vote: (decisionId: string, vote: Vote) => void;
  retryJoin: () => void;
}

export interface V3Model {
  roomId: string;
  wsUrl: string;
  view: AppView;
  status: ConnStatus;
  /** Round trip of the latest /readyz, ms; null before the first. */
  rttMs: number | null;
  rttHistory: number[];
  isOwner: boolean;
  settings: RoomSettingsUpdate | null;
  requireApproval: boolean | null;
  accessNotice: string | null;
  lanes: LaneView[];
  onlineIds: string[];
  conflicts: ConflictAlert[];
  conflictHistory: ConflictAlert[];
  held: HeldEdit[];
  log: SessionLog;
  stats: RoomStats;
  prevention: PreventionSummary;
  feedback: FeedbackMap;
  votes: Record<string, Vote>;
  activity: ActivityItem[];
  totalDiffs: number;
  nowSeconds: number;
  /** "now" in coarse steps, for memoised derivations. */
  laneNow: number;
  tasks: Record<string, Task>;
  highlightFile: string | null;
  actions: V3Actions;
}
