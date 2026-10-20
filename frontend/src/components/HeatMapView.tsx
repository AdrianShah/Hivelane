import { useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  HEAT_WINDOW_SECONDS,
  buildFileTree,
  collectFileStats,
  filesUnder,
  heatIntensity,
  visibleRows,
  type FileStat,
  type TreeNode,
} from '../dashboard/fileTree';
import {
  fileMatches,
  hasDecisionFacets,
  matchesTimelineEntry,
  matchesTouch,
  textMatches,
  type DashboardFilters,
} from '../dashboard/filters';
import { clockTime, initials, personColor, relativeTime } from '../dashboard/people';
import type { SessionLog } from '../dashboard/sessionLog';
import { PersonName } from './PersonBits';

/**
 * The repo as this session has seen it: every file from edits, plans and
 * decisions, coloured by who touched it last (stable colour per person) and
 * brighter the more recent the edit, fading out over the last hour. Planned
 * files nobody has edited yet are hollow. Hover or focus a file for details;
 * activate it to open the collision timeline filtered to that file.
 */

interface HeatMapViewProps {
  log: SessionLog;
  filters: DashboardFilters;
  nowSeconds: number;
  filterBar: ReactNode;
  /** Show the collision timeline filtered to this file. */
  onOpenFile: (path: string) => void;
}

/** Fill opacity: recent edits bright, an hour or older still faintly visible. */
function heatOpacity(intensity: number): number {
  return intensity > 0 ? 0.25 + 0.75 * intensity : 0.14;
}

function HeatCell({ stat, nowSeconds }: { stat: FileStat; nowSeconds: number }) {
  const last = stat.last;
  if (!last) {
    if (stat.plannedBy.length > 0) {
      return (
        <span
          className="heat-cell is-planned"
          style={{ color: personColor(stat.plannedBy[0]) }}
          aria-hidden="true"
        />
      );
    }
    return (
      <span className="heat-cell is-decision-only" aria-hidden="true">
        <span className="heat-cell__label">!</span>
      </span>
    );
  }
  const opacity = heatOpacity(heatIntensity(last.timestamp, nowSeconds));
  return (
    <span className="heat-cell" aria-hidden="true">
      <span
        className="heat-cell__fill"
        style={{ background: personColor(last.user_id), opacity }}
      />
      <span
        className="heat-cell__label"
        style={{ color: opacity >= 0.6 ? '#09090b' : 'rgb(var(--c-on-surface))' }}
      >
        {initials(last.user_id)}
      </span>
    </span>
  );
}

function describe(stat: FileStat, nowSeconds: number): string {
  const parts = [stat.path];
  if (stat.last) {
    parts.push(`last edited by ${stat.last.user_id} ${relativeTime(stat.last.timestamp, nowSeconds)}`);
    if (stat.last.symbols.length) parts.push(`symbols ${stat.last.symbols.join(', ')}`);
  }
  if (stat.plannedBy.length) parts.push(`planned by ${stat.plannedBy.join(', ')}`);
  if (stat.collisions) parts.push(`${stat.collisions} collision${stat.collisions === 1 ? '' : 's'}`);
  parts.push('open in timeline');
  return parts.join(', ');
}

interface TooltipState {
  stat: FileStat;
  x: number;
  y: number;
}

function Tooltip({ tip, nowSeconds }: { tip: TooltipState; nowSeconds: number }) {
  const { stat } = tip;
  return (
    <div className="heat-tooltip" style={{ left: tip.x, top: tip.y }} role="tooltip">
      <p className="m-0 font-mono text-[11.5px] text-primary break-all">{stat.path}</p>
      {stat.last ? (
        <p className="m-0 mt-1.5 flex items-center gap-1.5 flex-wrap">
          <PersonName userId={stat.last.user_id} className="font-semibold" />
          <span className="text-on-muted">
            {relativeTime(stat.last.timestamp, nowSeconds)} · {clockTime(stat.last.timestamp)}
          </span>
        </p>
      ) : (
        <p className="m-0 mt-1.5 text-on-muted">Not edited this session</p>
      )}
      {stat.last && stat.last.symbols.length > 0 ? (
        <p className="m-0 mt-1 font-mono text-[11px] text-lavender break-words">
          {stat.last.symbols.join(', ')}
        </p>
      ) : null}
      <p className="m-0 mt-1.5 text-[11px] text-on-dim">
        {stat.edits} edit{stat.edits === 1 ? '' : 's'}
        {stat.editors.length > 1 ? ` by ${stat.editors.join(', ')}` : ''}
        {stat.collisions ? ` · ${stat.collisions} collision${stat.collisions === 1 ? '' : 's'}` : ''}
      </p>
      {stat.plannedBy.length > 0 ? (
        <p className="m-0 mt-1 text-[11px] text-on-dim">Planned by {stat.plannedBy.join(', ')}</p>
      ) : null}
    </div>
  );
}

