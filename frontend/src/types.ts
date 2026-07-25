/** Mirrors server/models.py message schema (shared contract). */

export type Verdict = 'overlapping' | 'related' | 'unrelated';

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
}

/** Server → clients broadcast after an activity message. */
export interface ActivityUpdate {
  type: 'activity_update';
  user_id: string;
  timestamp: number;
  prompt: string | null;
  changes: FileChange[];
  caption: string | null;
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
}

export interface UserLeft {
  type: 'user_left';
  user_id: string;
}

export interface NameRejected {
  type: 'name_rejected';
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
}

export type ServerMessage =
  | ActivityUpdate
  | ConflictAlert
  | UserJoined
  | UserLeft
  | NameRejected;

export interface LaneState {
  caption: string | null;
  changes: FileChange[];
  prompt: string | null;
}
