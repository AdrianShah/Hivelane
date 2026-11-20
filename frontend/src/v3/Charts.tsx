import { useMemo, useState } from 'react';
import {
  barHeights,
  overlapBar,
  sessionSeries,
  sparkPath,
  type ChartDot,
  type ChartRange,
  type LineRange,
} from '../dashboard/charts';
import { clockTime } from '../dashboard/people';
import type { SessionLog } from '../dashboard/sessionLog';

/** A small trend line under a key number. Draws in once. */
export function Sparkline({
  values,
  width = 96,
  height = 28,
  tone = 'ink',
}: {
  values: number[];
  width?: number;
  height?: number;
  tone?: 'ink' | 'accent' | 'warn';
}) {
  const path = useMemo(() => sparkPath(values, width, height), [values, width, height]);
  if (!path.line) {
    return <svg className="v3-spark" width={width} height={height} aria-hidden="true" />;
  }
  return (
    <svg
      className={`v3-spark${tone === 'ink' ? '' : ` is-${tone}`}`}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path className="v3-spark__area" d={path.area} />
      <path className="v3-spark__line v3-draw" d={path.line} pathLength={1} />
    </svg>
  );
}

const RANGES: { id: ChartRange; label: string }[] = [
  { id: '15m', label: '15m' },
  { id: '1h', label: '1h' },
  { id: 'all', label: 'All' },
];

const DOT_LABEL: Record<ChartDot['kind'], string> = {
  warn: 'Warning',
  block: 'Held edit',
  conflict: 'Overlap',
};

