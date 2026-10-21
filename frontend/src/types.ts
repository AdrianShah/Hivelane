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
  | 'unknown';

export interface FileChange {
  file_path: string;
  diff: string;
}

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
}

/** What an agent is currently trying to do — latest per user wins. */
export interface IntentUpdate {
  type: 'intent_update';
  user_id: string;
  prompt: string;
  timestamp: number;
  agent?: AgentKind;
  repo_id?: RepoId;
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
  | UserJoined
  | UserLeft;

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
}

export type ServerMessage =
  | SynergoFeed
  | ActivityUpdate
  | ConflictAlert
  | EditDecision
  | IntentUpdate
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
