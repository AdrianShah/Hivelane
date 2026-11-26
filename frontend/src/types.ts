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
  /** Fingerprint of the file version line_ranges are numbered in. */
  version?: string;
}

/** Granularity at which the relay found a conflict. */
export type DecisionScope = 'symbol' | 'lines' | 'file' | 'intent' | 'plan' | 'branch' | 'task' | 'pending';

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

/** What an agent is currently trying to do, latest per user wins. */
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
  /** The requester's place in the file's wait queue (1 = next); 0 = not queued. */
  queue_position?: number;
  /** Actionable alternative the relay offered (free file nearby, tests, wait time). */
  suggestion?: string;
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

/** One file a member changed but has not pushed (pending_update). */
export interface PendingFile {
  path: string;
  /** Definitions the unpushed changes touch; empty = the whole file. */
  symbols?: string[];
  /** Unix seconds it has been pending since (oldest unpushed commit, or first seen changed); 0 = unknown. */
  since?: number;
}

/**
 * A member's unpushed work in one repo: uncommitted changes plus commits
 * ahead of its upstream. Replaces the previous one for (user_id, repo_id);
 * an empty files list clears it. online is false for the last report of a
 * member that has left. In an e2e room paths, symbols, branch and base are
 * h: hashes and the real values are in the blob.
 */
export interface PendingUpdate {
  type: 'pending_update';
  user_id: string;
  agent?: AgentKind;
  repo_id?: RepoId;
  branch?: string;
  base?: string;
  files: PendingFile[];
  truncated?: boolean;
  timestamp: number;
  online?: boolean;
  e2e?: string;
}

export type TaskStatus ='open' | 'claimed' | 'done' | 'cancelled';

/** A note on a room task (kind "done" is the result, "cancel" the reason). */
export interface TaskNote {
  user_id: string;
  text: string;
  timestamp: number;
  kind?: 'note' | 'done' | 'cancel';
  e2e?: string;
}

/**
 * A room task: created and claimed from the CLI, MCP or a dashboard, or one
 * step of an agent's own plan (source "agent_plan", kept in step by its
 * daemon; `lane` wrote it, `active` is the step it is on). While claimed its
 * files are reserved (others' edits to them are blocked, scope "task"); a
 * plan step reserves only while active. In an e2e room title, body and note
 * text live in the blobs and files are h: hashes.
 */
export interface Task {
  task_id: string;
  repo_id?: RepoId;
  title: string;
  body?: string;
  files: string[];
  depends_on: string[];
  status: TaskStatus;
  created_by: string;
  created_at: number;
  updated_at: number;
  claimer?: string | null;
  claimed_at?: number | null;
  lease_until?: number | null;
  notes: TaskNote[];
  e2e?: string;
  source?: TaskSource;
  lane?: string;
  active?: boolean;
  /** Who the creator thinks should take it (claim --next offers it to them first). */
  suggested_for?: string;
  /** Set when a dashboard created it: a public tag of that dashboard's task key. */
  dashboard_tag?: string;
}

export type TaskSource = 'manual' | 'agent_plan';

/** A task changed (dashboards only), also sent in the join catch-up. */
export interface TaskUpdate {
  type: 'task_update';
  task: Task;
}

/**
 * What a dashboard may send about tasks: create one (credited to `as_name`)
 * or cancel one it created. `dashboard_key` is this browser's own secret.
 */
export interface TaskRequest {
  type: 'task';
  request_id: string;
  op: 'create' | 'cancel';
  task_id?: string;
  title?: string;
  body?: string;
  files?: string[];
  depends_on?: string[];
  repo_id?: RepoId;
  suggested_for?: string;
  as_name?: string;
  dashboard_key: string;
  e2e?: string;
}

/** The relay's answer to one task request. */
export interface TaskReply {
  type: 'task_reply';
  request_id: string;
  ok: boolean;
  reason?: string;
  task?: Task | null;
  tasks?: Task[];
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
  /** The plan's request_id, echoed ("" when the plan sent none). */
  request_id?: string;
  /** Why the plan was not recorded; "" (or absent) when it was. */
  reason?: '' | 'rate_limited' | 'invalid';
}

/** Member → relay (daemon-only; declared for parity with server/models.py). */
export interface PlanMessage {
  type: 'plan';
  user_id: string;
  summary?: string;
  files?: string[];
  symbols?: string[];
  repo_id?: string;
  branch?: string;
  ttl_seconds?: number;
  e2e?: string;
  /** Echoed on the plan_conflicts reply (max 128 chars); "" when unset. */
  request_id?: string;
}

/** Member → relay: ask for a held file and join its wait queue (daemon-only). */
export interface FileRequest {
  type: 'file_request';
  file_path: string;
  repo_id?: string;
  e2e?: string;
  /** What the requester means to edit, so holders are judged at the same granularity. */
  branch?: string;
  symbols?: string[];
}

