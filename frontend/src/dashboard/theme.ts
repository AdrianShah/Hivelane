/**
 * Light / dark / system theme. The preference lives in localStorage (wrapped:
 * storage can be blocked); the resolved theme is written to <html
 * data-theme> and the Tailwind `dark` class, and every colour in index.css
 * and the Tailwind theme reads CSS custom properties keyed off data-theme.
 * index.html applies the same logic inline before first paint, so there is
 * no flash of the wrong theme.
 *
 * Start-up order: a stored choice, else the system setting
 * (prefers-color-scheme), else light. Light is also the answer when storage
 * or matchMedia is unavailable.
 */

export type ThemePref = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

/** Keep in sync with the inline script in index.html. */
export const THEME_STORAGE_KEY = 'hivelane:theme';

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

/** True only when the system asks for dark; no preference (or no matchMedia) is light. */
export function systemPrefersDark(
  matchMedia: ((query: string) => { matches: boolean }) | undefined = globalThis.matchMedia,
): boolean {
  try {
    return matchMedia?.call(globalThis, '(prefers-color-scheme: dark)').matches ?? false;
  } catch {
    return false;
  }
}

/** The theme a first visit (nothing stored) renders with. */
export function initialTheme(
  stored: string | null | undefined,
  matchMedia?: (query: string) => { matches: boolean },
): ResolvedTheme {
  return resolveTheme(parseThemePref(stored), systemPrefersDark(matchMedia));
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
