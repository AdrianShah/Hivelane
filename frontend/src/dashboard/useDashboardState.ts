import { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import type { FeedbackMap } from '../detection';
import { summarizePrevention } from './prevented';
import { createSessionLog, reduceSessionLog, sessionPeople } from './sessionLog';
import { createShortcutMatcher, isTypingTarget } from './shortcuts';
import { roomStats } from './stats';
import { useUrlState } from './useUrlState';

/**
 * The Phase 5 dashboard state App.tsx wires in with a handful of lines: the
 * session log fed from relay frames, URL-backed filters and view, derived
 * prevention stats, the share/help overlays and keyboard shortcuts.
 */

/** Views the URL can open, and the default (no ?view=). */
export const DEFAULT_URL_VIEW = 'agents';
export const URL_VIEWS = new Set([
  'dashboard',
  'agents',
  'timeline',
  'heatmap',
  'invite',
  'conflicts',
  'history',
  'analytics',
  'settings',
  'docs',
  // Views the notifications/history work adds (ReplayView, NotificationsSettings).
  'replay',
  'notifications',
]);

/**
 * ?view= as a known view: no param means the default; an unknown one
 * (a view this build does not list) yields null, i.e. "leave it alone".
 */
export function viewFromParam(param: string | null): string | null {
  if (param == null) return DEFAULT_URL_VIEW;
  return URL_VIEWS.has(param) ? param : null;
}

type ShortcutView = 'agents' | 'timeline' | 'heatmap';

interface Options {
  /** Switch views (App's navigateTo). */
  onNavigate: (view: ShortcutView) => void;
  feedback: FeedbackMap;
  nowSeconds: number;
}

export function useDashboardState({ onNavigate, feedback, nowSeconds }: Options) {
  const [log, recordEvent] = useReducer(reduceSessionLog, undefined, createSessionLog);
  const { filters, setFilters, viewParam, setViewParam, setViewAndFilters } = useUrlState();
  const searchRef = useRef<HTMLInputElement>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);

  // Derived numbers move in 5 s steps: fine for "pending" windows and cheap.
  const clock = Math.floor(nowSeconds / 5) * 5;
  const prevention = useMemo(() => summarizePrevention(log, clock), [log, clock]);
  const stats = useMemo(
    () => roomStats(log, prevention, feedback, clock),
    [log, prevention, feedback, clock],
  );
  const people = useMemo(() => sessionPeople(log), [log]);
  const agentKinds = useMemo(
    () => [...new Set(Object.values(log.agents))].filter((k) => k !== 'unknown').sort(),
    [log],
  );

  /** Mirror a view change into ?view= (a history entry) when it differs. */
  const syncViewParam = (view: string) => {
    const wanted = view === DEFAULT_URL_VIEW ? null : view;
    if (wanted !== viewParam) setViewParam(wanted);
  };

  /**
   * Heat map → timeline for one file, in a single URL change; App follows
   * ?view= (its Back/Forward effect) to switch the view.
   */
  const showFileInTimeline = (path: string) => {
    setViewAndFilters('timeline', (prev) => ({ ...prev, file: path }));
  };

  // Shortcuts: one listener for the page's lifetime; the latest callbacks
  // are read through a ref so the g-then-x sequence survives re-renders.
  const latest = useRef({ onNavigate, helpOpen, shareOpen });
  useEffect(() => {
    latest.current = { onNavigate, helpOpen, shareOpen };
  });
  useEffect(() => {
    const match = createShortcutMatcher();
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;
      const action = match(event.key, Date.now());
      if (!action) return;
      const { onNavigate: go, helpOpen: help, shareOpen: share } = latest.current;
      const modalOpen = document.querySelector('[aria-modal="true"]') != null;
      if (action === 'close') {
        if (help) setHelpOpen(false);
        if (share) setShareOpen(false);
        return;
      }
      if (action === 'toggle-help') {
        if (modalOpen && !help) return;
        event.preventDefault();
        setHelpOpen((open) => !open);
        return;
      }
      if (modalOpen) return;
      event.preventDefault();
      if (action === 'focus-search') {
        if (searchRef.current) {
          searchRef.current.focus();
        } else {
          go('agents');
          window.setTimeout(() => searchRef.current?.focus(), 60);
        }
        return;
      }
      go(action === 'go-timeline' ? 'timeline' : action === 'go-heatmap' ? 'heatmap' : 'agents');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return {
    log,
    recordEvent,
    filters,
    setFilters,
    viewParam,
    syncViewParam,
    showFileInTimeline,
    searchRef,
    helpOpen,
    setHelpOpen,
    shareOpen,
    setShareOpen,
    prevention,
    stats,
    people,
    agentKinds,
  };
}
