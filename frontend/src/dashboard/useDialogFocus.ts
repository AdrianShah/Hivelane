import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE =
  'a[href], button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Focus handling for a modal dialog: focus `initial` when it opens, keep Tab
 * inside `panel`, close on Escape (unless `escape` is false: a caller that
 * handles Esc itself), and give focus back on close.
 *
 * Depends on `open` only. `onClose` is read through a ref: callers pass a new
 * arrow function on every render, and re-running this effect each render
 * (the dashboard re-renders on every relay frame) yanked focus back to the
 * initial button while a keyboard user was on another control.
 */
export function useDialogFocus(
  open: boolean,
  onClose: () => void,
  panel: RefObject<HTMLElement | null>,
  initial: RefObject<HTMLElement | null>,
  escape = true,
): void {
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    (initial.current ?? panel.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && escape) {
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab' || !panel.current) return;
      const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !panel.current.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !panel.current.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      previous?.focus?.();
    };
    // Refs are stable; escape is fixed per call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
}
