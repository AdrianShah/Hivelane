/** Frame builders shared by the dashboard unit tests. */
import type {
  ActivityUpdate,
  ConflictAlert,
  EditDecision,
  IntentUpdate,
  PlanUpdate,
} from '../../src/types.ts';

export function activity(
  user: string,
  timestamp: number,
  files: string[],
  extra: Partial<ActivityUpdate> = {},
): ActivityUpdate {
  return {
    type: 'activity_update',
    user_id: user,
    timestamp,
    prompt: null,
    caption: null,
    changes: files.map((file_path) => ({ file_path, diff: `+edit ${file_path}`, symbols: [] })),
    agent: 'claude-code',
    ...extra,
  };
}

export function decision(
  user: string,
  timestamp: number,
  file: string,
  kind: 'warn' | 'block',
  extra: Partial<EditDecision> = {},
): EditDecision {
  return {
    type: 'edit_decision',
    user_id: user,
    file_path: file,
    decision: kind,
    others: ['ada'],
    reason: `ada is editing ${file}`,
    timestamp,
    decision_id: `d-${user}-${timestamp}`,
    scope: 'file',
    ...extra,
  };
}

export function conflict(
  users: string[],
  timestamp: number,
  file: string,
  verdict: ConflictAlert['verdict'] = 'overlapping',
): ConflictAlert {
  return {
    type: 'conflict_alert',
    users,
    file_path: file,
    verdict,
    explanation: `both edited ${file}`,
    timestamp,
  };
}

export function intent(user: string, timestamp: number, prompt: string): IntentUpdate {
  return { type: 'intent_update', user_id: user, timestamp, prompt, agent: 'cursor' };
}

export function plan(
  user: string,
  timestamp: number,
  files: string[],
  expiresIn = 600,
): PlanUpdate {
  return {
    type: 'plan_update',
    user_id: user,
    summary: 'refactor auth',
    files,
    symbols: [],
    timestamp,
    expires_at: timestamp + expiresIn,
  };
}
