/**
 * Room expiry and relay round-trip labels for the sidebar footer, plus the
 * rooms this browser owns (for the header's room switcher). Pure where it
 * can be; the storage scan takes the storage as an argument.
 */

/** "Expires in 6 days" / "in 5 h" / "in 12 min" / "Expired"; null when unknown. */
export function expiryLabel(expiresAt: number | null | undefined, nowSeconds: number): string | null {
  if (!expiresAt || !Number.isFinite(expiresAt) || expiresAt <= 0) return null;
  const left = expiresAt - nowSeconds;
  if (left <= 0) return 'Expired';
  const minutes = Math.floor(left / 60);
  if (minutes < 60) return `Expires in ${Math.max(1, minutes)} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `Expires in ${hours} h`;
  // Rounded: a room created a minute ago reads 7 days, not 6.
  return `Expires in ${Math.round(hours / 24)} days`;
}

/** True in the last day of a room's life (the footer turns amber). */
export function expiresSoon(expiresAt: number | null | undefined, nowSeconds: number): boolean {
  return !!expiresAt && expiresAt > 0 && expiresAt - nowSeconds < 24 * 60 * 60;
}

/** "42 ms" (rounded), or null without a sample. */
export function rttLabel(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/**
 * What the relay says about a room this browser owns (GET /rooms/{code}).
 * 'gone' only when the relay definitively says so; anything else we could
 * not read (offline, rate-limited, a relay without the route) is 'unknown',
 * and the room stays listed with its token kept.
 */
export type RoomLiveness = { state: 'alive'; expiresAt: number } | { state: 'gone' } | { state: 'unknown' };

export function livenessFromResponse(status: number, body: unknown): RoomLiveness {
  const record = body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  if (status === 200 && record.alive === true) {
    const expiresAt = typeof record.expires_at === 'number' && Number.isFinite(record.expires_at) ? record.expires_at : 0;
    return { state: 'alive', expiresAt };
  }
  // A bare 404 also comes from a relay that predates the route: only the
  // marked one means the room is gone (server/main.py room_info).
  if (status === 404 && record.alive === false) return { state: 'gone' };
  return { state: 'unknown' };
}

/** The "Your rooms" chip's second line: "Live · expires in 6 days", or null when not known alive. */
export function livenessLabel(liveness: RoomLiveness | undefined, nowSeconds: number): string | null {
  if (!liveness || liveness.state !== 'alive') return null;
  const expiry = expiryLabel(liveness.expiresAt, nowSeconds);
  return expiry ? `Live · ${expiry.charAt(0).toLowerCase()}${expiry.slice(1)}` : 'Live';
}

/**
 * Room codes this browser holds an owner token for on `relayBase`, newest
 * key last (storage order). Keys are `<prefix><relay>|<CODE>` (roomAccess.ts).
 */
export function ownedRoomCodes(
  storage: Pick<Storage, 'length' | 'key'> | null | undefined,
  prefix: string,
  relayBase: string,
): string[] {
  if (!storage) return [];
  const want = `${prefix}${relayBase}|`;
  const codes: string[] = [];
  try {
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key && key.startsWith(want)) {
        const code = key.slice(want.length);
        if (/^[A-Z0-9]{6,8}$/.test(code) && !codes.includes(code)) codes.push(code);
      }
    }
  } catch {
    return codes;
  }
  return codes.sort();
}