function DirStrip({ node, nowSeconds }: { node: TreeNode; nowSeconds: number }) {
  const files = filesUnder(node).slice(0, 12);
  return (
    <span className="heat-strip" aria-hidden="true">
      {files.map((stat) => (
        <span
          key={stat.path}
          style={
            stat.last
              ? {
                  background: personColor(stat.last.user_id),
                  opacity: heatOpacity(heatIntensity(stat.last.timestamp, nowSeconds)),
                }
              : stat.plannedBy.length
                ? { background: 'transparent', boxShadow: `inset 0 0 0 1.5px ${personColor(stat.plannedBy[0])}` }
                : undefined
          }
        />
      ))}
    </span>
  );
}

export default function HeatMapView({ log, filters, nowSeconds, filterBar, onOpenFile }: HeatMapViewProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [tip, setTip] = useState<TooltipState | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  // Plans expire on the relay clock; a minute's resolution is plenty here.
  const minute = Math.floor(nowSeconds / 60);

  const tree = useMemo(() => {
    const now = minute * 60;
    const touches = log.touches.filter((t) => matchesTouch(t, filters));
    const plans = Object.values(log.plans).filter(
      (p) =>
        p.expires_at > now &&
        (!filters.people.length || filters.people.includes(p.user_id)) &&
        (!filters.agents.length || filters.agents.includes(log.agents[p.user_id] ?? 'unknown')) &&
        textMatches(filters.q, [p.summary, log.intents[p.user_id]?.prompt]),
    );
    const timeline = log.timeline.filter((e) => matchesTimelineEntry(e, filters, log.agents));
    let stats = collectFileStats({ touches, plans, timeline });
    if (filters.file.trim()) stats = stats.filter((s) => fileMatches(filters.file, s.path));
    if (hasDecisionFacets(filters)) stats = stats.filter((s) => s.collisions > 0);
    return buildFileTree(stats);
  }, [log, filters, minute]);

  const rows = visibleRows(tree, collapsed);
  const allDirs = useMemo(() => {
    const out: string[] = [];
    const walk = (n: TreeNode) => {
      for (const c of n.children) {
        if (c.kind === 'dir') {
          out.push(c.path);
          walk(c);
        }
      }
    };
    walk(tree);
    return out;
  }, [tree]);

  const people = useMemo(() => {
    const seen = new Set<string>();
    for (const stat of filesUnder(tree)) {
      if (stat.last) seen.add(stat.last.user_id);
      for (const p of stat.plannedBy) seen.add(p);
    }
    return [...seen].sort();
  }, [tree]);

  const recentEdits = log.touches.filter(
    (t) => nowSeconds - t.timestamp <= HEAT_WINDOW_SECONDS && matchesTouch(t, filters),
  ).length;

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const showTip = (stat: FileStat, el: HTMLElement) => {
    const rect = el.getBoundingClientRect();
    const x = Math.min(rect.left + 40, window.innerWidth - 360);
    const below = rect.bottom + 6;
    const y = below + 150 > window.innerHeight ? Math.max(8, rect.top - 150) : below;
    setTip({ stat, x: Math.max(8, x), y });
  };

  // Up/Down move between rows; Left/Right fold and unfold folders.
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, node: TreeNode) => {
    const buttons = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>('button.heat-row') ?? []);
    const index = buttons.indexOf(event.currentTarget);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const next = buttons[index + (event.key === 'ArrowDown' ? 1 : -1)];
      next?.focus();
    } else if (node.kind === 'dir' && event.key === 'ArrowLeft' && !collapsed.has(node.path)) {
      event.preventDefault();
      toggle(node.path);
    } else if (node.kind === 'dir' && event.key === 'ArrowRight' && collapsed.has(node.path)) {
      event.preventDefault();
      toggle(node.path);
    }
  };

  return (
    <div className="p-4 sm:p-6 lg:p-8 animate-fade-up">
      <div className="max-w-[1400px]">
        <h1 className="text-xl font-bold tracking-tight text-on-surface m-0">File heat map</h1>
        <p className="text-[13px] text-on-muted mt-1 mb-5">
          Every file this session touched, coloured by who edited it last and brighter the more
          recent. Select a file to see its collisions.
        </p>
        {filterBar}

        <section className="lane-shell rounded-2xl p-4 sm:p-5" aria-labelledby="heat-title">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mb-3">
            <h2 id="heat-title" className="section-label m-0">
              {tree.fileCount} file{tree.fileCount === 1 ? '' : 's'} · {recentEdits} edit
              {recentEdits === 1 ? '' : 's'} in the last hour
            </h2>
            <div className="flex items-center gap-2 text-[11px] text-on-dim" aria-label="Recency scale">
              <span>1 h ago</span>
              <span className="heat-scale" aria-hidden="true" />
              <span>now</span>
            </div>
            <div className="flex items-center gap-1.5 text-[11px] text-on-dim">
              <span className="heat-cell is-planned" style={{ color: 'rgb(var(--c-on-dim))' }} aria-hidden="true" />
              planned, not edited yet
            </div>
            <div className="flex items-center gap-1.5 text-[11px] text-on-dim">
              <span className="heat-cell is-decision-only" aria-hidden="true">
                <span className="heat-cell__label">!</span>
              </span>
              only in a warning
            </div>
            {allDirs.length > 0 ? (
              <div className="flex items-center gap-1.5 ml-auto">
                <button type="button" className="filter-chip" onClick={() => setCollapsed(new Set())}>
                  Expand all
                </button>
                <button type="button" className="filter-chip" onClick={() => setCollapsed(new Set(allDirs))}>
                  Collapse all
                </button>
              </div>
            ) : null}
          </div>

          {people.length > 0 ? (
            <ul className="list-none m-0 p-0 mb-3 flex flex-wrap gap-x-3 gap-y-1 text-[11.5px]" aria-label="People">
              {people.map((p) => (
                <li key={p}>
                  <PersonName userId={p} className="text-on-muted" />
                </li>
              ))}
            </ul>
          ) : null}

          {rows.length === 0 ? (
            <p className="m-0 py-6 text-center text-[13px] text-on-muted">
              No files yet{filters.people.length || filters.file || filters.q ? ' match these filters' : ''}. Edits,
              plans and warnings appear here as agents work.
            </p>
          ) : (
            <ul ref={listRef} className="heat-tree" aria-label="Files by folder" onMouseLeave={() => setTip(null)}>
              {rows.map(({ node, depth }) => {
                const pad = { paddingLeft: `${0.5 + depth * 1.1}rem` };
                if (node.kind === 'dir') {
                  const open = !collapsed.has(node.path);
                  return (
                    <li key={`d:${node.path}`}>
                      <button
                        type="button"
                        className="heat-row is-dir"
                        style={pad}
                        aria-expanded={open}
                        onClick={() => toggle(node.path)}
                        onKeyDown={(e) => onKeyDown(e, node)}
                      >
                        <span
                          className={`material-symbols-outlined text-[16px] text-on-dim transition-transform ${open ? 'rotate-90' : ''}`}
                          aria-hidden="true"
                        >
                          chevron_right
                        </span>
                        <span className="material-symbols-outlined text-[16px] text-on-dim" aria-hidden="true">
                          {open ? 'folder_open' : 'folder'}
                        </span>
                        <span className="font-mono text-[12px] truncate min-w-0">{node.name}/</span>
                        <span className="text-[10.5px] text-on-dim shrink-0 tabular-nums">
                          {node.fileCount}
                          <span className="sr-only"> files</span>
                        </span>
                        {!open ? <DirStrip node={node} nowSeconds={nowSeconds} /> : null}
                        <span className="ml-auto text-[10.5px] text-on-dim shrink-0 tabular-nums hidden sm:inline">
                          {node.lastAt != null ? relativeTime(node.lastAt, nowSeconds) : ''}
                        </span>
                      </button>
                    </li>
                  );
                }
                const stat = node.stat;
                if (!stat) return null;
                return (
                  <li key={`f:${node.path}`}>
                    <button
                      type="button"
                      className={`heat-row ${tip?.stat.path === stat.path ? 'is-active' : ''}`}
                      style={pad}
                      aria-label={describe(stat, nowSeconds)}
                      onClick={() => onOpenFile(stat.path)}
                      onMouseEnter={(e) => showTip(stat, e.currentTarget)}
                      onFocus={(e) => showTip(stat, e.currentTarget)}
                      onBlur={() => setTip(null)}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') setTip(null);
                        onKeyDown(e, node);
                      }}
                    >
                      <span className="w-4 shrink-0" aria-hidden="true" />
                      <HeatCell stat={stat} nowSeconds={nowSeconds} />
                      <span className="font-mono text-[12px] truncate min-w-0">{node.name}</span>
                      {stat.collisions > 0 ? (
                        <span className="outcome-tag is-edited-anyway shrink-0" title="Collisions on this file">
                          <span className="material-symbols-outlined" aria-hidden="true">
                            warning
                          </span>
                          {stat.collisions}
                        </span>
                      ) : null}
                      <span className="ml-auto text-[10.5px] text-on-dim shrink-0 tabular-nums">
                        {stat.last
                          ? relativeTime(stat.last.timestamp, nowSeconds)
                          : stat.plannedBy.length
                            ? 'planned'
                            : ''}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
      {/* Portalled: the view's fade-in transform would otherwise anchor position:fixed. */}
      {tip ? createPortal(<Tooltip tip={tip} nowSeconds={nowSeconds} />, document.body) : null}
    </div>
  );
}
