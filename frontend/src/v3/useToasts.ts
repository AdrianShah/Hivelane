import { useCallback, useEffect, useRef, useState } from 'react';

export interface Toast {
  id: number;
  message: string;
}

const TOAST_MS = 2400;
const MAX_TOASTS = 3;

/** Confirmation toasts: a short stack, newest last, each gone after a moment. */
export function useToasts(): [Toast[], (message: string) => void, (id: number) => void] {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const timers = useRef(new Map<number, number>());
  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    window.clearTimeout(timers.current.get(id));
    timers.current.delete(id);
  }, []);
  const show = useCallback(
    (message: string) => {
      const id = next.current++;
      setToasts((prev) => [...prev.filter((t) => t.message !== message), { id, message }].slice(-MAX_TOASTS));
      timers.current.set(id, window.setTimeout(() => dismiss(id), TOAST_MS));
    },
    [dismiss],
  );
  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((t) => window.clearTimeout(t));
  }, []);
  return [toasts, show, dismiss];
}

