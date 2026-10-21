import type { KeyboardEvent } from 'react';
import { THEME_PREFS, type ThemePref } from '../dashboard/theme';
import { useThemePref } from '../dashboard/useTheme';

const META: Record<ThemePref, { label: string; icon: string }> = {
  light: { label: 'Light', icon: 'light_mode' },
  dark: { label: 'Dark', icon: 'dark_mode' },
  system: { label: 'System', icon: 'contrast' },
};

/** Light / dark / system, as a radio group (arrow keys move the choice). */
export default function ThemeToggle({ showLabels = false }: { showLabels?: boolean }) {
  const [pref, setPref] = useThemePref();

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const index = THEME_PREFS.indexOf(pref);
    const next = THEME_PREFS[(index + step + THEME_PREFS.length) % THEME_PREFS.length];
    setPref(next);
    const group = event.currentTarget.parentElement;
    group?.querySelector<HTMLButtonElement>(`[data-pref="${next}"]`)?.focus();
  };

  return (
    <div className="theme-toggle" role="radiogroup" aria-label="Colour theme">
      {THEME_PREFS.map((option) => {
        const checked = option === pref;
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={checked}
            data-pref={option}
            tabIndex={checked ? 0 : -1}
            title={`${META[option].label} theme`}
            aria-label={`${META[option].label} theme`}
            onClick={() => setPref(option)}
            onKeyDown={onKeyDown}
            className={showLabels ? '!w-auto px-2.5 gap-1 !flex items-center' : ''}
          >
            <span className="material-symbols-outlined" aria-hidden="true">
              {META[option].icon}
            </span>
            {showLabels ? <span className="text-[11px] font-semibold">{META[option].label}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
