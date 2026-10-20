/**
 * Dashboard filters, kept in the URL query string (never localStorage) so a
 * filtered view is a link someone can send:
 *
 *   /r/K7M2PQ2X?view=timeline&person=ada&person=sam&type=block&q=login
 *
 * One filter set applies to lanes, the collision timeline and the heat map;
 * each view applies the facets that make sense for it (see the match*
 * functions). Parsing is forgiving — unknown values are dropped, text is
 * trimmed and capped — because links are hand-edited and old.
 */
import { decisionScope } from '../detection.ts';
import type { AgentKind, DecisionScope } from '../types';
import { normPath } from './paths.ts';
import type { TimelineEntry, TimelineKind, Touch } from './sessionLog.ts';

export interface DashboardFilters {
  /** User ids; empty = everyone. */
  people: string[];
  /** Case-insensitive path substring; '' = any file. */
  file: string;
  /** Agent kinds; empty = any. */
  agents: string[];
  /** Timeline entry kinds; empty = all. */
  types: TimelineKind[];
  /** Decision scopes; empty = all. */
  scopes: DecisionScope[];
  /** Free text over intents, captions and decision reasons. */
  q: string;
}

export const EMPTY_FILTERS: DashboardFilters = {
  people: [],
  file: '',
  agents: [],
  types: [],
  scopes: [],
  q: '',
};

/** Query-string key per filter facet. */
export const FILTER_PARAMS = {
  people: 'person',
  file: 'file',
  agents: 'agent',
  types: 'type',
  scopes: 'scope',
  q: 'q',
} as const satisfies Record<keyof DashboardFilters, string>;

export const TIMELINE_KINDS: TimelineKind[] = ['block', 'warn', 'conflict'];
export const FILTER_SCOPES: DecisionScope[] = [
  'symbol',
  'lines',
  'file',
  'plan',
  'branch',
  'intent',
];

const MAX_TEXT = 200;
const MAX_VALUES = 50;
const TOKEN = /^[a-z0-9_.:-]{1,64}$/i;

function listParam(params: URLSearchParams, key: string): string[] {
  const out: string[] = [];
  for (const raw of params.getAll(key)) {
    for (const part of raw.split(',')) {
      const value = part.trim();
      if (value && TOKEN.test(value) && !out.includes(value)) out.push(value);
    }
  }
  return out.slice(0, MAX_VALUES);
}

function textParam(params: URLSearchParams, key: string): string {
  return (params.get(key) ?? '').trim().slice(0, MAX_TEXT);
}

/** Free-text filters as the URL keeps them (trimmed): equal means "no change". */
export function sameFilterText(a: string, b: string): boolean {
  return a.trim() === b.trim();
}

export function parseFilters(params: URLSearchParams): DashboardFilters {
  return {
    people: listParam(params, FILTER_PARAMS.people),
    file: textParam(params, FILTER_PARAMS.file),
    agents: listParam(params, FILTER_PARAMS.agents).map((a) => a.toLowerCase()),
    types: listParam(params, FILTER_PARAMS.types)
      .map((t) => t.toLowerCase())
      .filter((t): t is TimelineKind => (TIMELINE_KINDS as string[]).includes(t)),
    scopes: listParam(params, FILTER_PARAMS.scopes)
      .map((s) => s.toLowerCase())
      .filter((s): s is DecisionScope => (FILTER_SCOPES as string[]).includes(s)),
    q: textParam(params, FILTER_PARAMS.q),
  };
}

/** A copy of `base` with the filter keys replaced; other keys (view, …) kept. */
export function writeFilters(
  base: URLSearchParams,
  filters: DashboardFilters,
): URLSearchParams {
  const next = new URLSearchParams(base);
  for (const key of Object.values(FILTER_PARAMS)) next.delete(key);
  for (const person of filters.people) next.append(FILTER_PARAMS.people, person);
  if (filters.file.trim()) next.set(FILTER_PARAMS.file, filters.file.trim());
  for (const agent of filters.agents) next.append(FILTER_PARAMS.agents, agent);
  for (const type of filters.types) next.append(FILTER_PARAMS.types, type);
  for (const scope of filters.scopes) next.append(FILTER_PARAMS.scopes, scope);
  if (filters.q.trim()) next.set(FILTER_PARAMS.q, filters.q.trim());
  return next;
}

