/**
 * Mirrors server/models.py message schema (shared contract; the language-neutral
 * copy is protocol/schema.json). server/tests/test_protocol_contract.py fails if
 * a relay frame the dashboard receives, or one of its fields, is missing here.
 */

export type Verdict = 'overlapping' | 'related' | 'unrelated';

export type AgentKind =
  | 'cursor'
  | 'claude-code'
  | 'codex'
  | 'antigravity'
  | 'gemini-cli'
  | 'devin'
  | 'grok'
  | 'aider'
  | 'human'
  | 'unknown';

export interface FileChange {
  file_path: string;
  diff: string;
  /** Touched function/class names; "<imports>" marks an import-block edit. */
  symbols?: string[];
  /** 1-based inclusive [start, end] ranges in the new file. */
  line_ranges?: [number, number][];
}

/** Granularity at which the relay found a conflict. */
export type DecisionScope = 'symbol' | 'lines' | 'file' | 'intent' | 'plan' | 'branch';

/**
 * Opaque repository identity from the daemon (hash of the normalized origin
 * URL, else the root commit). Empty when unknown, or sent by an older client.
 * The relay only compares touches within one repo_id.
 */
export type RepoId = string;

/** Daemon → server activity payload. */
export interface Activity {
  type: 'activity';
  user_id: string;
  timestamp: number;
  prompt: string | null;
  changes: FileChange[];
  agent?: AgentKind;
  repo_id?: RepoId;
  e2e?: string;
}

/** Server → clients broadcast after an activity message. */
export interface ActivityUpdate {
  type: 'activity_update';
  user_id: string;
  timestamp: number;
  prompt: string | null;
  changes: FileChange[];
  caption: string | null;
  agent?: AgentKind;
  repo_id?: RepoId;
  branch?: string;
  /** Opaque client ciphertext (e2e rooms only); the relay never reads it. */
  e2e?: string;
}

/** What an agent is currently trying to do — latest per user wins. */
export interface IntentUpdate {
  type: 'intent_update';
  user_id: string;
  prompt: string;
  timestamp: number;
  agent?: AgentKind;
  repo_id?: RepoId;
  e2e?: string;
}

/** A pre-edit check that came back non-allow; allow decisions aren't broadcast. */
export interface EditDecision {
  type: 'edit_decision';
  user_id: string;
  file_path: string;
  decision: 'warn' | 'block';
  others: string[];
  reason: string;
  timestamp: number;
  agent?: AgentKind;
  repo_id?: RepoId;
  scope?: DecisionScope;
  /** Non-empty for warn/block; key for decision_feedback votes. */
  decision_id?: string;
  e2e?: string;
}

/** A member's declared plan (files/symbols it intends to change). */
export interface PlanUpdate {
  type: 'plan_update';
  user_id: string;
  agent?: AgentKind;
  summary: string;
  files: string[];
  symbols: string[];
  repo_id?: RepoId;
  branch?: string;
  timestamp: number;
  expires_at: number;
  e2e?: string;
}

export interface PlanConflict {
  file_path: string;
  user_id: string;
  agent?: AgentKind;
  kind: 'edited' | 'planned';
  summary: string;
  seconds_ago: number;
  e2e?: string;
}

/** Point-to-point reply to a member's plan message. */
export interface PlanConflicts {
  type: 'plan_conflicts';
  conflicts: PlanConflict[];
}

/** Spectator → relay: was this warn/block useful or noise? */
export interface DecisionFeedback {
  type: 'decision_feedback';
  decision_id: string;
  useful: boolean;
}

/** Running vote tally for one decision, broadcast after each vote. */
export interface DecisionFeedbackUpdate {
  type: 'decision_feedback_update';
  decision_id: string;
  useful_count: number;
  noise_count: number;
}

/** One conflict alert per overlapping/related file pair (server → clients). */
export interface ConflictAlert {
  type: 'conflict_alert';
  users: string[];
  file_path: string;
  verdict: Verdict;
  explanation: string;
  timestamp: number;
}

export interface UserJoined {
  type: 'user_joined';
  user_id: string;
  agent?: AgentKind;
}

