/**
 * The relay's clock as seen from this browser. Relay timestamps (conflict
 * alerts, decisions, join requests) come from the relay's time.time(); a
 * viewer whose own clock runs two minutes fast would otherwise see every
 * conflict as "expired" on arrival, and a slow one keeps them too long.
 *
 * The offset is the largest (relay timestamp - local now) seen on a live
 * frame: a fresh frame was stamped a few ms before it arrived, so that is
 * the relay's clock minus latency; an older one can only pull it down, so
 * the maximum wins. Catch-up frames (stored, possibly hours old) are not fed
 * in. Before any live frame the local clock is used as is.
 */

let offsetSeconds: number | null = null;

/** Note a relay timestamp from a frame that was stamped just before it was sent. */
export function observeServerTimestamp(timestamp: unknown): void {
  if (typeof timestamp !== 'number' || !Number.isFinite(timestamp) || timestamp <= 0) return;
  const sample = timestamp - Date.now() / 1000;
  if (offsetSeconds == null || sample > offsetSeconds) offsetSeconds = sample;
}

/** Seconds since the epoch on the relay's clock (best estimate). */
export function serverNowSeconds(): number {
  return Date.now() / 1000 + (offsetSeconds ?? 0);
}

/** For tests. */
export function resetServerClock(): void {
  offsetSeconds = null;
}