export function activeFilterCount(f: DashboardFilters): number {
  return (
    f.people.length +
    f.agents.length +
    f.types.length +
    f.scopes.length +
    (f.file.trim() ? 1 : 0) +
    (f.q.trim() ? 1 : 0)
  );
}

export function isFiltering(f: DashboardFilters): boolean {
  return activeFilterCount(f) > 0;
}

/** Toggle one value in a list facet. */
export function toggleValue<T extends string>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

/** Every whitespace-separated term appears in at least one haystack (case-insensitive). */
export function textMatches(
  q: string,
  haystacks: ReadonlyArray<string | null | undefined>,
): boolean {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const text = haystacks
    .filter((h): h is string => typeof h === 'string' && h.length > 0)
    .join('\n')
    .toLowerCase();
  return terms.every((term) => text.includes(term));
}

export function fileMatches(fileFilter: string, path: string): boolean {
  const needle = normPath(fileFilter.trim()).toLowerCase();
  if (!needle) return true;
  return normPath(path).toLowerCase().includes(needle);
}

function agentOf(user: string, agents: Record<string, AgentKind>): string {
  return agents[user] ?? 'unknown';
}

/** Collision timeline: who was involved, which file, what kind, what scope, what text. */
export function matchesTimelineEntry(
  entry: TimelineEntry,
  f: DashboardFilters,
  agents: Record<string, AgentKind>,
): boolean {
  if (f.people.length && !entry.users.some((u) => f.people.includes(u))) return false;
  if (f.agents.length) {
    const kinds = entry.users.map((u, i) =>
      i === 0 && entry.decision?.agent ? entry.decision.agent : agentOf(u, agents),
    );
    if (!kinds.some((k) => f.agents.includes(k))) return false;
  }
  if (!fileMatches(f.file, entry.file_path)) return false;
  if (f.types.length && !f.types.includes(entry.kind)) return false;
  if (f.scopes.length) {
    if (!entry.decision || !f.scopes.includes(decisionScope(entry.decision))) return false;
  }
  return textMatches(f.q, [
    entry.decision?.reason,
    entry.decision?.suggestion,
    entry.conflict?.explanation,
    ...Object.values(entry.intents),
  ]);
}

/** Heat map edits: person, agent kind, file and text (caption / intent at the time). */
export function matchesTouch(touch: Touch, f: DashboardFilters): boolean {
  if (f.people.length && !f.people.includes(touch.user_id)) return false;
  if (f.agents.length && !f.agents.includes(touch.agent)) return false;
  if (!fileMatches(f.file, touch.file_path)) return false;
  return textMatches(f.q, [touch.caption, touch.intent]);
}

/** Decision facets narrow the heat map to files with a matching collision. */
export function hasDecisionFacets(f: DashboardFilters): boolean {
  return f.types.length > 0 || f.scopes.length > 0;
}

export interface LaneFacts {
  userId: string;
  agent: string;
  /** Files the lane shows: recent edits, plan, decisions. */
  files: string[];
  /** Intent, caption, prompt, plan summary. */
  texts: ReadonlyArray<string | null | undefined>;
  /** Timeline entries this person is involved in. */
  entries: TimelineEntry[];
}

/** Lanes: a lane stays when its person, agent, files, collisions and text all match. */
export function matchesLane(lane: LaneFacts, f: DashboardFilters): boolean {
  if (f.people.length && !f.people.includes(lane.userId)) return false;
  if (f.agents.length && !f.agents.includes(lane.agent)) return false;
  if (f.file.trim() && !lane.files.some((path) => fileMatches(f.file, path))) return false;
  if (hasDecisionFacets(f)) {
    const hit = lane.entries.some(
      (e) =>
        (!f.types.length || f.types.includes(e.kind)) &&
        (!f.scopes.length || (e.decision != null && f.scopes.includes(decisionScope(e.decision)))),
    );
    if (!hit) return false;
  }
  return textMatches(f.q, lane.texts);
}
