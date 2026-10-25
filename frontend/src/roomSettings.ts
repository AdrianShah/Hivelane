import type { RoomSettings, RoomSettingsUpdate } from './types';

/**
 * What the relay enforces in a room nobody configured (server/room.py
 * DEFAULT_SETTINGS). Shown until the relay's room_settings frame arrives, and
 * accurate even against a relay too old to send one.
 */
export const DEFAULT_ROOM_SETTINGS: RoomSettings = {
  block_window_seconds: 20,
  warn_window_seconds: 60,
};

function formatSeconds(seconds: number): string {
  if (seconds >= 120 && seconds % 60 === 0) return `${seconds / 60} min`;
  return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)} s`;
}

/** "Blocks within 20 s, warns within 60 s", a zero window turns that tier off. */
export function describeWindows(settings: RoomSettings): string {
  const block = settings.block_window_seconds;
  const warn = settings.warn_window_seconds;
  const blocks = block > 0 ? `Blocks within ${formatSeconds(block)}` : 'Never blocks';
  const warns =
    warn > 0 ? `warns within ${formatSeconds(warn)}` : 'warns only on stated intent';
  return `${blocks}, ${warns}`;
}

/** Where the windows in force came from, for a caption under them. */
export function describeSettingsSource(settings: RoomSettingsUpdate | null): string {
  if (settings?.set_by) return `Set by ${settings.set_by}'s .hivelane.toml`;
  const isDefault =
    settings == null ||
    (settings.block_window_seconds === DEFAULT_ROOM_SETTINGS.block_window_seconds &&
      settings.warn_window_seconds === DEFAULT_ROOM_SETTINGS.warn_window_seconds);
  return isDefault ? 'Relay defaults' : 'Set when the room was created';
}
