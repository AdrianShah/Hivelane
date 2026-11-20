/**
 * Pure geometry for the v3 dashboard's small charts: sparkline paths, the
 * session chart's re-bucketed series and event dots, per-lane activity bars
 * and the overlap bar drawn from two people's line ranges. Kept free of
 * React so every number on the dashboard is unit-tested.
 */

/** A point list as an SVG path inside width × height (y grows down). */
export interface SparkPath {
  line: string;
  /** The same line closed down to the baseline, for the area fill. */
  area: string;
  /** Last point, for the end dot. */
  end: { x: number; y: number } | null;
}

function round(n: number): number {
  return Math.round(n * 100) / 100;
}

/**
 * Sparkline path for `values` across `width` × `height`, with `pad` px kept
 * clear at the top and bottom so the stroke is never clipped. All-equal
 * values draw a flat line in the middle; fewer than two points draw nothing.
 */
export function sparkPath(values: number[], width: number, height: number, pad = 2): SparkPath {
  const clean = values.map((v) => (Number.isFinite(v) ? v : 0));
  if (clean.length < 2 || width <= 0 || height <= 0) return { line: '', area: '', end: null };
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min;
  const inner = Math.max(0, height - pad * 2);
  const step = width / (clean.length - 1);
  const points = clean.map((v, i) => ({
    x: round(i * step),
    y: round(span === 0 ? height / 2 : pad + inner - ((v - min) / span) * inner),
  }));
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x} ${p.y}`).join(' ');
  const area = `${line} L${round(width)} ${round(height)} L0 ${round(height)} Z`;
  return { line, area, end: points[points.length - 1] };
}

/**
 * "Now" on the dashboard moves in coarse steps while relay timestamps do
 * not, so an edit can land a few seconds after it. Anything up to this far
 * ahead counts in the last bucket instead of disappearing.
 */
export const CLOCK_SLACK_SECONDS = 120;

export type ChartRange = '15m' | '1h' | 'all';

export const RANGE_SECONDS: Record<Exclude<ChartRange, 'all'>, number> = {
  '15m': 15 * 60,
  '1h': 60 * 60,
};

export type DotKind = 'warn' | 'block' | 'conflict';

export interface ChartDot {
  key: string;
  kind: DotKind;
  /** 0..1 across the chart. */
  x: number;
  /** Bucket index it falls in. */
  bucket: number;
  timestamp: number;
}

export interface SessionSeries {
  start: number;
  end: number;
  counts: number[];
  dots: ChartDot[];
}

/**
 * Edits per bucket for the chosen range ending at `now`, and the warns /
 * blocks / overlaps inside it as dots. "all" spans from the session's start.
 * Timestamps outside the window are left out.
 */
export function sessionSeries(
  editTimes: number[],
  events: { key: string; kind: DotKind; timestamp: number }[],
  range: ChartRange,
  now: number,
  sessionStart: number | null,
  buckets = 48,
): SessionSeries {
  const end = now;
  const start =
    range === 'all' ? Math.min(sessionStart ?? now - 60, now - 60) : now - RANGE_SECONDS[range];
  const span = Math.max(1, end - start);
  const counts = new Array<number>(buckets).fill(0);
  const bucketOf = (ts: number) => Math.min(buckets - 1, Math.floor(((ts - start) / span) * buckets));
  const inside = (ts: number) => ts >= start && ts <= end + CLOCK_SLACK_SECONDS;
  for (const ts of editTimes) {
    if (!inside(ts)) continue;
    counts[bucketOf(Math.min(ts, end))] += 1;
  }
  const dots = events
    .filter((e) => inside(e.timestamp))
    .map((e) => ({
      key: e.key,
      kind: e.kind,
      timestamp: e.timestamp,
      x: (Math.min(e.timestamp, end) - start) / span,
      bucket: bucketOf(Math.min(e.timestamp, end)),
    }))
    .sort((a, b) => a.timestamp - b.timestamp);
  return { start, end, counts, dots };
}

/**
 * One lane's activity over the last `windowSeconds`, in `buckets` equal
 * slices (oldest first): how many edits landed in each. The lane row draws
 * these as a small bar chart.
 */
export function laneBuckets(
  editTimes: number[],
  now: number,
  windowSeconds = 30 * 60,
  buckets = 24,
): number[] {
  const out = new Array<number>(buckets).fill(0);
  const start = now - windowSeconds;
  for (const ts of editTimes) {
    if (ts < start || ts > now + CLOCK_SLACK_SECONDS) continue;
    const index = Math.min(buckets - 1, Math.floor(((Math.min(ts, now) - start) / windowSeconds) * buckets));
    out[index] += 1;
  }
  return out;
}

/** Bar heights 0..1 for counts (the busiest bucket is 1; empty stays 0). */
export function barHeights(counts: number[]): number[] {
  const max = Math.max(0, ...counts);
  return counts.map((c) => (max === 0 ? 0 : c / max));
}

export type LineRange = [number, number];

export interface OverlapBar {
  /** First and last line covered by either side. */
  lo: number;
  hi: number;
  /** Segments as 0..1 fractions of [lo, hi]. */
  mine: [number, number][];
  theirs: [number, number][];
  both: [number, number][];
}

function merge(ranges: LineRange[]): LineRange[] {
  const sorted = ranges
    .map(([a, b]) => [Math.min(a, b), Math.max(a, b)] as LineRange)
    .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b))
    .sort((x, y) => x[0] - y[0]);
  const out: LineRange[] = [];
  for (const [a, b] of sorted) {
    const last = out[out.length - 1];
    if (last && a <= last[1] + 1) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}

/**
 * Where two people's edits of one file sit, and where they overlap, from
 * FileChange.line_ranges (inclusive line numbers). Null when either side
 * has no ranges (the relay judged the file as a whole).
 */
export function overlapBar(mine: LineRange[], theirs: LineRange[]): OverlapBar | null {
  const a = merge(mine);
  const b = merge(theirs);
  if (a.length === 0 || b.length === 0) return null;
  const lo = Math.min(a[0][0], b[0][0]);
  const hi = Math.max(a[a.length - 1][1], b[b.length - 1][1]);
  const span = Math.max(1, hi - lo + 1);
  const frac = ([x, y]: LineRange): [number, number] => [
    round((x - lo) / span),
    round((y - lo + 1) / span),
  ];
  const both: LineRange[] = [];
  for (const [x1, y1] of a) {
    for (const [x2, y2] of b) {
      const x = Math.max(x1, x2);
      const y = Math.min(y1, y2);
      if (x <= y) both.push([x, y]);
    }
  }
  return { lo, hi, mine: a.map(frac), theirs: b.map(frac), both: merge(both).map(frac) };
}