/** Smooth-ish path through the bucket counts (monotone-ish cubic). */
function curve(points: { x: number; y: number }[]): string {
  if (points.length === 0) return '';
  let d = `M${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length; i += 1) {
    const prev = points[i - 1];
    const cur = points[i];
    const mid = (prev.x + cur.x) / 2;
    d += ` C${mid} ${prev.y} ${mid} ${cur.y} ${cur.x} ${cur.y}`;
  }
  return d;
}

/**
 * The session chart: edits over time (area + line) for 15 min, 1 h or the
 * whole session, with a clickable dot for every warn, held edit and overlap.
 */
export function EditsChart({
  log,
  nowSeconds,
  onDot,
}: {
  log: SessionLog;
  nowSeconds: number;
  onDot: (dot: ChartDot) => void;
}) {
  // The whole session by default: a fresh room's edits fill the chart.
  const [range, setRange] = useState<ChartRange>('all');
  const width = 640;
  const height = 132;
  const top = 10;
  const series = useMemo(
    () =>
      sessionSeries(
        log.touches.map((t) => t.timestamp),
        log.timeline.map((e) => ({ key: e.key, kind: e.kind, timestamp: e.timestamp })),
        range,
        Math.floor(nowSeconds / 5) * 5,
        log.startedAt,
      ),
    [log, range, nowSeconds],
  );
  const max = Math.max(1, ...series.counts);
  const step = width / Math.max(1, series.counts.length - 1);
  const points = series.counts.map((c, i) => ({
    x: Math.round(i * step * 10) / 10,
    y: Math.round((top + (height - top) * (1 - c / max)) * 10) / 10,
  }));
  const line = curve(points);
  const area = line ? `${line} L${width} ${height} L0 ${height} Z` : '';
  const total = series.counts.reduce((a, b) => a + b, 0);

  return (
    <div>
      <div className="v3-section__head">
        <div>
          <p className="v3-title">Session</p>
          <p className="v3-label" style={{ marginTop: 4 }}>
            {total} {total === 1 ? 'edit' : 'edits'} · {series.dots.length}{' '}
            {series.dots.length === 1 ? 'event' : 'events'}
          </p>
        </div>
        <div className="v3-seg" role="group" aria-label="Chart range">
          {RANGES.map((r) => (
            <button key={r.id} type="button" aria-pressed={range === r.id} onClick={() => setRange(r.id)}>
              {r.label}
            </button>
          ))}
        </div>
      </div>
      <div className="v3-chart">
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" height={height} role="img" aria-label={`Edits over the last ${range === 'all' ? 'session' : range}`}>
          {[0.25, 0.5, 0.75].map((f) => (
            <line key={f} className="v3-chart__grid" x1={0} x2={width} y1={top + (height - top) * f} y2={top + (height - top) * f} />
          ))}
          {area ? <path className="v3-chart__area" d={area} /> : null}
          {line ? <path className="v3-chart__line v3-draw" d={line} pathLength={1} key={range} /> : null}
        </svg>
        {series.dots.map((dot) => (
          <button
            key={dot.key}
            type="button"
            className="v3-chart__dot"
            style={{ left: `${dot.x * 100}%`, top: height - 9 }}
            title={`${DOT_LABEL[dot.kind]} at ${clockTime(dot.timestamp)}`}
            aria-label={`${DOT_LABEL[dot.kind]} at ${clockTime(dot.timestamp)}`}
            onClick={() => onDot(dot)}
          >
            <span className={`v3-dot is-${dot.kind === 'conflict' ? 'warn' : dot.kind}`} />
          </button>
        ))}
        <div className="v3-chart__axis" aria-hidden="true">
          <span>{clockTime(series.start)}</span>
          <span>{clockTime(series.start + (series.end - series.start) / 2)}</span>
          <span>now</span>
        </div>
      </div>
      <div className="v3-legend" style={{ marginTop: 12 }}>
        <span>
          <span className="v3-dot is-warn" /> Overlap or warning
        </span>
        <span>
          <span className="v3-dot is-block" /> Held edit
        </span>
      </div>
    </div>
  );
}

/** 24 rising bars: one lane's edits over the last half hour. */
export function LaneBars({ counts, label }: { counts: number[]; label: string }) {
  const heights = barHeights(counts);
  const latest = counts.length - 1 - [...counts].reverse().findIndex((c) => c > 0);
  return (
    <span className="v3-bars" role="img" aria-label={label}>
      {heights.map((h, i) => (
        <span
          key={i}
          className={i === latest && counts[i] > 0 ? 'is-hot' : undefined}
          style={{ height: `${Math.max(8, h * 100)}%`, ['--i' as string]: i }}
        />
      ))}
    </span>
  );
}

/** Where two people's edits of one file sit, and where they overlap. */
export function OverlapBar({
  mine,
  theirs,
  mineLabel,
  theirsLabel,
}: {
  mine: LineRange[];
  theirs: LineRange[];
  mineLabel: string;
  theirsLabel: string;
}) {
  const bar = overlapBar(mine, theirs);
  if (!bar) return null;
  const seg = (cls: string, [a, b]: [number, number], i: number) => (
    <span key={`${cls}${i}`} className={cls} style={{ left: `${a * 100}%`, width: `${Math.max(0.5, (b - a) * 100)}%` }} />
  );
  return (
    <div className="v3-overlap">
      <div className="v3-legend">
        <span>
          <span className="v3-dot" style={{ background: 'color-mix(in oklab, var(--v3-ink) 40%, transparent)' }} />
          {mineLabel}
        </span>
        <span>
          <span className="v3-dot" style={{ background: 'color-mix(in oklab, var(--v3-accent) 55%, transparent)' }} />
          {theirsLabel}
        </span>
        {bar.both.length ? (
          <span>
            <span className="v3-dot is-warn" /> Same lines
          </span>
        ) : null}
      </div>
      <div
        className="v3-overlap__track"
        role="img"
        aria-label={`Lines ${bar.lo} to ${bar.hi}: ${bar.both.length ? 'the edits overlap' : 'no shared lines'}`}
      >
        {bar.mine.map((s, i) => seg('v3-overlap__mine', s, i))}
        {bar.theirs.map((s, i) => seg('v3-overlap__theirs', s, i))}
        {bar.both.map((s, i) => seg('v3-overlap__both', s, i))}
      </div>
      <div className="v3-overlap__axis" aria-hidden="true">
        <span>L{bar.lo}</span>
        <span>L{bar.hi}</span>
      </div>
    </div>
  );
}