/** GET /readyz on the relay (fields the dashboard may show). */
export interface ReadyzResponse {
  status: string;
  /** The hivelane release the relay runs ("" when unknown). */
  version: string;
  /** The commit the deploy was built from ("" when the deploy does not say). */
  commit: string;
  [key: string]: unknown;
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
  /** Hours of events the relay keeps for history_request; 0 = none kept. */
  history_hours?: number;
  set_by: string | null;
  timestamp: number;
  /** When the room was created and when its code stops working (seconds); 0 from an older relay. */
  created_at?: number;
  expires_at?: number;
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

/**
 * Relay → a joiner just let in (approved, or by invite link): a per-member
 * token to present as `member_token` next time, so a reload is not asked
 * again. Sent once; the relay keeps only its hash.
 */
export interface MemberToken {
  type: 'member_token';
  member_id: string;
  token: string;
}

/** One person the room remembers (holds a member token). */
export interface RememberedMember {
  member_id: string;
  name: string;
  kind: 'member' | 'dashboard';
  issued_at: number;
  online: boolean;
}

/**
 * Relay → owner sockets only: the invite secret (to show the share link
 * again; null for rooms from before invite links) and who the room remembers.
 * In an owner's catch-up and after every rotation, admission or revocation.
 */
export interface RoomAccess {
  type: 'room_access';
  invite_required: boolean;
  invite_secret?: string | null;
  members: RememberedMember[];
}

/** Owner → relay: replace the invite link; old links stop admitting. */
export interface InviteRotate {
  type: 'invite_rotate';
}

/** Owner → relay: forget a remembered member and disconnect it. */
export interface MemberRevoke {
  type: 'member_revoke';
  member_id: string;
}

/** Spectator → relay: replay the room's kept events newer than `since` (0 = all). */
/** Owner -> relay: let a held (blocked) edit go ahead (server/models.py HoldRelease). */
export interface HoldRelease {
  type: 'hold_release';
  decision_id: string;
}

/** Owner -> relay: who edits a contested file first (QueueOrder). */
export interface QueueOrder {
  type: 'queue_order';
  file_path: string;
  repo_id?: RepoId;
  first_user_id: string;
}

/** Owner -> relay: ping one member's agent (Nudge). */
export interface Nudge {
  type: 'nudge';
  to_user_id: string;
  text: string;
}

/** Relay -> dashboards: an owner pinged user_id (delivered: its daemon got it). */
export interface Nudged {
  type: 'nudged';
  user_id: string;
  by: string;
  text: string;
  delivered: boolean;
  timestamp: number;
}

/** Relay -> dashboards: an owner released decision_id's hold. */
export interface HoldReleased {
  type: 'hold_released';
  decision_id: string;
  user_id: string;
  file_path: string;
  repo_id?: RepoId;
  by: string;
  timestamp: number;
  e2e?: string;
}

/** Relay -> dashboards: an owner let first_user_id go first; waiting is the rest of the line. */
export interface QueueReordered {
  type: 'queue_reordered';
  file_path: string;
  repo_id?: RepoId;
  first_user_id: string;
  waiting: string[];
  timestamp: number;
  e2e?: string;
}

export interface HistoryRequest {
  type: 'history_request';
  since?: number;
}

/** Relay → spectator answer to history_request, in timestamp order; `done` marks the last chunk. */
export interface HistoryChunk {
  type: 'history_chunk';
  events: FeedEvent[];
  done: boolean;
}

/** Debounced room broadcast, one frame every ~500ms with queued events. */
export interface HivelaneFeed {
  type: 'hivelane_feed';
  events: FeedEvent[];
}

/** Events that appear inside a Hivelane feed batch (or as catch-up sends). */
export type FeedEvent =
  | ActivityUpdate
  | ConflictAlert
  | EditDecision
  | IntentUpdate
  | PlanUpdate
  | PendingUpdate
  | DecisionFeedbackUpdate
  | RoomSettingsUpdate
  | RoomPolicyUpdate
  | UserJoined
  | UserLeft
  | TaskUpdate
  | Nudged
  | HoldReleased
  | QueueReordered;

/** Team-wide conflict windows from a repo's committed .hivelane.toml. */
export interface RoomSettings {
  block_window_seconds: number;
  warn_window_seconds: number;
  e2e?: boolean;
  history_hours?: number;
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
  /** True when discovered via GET /rooms/by-repo; repo-bound rooms enforce approval regardless. */
  via_repo_lookup?: boolean;
  /** The invite secret from a share link (`#invite=`): admitted without approval. */
  invite_secret?: string | null;
  /** The token from an earlier member_token: skips approval until revoked. */
  member_token?: string | null;
}

export type ServerMessage =
  | HivelaneFeed
  | HistoryChunk
  | ActivityUpdate
  | ConflictAlert
  | EditDecision
  | IntentUpdate
  | PlanUpdate
  | PendingUpdate
  | PlanConflicts
  | TaskUpdate
  | TaskReply
  | DecisionFeedbackUpdate
  | RoomSettingsUpdate
  | RoomPolicyUpdate
  | JoinPending
  | JoinRequest
  | JoinResolved
  | MemberToken
  | RoomAccess
  | UserJoined
  | UserLeft
  | NameRejected
  | RateLimited
  | RoomRejected
  | Nudged
  | HoldReleased
  | QueueReordered;

export interface LaneState {
  caption: string | null;
  changes: FileChange[];
  prompt: string | null;
  intent: string | null;
  decisions: EditDecision[];
  agent: AgentKind;
  timestamp: number | null;
}
