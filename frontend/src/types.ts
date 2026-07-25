/** Mirrors server/models.py message schema (shared contract). */

export interface FileChange {
  file_path: string;
  diff: string;
}

/** Watcher → server activity payload (echoed into ActivityUpdate). */
export interface Activity {
  type: 'activity';
  user_id: string;
  changes: FileChange[];
  prompt: string | null;
}

/** Server → clients broadcast after an activity message. */
export interface ActivityUpdate {
  type: 'activity_update';
  user_id: string;
  caption: string | null;
  changes: FileChange[];
  prompt: string | null;
}

export interface ConflictItem {
  file_path: string;
  users: string[];
}

export interface ConflictAlert {
  type: 'conflict_alert';
  conflicts: ConflictItem[];
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
