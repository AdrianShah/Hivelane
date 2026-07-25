/** Mirrors server/models.py message schema (shared contract). */

export type Verdict = 'overlapping' | 'related' | 'unrelated';

export type AgentKind = 'cursor' | 'claude-code' | 'codex' | 'unknown';

export interface FileChange {
  file_path: string;
  diff: string;
}

/** Watcher → server activity payload. */
export interface Activity {
  type: 'activity';
  user_id: string;
  timestamp: number;
  prompt: string | null;
  changes: FileChange[];
  agent?: AgentKind;
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

/**
 * First message after connect.
 * Spectator: name is null (frontend).
 * Named agent: name is a raw display string (watcher).
 */
export interface JoinMessage {
  type: 'join';
  name: string | null;
  role?: 'spectator';
  agent?: AgentKind;
}

export type ServerMessage =
  | ActivityUpdate
  | ConflictAlert
  | UserJoined
  | UserLeft
  | NameRejected
  | RoomRejected;

export interface LaneState {
  caption: string | null;
  changes: FileChange[];
  prompt: string | null;
  agent: AgentKind;
  timestamp: number | null;
}

/* --- Local companion service (http://localhost:4500) ---------------------- */

/** Tools the companion can detect; a superset of the relay's AgentKind. */
export type ToolId = 'cursor' | 'claude-code' | 'codex' | 'antigravity' | 'unknown';

/** One row of GET /detect. `running` is null when psutil is unavailable. */
export interface DetectedTool {
  id: ToolId;
  label: string;
  installed: boolean;
  install_path: string | null;
  running: boolean | null;
}

export interface DetectResponse {
  platform: string;
  tools: DetectedTool[];
}

export type WatcherStatus = 'running' | 'stopped' | 'errored';

/** One entry of GET /watchers. */
export interface WatcherInfo {
  watcher_id: string;
  path: string;
  agent: ToolId;
  room_id: string;
  name: string;
  server_url: string;
  status: WatcherStatus;
  pid: number | null;
  exit_code: number | null;
  started_at: number;
  logs: string[];
}

/** POST /watchers body. */
export interface CreateWatcherRequest {
  path: string;
  room_id: string;
  name: string;
  agent: ToolId;
  server_url: string;
}

export interface CreateWatcherResponse {
  watcher_id: string;
}
