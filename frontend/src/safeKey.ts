/**
 * Lanes, decisions and feedback are kept in plain objects keyed by ids the
 * relay forwards from clients (user_id, decision_id). A key such as
 * "__proto__" assigned into one of those objects would reach Object.prototype,
 * so frames carrying one are dropped before they touch any state.
 */
const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);

export function isSafeKey(key: unknown): boolean {
  return typeof key !== 'string' || !UNSAFE_KEYS.has(key);
}

/** True when every id a frame could be filed under is a safe object key. */
export function hasSafeKeys(frame: { user_id?: unknown; decision_id?: unknown }): boolean {
  return isSafeKey(frame.user_id) && isSafeKey(frame.decision_id);
}
