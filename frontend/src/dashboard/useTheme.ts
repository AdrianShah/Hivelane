import { useSyncExternalStore } from 'react';
import { applyTheme, readThemePref, storeThemePref, type ThemePref } from './theme';

/**
 * One theme preference for the whole page (several toggles stay in sync),
 * persisted in localStorage and re-applied when the OS theme flips while the
 * preference is "system".
 */

let current: ThemePref | null = null;
const listeners = new Set<() => void>();

function snapshot(): ThemePref {
  if (current == null) current = readThemePref();
  return current;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setThemePref(pref: ThemePref): void {
  current = pref;
  storeThemePref(pref);
  applyTheme(pref);
  for (const listener of listeners) listener();
}

if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (snapshot() === 'system') applyTheme('system');
    });
  } catch {
    // Old browsers without addEventListener on MediaQueryList: no live follow.
  }
}

export function useThemePref(): [ThemePref, (pref: ThemePref) => void] {
  const pref = useSyncExternalStore(subscribe, snapshot, () => 'system' as ThemePref);
  return [pref, setThemePref];
}
