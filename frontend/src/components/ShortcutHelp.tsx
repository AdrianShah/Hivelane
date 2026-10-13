import { useEffect, useId, useRef } from 'react';
import { SHORTCUTS } from '../dashboard/shortcuts';

/** The "?" overlay listing keyboard shortcuts. Esc (handled by App) closes it. */
export default function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => previous?.focus?.();
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[105] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <button type="button" className="absolute inset-0 overlay-backdrop" aria-label="Close shortcuts" tabIndex={-1} onClick={onClose} />
      <div className="relative w-full max-w-md dialog-panel rounded-2xl p-5 animate-fade-up">
        <div className="flex items-center justify-between gap-3 mb-3">
          <h2 id={titleId} className="text-base font-bold tracking-tight m-0">
            Keyboard shortcuts
          </h2>
          <button ref={closeRef} type="button" className="icon-btn p-1.5" onClick={onClose} aria-label="Close">
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
              close
            </span>
          </button>
        </div>
        <dl className="m-0 divide-y divide-outline-soft/60">
          {SHORTCUTS.map((s) => (
            <div key={s.keys} className="flex items-center justify-between gap-4 py-2">
              <dt className="text-[13px] text-on-muted">{s.description}</dt>
              <dd className="m-0 flex items-center gap-1 shrink-0">
                {s.keys.split(' ').map((k, i) => (
                  <span key={i} className="flex items-center gap-1">
                    {i > 0 ? <span className="text-[11px] text-on-dim">then</span> : null}
                    <kbd>{k === 'Escape' ? 'Esc' : k}</kbd>
                  </span>
                ))}
              </dd>
            </div>
          ))}
        </dl>
        <p className="m-0 mt-3 text-[11.5px] text-on-dim">Shortcuts are ignored while you type in a field.</p>
      </div>
    </div>
  );
}