export interface UserLeft {
  type: 'user_left';
  user_id: string;
}

export interface NameRejected {
  type: 'name_rejected';
  reason: string;
}

export interface RoomRejected {
  type: 'room_rejected';
  reason: string;
}

/** Sent before close 4429 when the relay throttles connection attempts. */
export interface RateLimited {
  type: 'rate_limited';
  reason: string;
  retry_after: number;
}

/**
 * The conflict windows the relay enforces for this room. Sent first in a
 * spectator's catch-up, and broadcast in the feed whenever a member's join
 * changes them. set_by is null for relay defaults or settings chosen when the
 * room was created.
 */
export interface RoomSettingsUpdate {
  type: 'room_settings';
  block_window_seconds: number;
  warn_window_seconds: number;
  /** The team committed `e2e = true`: payloads arrive encrypted in `e2e`. */
  e2e?: boolean;
  set_by: string | null;
  timestamp: number;
}

/**
 * Sent to a joiner (member or spectator) held for owner approval. The socket
 * stays open; the next frame is normal traffic (approved) or room_rejected.
 */
export interface JoinPending {
  type: 'join_pending';
  request_id: string;
  reason: string;
}

/** Sent to owner sockets: someone is waiting to join. Answer with JoinDecision. */
export interface JoinRequest {
  type: 'join_request';
  request_id: string;
  /** "dashboard viewer" for a spectator. */
  name: string;
  agent?: AgentKind;
  timestamp: number;
}

/**
 * How a pending join request ended. Sent to owner sockets, and (outcome
 * "approved") to the admitted joiner itself, which must not send anything
 * before it arrives.
 */
export interface JoinResolved {
  type: 'join_resolved';
  request_id: string;
  outcome: 'approved' | 'declined' | 'timed_out' | 'withdrawn';
}

/** Owner → relay: admit or decline a pending joiner. */
export interface JoinDecision {
  type: 'join_decision';
  request_id: string;
  approve: boolean;
}

/** Owner → relay: turn join approval on or off. */
export interface RoomPolicy {
  type: 'room_policy';
  require_approval: boolean;
}

/** The room's join policy: in every spectator's catch-up and broadcast on change. */
export interface RoomPolicyUpdate {
  type: 'room_policy_update';
  require_approval: boolean;
}

/** Debounced room broadcast — one frame every ~500ms with queued events. */
export interface SynergoFeed {
  type: 'synergo_feed';
  events: FeedEvent[];
}

/** Events that appear inside a Synergo feed batch (or as catch-up sends). */
export type FeedEvent =
  | ActivityUpdate
  | ConflictAlert
  | EditDecision
  | IntentUpdate
  | PlanUpdate
  | DecisionFeedbackUpdate
  | RoomSettingsUpdate
  | RoomPolicyUpdate
  | UserJoined
  | UserLeft;

/** Team-wide conflict windows from a repo's committed .synergo.toml. */
export interface RoomSettings {
  block_window_seconds: number;
  warn_window_seconds: number;
  e2e?: boolean;
}

/**
 * First message after connect.
 * Spectator: name is null (frontend).
 * Named agent: name is a raw display string (daemon).
 */
export interface JoinMessage {
  type: 'join';
  name: string | null;
  role?: 'spectator';
  agent?: AgentKind;
  protocol_version?: number;
  repo_id?: RepoId;
  settings?: RoomSettings | null;
  /** Secret from POST /rooms; marks this socket as a room owner. */
  owner_token?: string | null;
}

export type ServerMessage =
  | SynergoFeed
  | ActivityUpdate
  | ConflictAlert
  | EditDecision
  | IntentUpdate
  | PlanUpdate
  | PlanConflicts
  | DecisionFeedbackUpdate
  | RoomSettingsUpdate
  | RoomPolicyUpdate
  | JoinPending
  | JoinRequest
  | JoinResolved
  | UserJoined
  | UserLeft
  | NameRejected
  | RateLimited
  | RoomRejected;

export interface LaneState {
  caption: string | null;
  changes: FileChange[];
  prompt: string | null;
  intent: string | null;
  decisions: EditDecision[];
  agent: AgentKind;
  timestamp: number | null;
}
