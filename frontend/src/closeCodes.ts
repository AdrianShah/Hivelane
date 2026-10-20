/**
 * What the dashboard does when the relay closes its socket, by close code.
 * Plain TypeScript (no JSX) so node --test can check it.
 *
 * - 4403: not admitted (an owner refused, or the approval wait ran out): stop.
 * - 4404 / 4004: the room does not exist (expired or redeployed): stop.
 * - 1013 "try again later": the relay dropped a socket that read too slowly;
 *   4408: the join frame did not arrive in time. Both are transient:
 *   reconnect, with the usual backoff. So is every other code (1000, 1001,
 *   1006 network drop, 4429 rate limited, ...).
 */
export type CloseAction = 'refused' | 'room_missing' | 'reconnect';

export const CLOSE_NOT_ADMITTED = 4403;
export const CLOSE_ROOM_NOT_FOUND = [4404, 4004] as const;
export const CLOSE_SLOW_SOCKET = 1013;
export const CLOSE_JOIN_TIMEOUT = 4408;

export function closeAction(code: number): CloseAction {
  if (code === CLOSE_NOT_ADMITTED) return 'refused';
  if ((CLOSE_ROOM_NOT_FOUND as readonly number[]).includes(code)) return 'room_missing';
  return 'reconnect';
}
