/**
 * Light / dark / system theme. The preference lives in localStorage (wrapped:
 * storage can be blocked); the resolved theme is written to <html
 * data-theme> and the Tailwind `dark` class, and every colour in index.css
 * and the Tailwind config reads CSS custom properties keyed off data-theme.
 * index.html applies the same logic inline before first paint, so there is
 * no flash of the wrong theme.
 */

export type ThemePref = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

/** Keep in sync with the inline script in index.html. */
export const THEME_STORAGE_KEY = 'synergo:theme';

export const THEME_PREFS: ThemePref[] = ['light', 'dark', 'system'];

export function parseThemePref(raw: string | null | undefined): ThemePref {
  return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system';
}

export function resolveTheme(pref: ThemePref, systemPrefersDark: boolean): ResolvedTheme {
  if (pref === 'system') return systemPrefersDark ? 'dark' : 'light';
  return pref;
}

export function readThemePref(): ThemePref {
  try {
    return parseThemePref(globalThis.localStorage?.getItem(THEME_STORAGE_KEY));
  } catch {
    return 'system';
  }
}

export function storeThemePref(pref: ThemePref): void {
  try {
    globalThis.localStorage?.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    // Blocked storage: the choice lasts for this page only.
  }
}

export function systemPrefersDark(): boolean {
  try {
    return globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches ?? true;
  } catch {
    return true;
  }
}

export function applyTheme(pref: ThemePref): ResolvedTheme {
  const resolved = resolveTheme(pref, systemPrefersDark());
  if (typeof document !== 'undefined') {
    const root = document.documentElement;
    root.dataset.theme = resolved;
    root.dataset.themePref = pref;
    root.classList.toggle('dark', resolved === 'dark');
    root.style.colorScheme = resolved;
  }
  return resolved;
}
