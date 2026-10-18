// Frame-handling cost of the dashboard's pure logic (frontend/src/dashboard),
// without a browser: the session-log reducer every feed event goes through,
// plus the derived views React recomputes from it (prevention summary, room
// stats, heat-map file tree). Node >= 22.18 runs the .ts sources directly.
//
//   node scripts/loadtest/dashboard_frames.mjs            # 1k and 10k events
//   node scripts/loadtest/dashboard_frames.mjs 50000
import { performance } from 'node:perf_hooks';
import { writeFileSync, mkdirSync } from 'node:fs';
import { createSessionLog, reduceSessionLog } from '../../frontend/src/dashboard/sessionLog.ts';
import { summarizePrevention } from '../../frontend/src/dashboard/prevented.ts';
import { roomStats } from '../../frontend/src/dashboard/stats.ts';
import { collectFileStats, buildFileTree } from '../../frontend/src/dashboard/fileTree.ts';

const DIFF = Array.from({ length: 36 }, (_, i) => `+    line ${i}: value = compute(value, ${i})  # padding padding`).join('\n');
const USERS = Array.from({ length: 12 }, (_, i) => `dev${i}`);

function events(n) {
  const out = [];
  let t = 1_700_000_000;
  for (let i = 0; i < n; i++) {
    t += 0.25;
    const user = USERS[i % USERS.length];
    const file = `src/pkg${i % 7}/mod${i % 40}.py`;
    const r = i % 10;
    if (r < 6) {
      out.push({ type: 'activity_update', user_id: user, agent: 'claude-code', timestamp: t, prompt: 'make the change',
        caption: null, changes: [{ file_path: file, diff: DIFF, symbols: [`fn${i % 8}`] }] });
    } else if (r < 8) {
      out.push({ type: 'edit_decision', user_id: user, agent: 'claude-code', file_path: file, decision: r === 6 ? 'block' : 'warn',
        others: [USERS[(i + 1) % USERS.length]], reason: 'x', timestamp: t, scope: 'symbol', decision_id: `d${i}` });
    } else if (r === 8) {
      out.push({ type: 'intent_update', user_id: user, agent: 'claude-code', prompt: `fix mod${i % 40}.py`, timestamp: t });
    } else {
      out.push({ type: 'conflict_alert', file_path: file, users: [user, USERS[(i + 3) % USERS.length]], verdict: 'conflict',
        explanation: 'same function', timestamp: t });
    }
  }
  return out;
}

function run(n) {
  const evs = events(n);
  const now = evs[evs.length - 1].timestamp;
  let log = createSessionLog();
  const t0 = performance.now();
  for (const ev of evs) log = reduceSessionLog(log, ev);
  const reduceMs = performance.now() - t0;
  const t1 = performance.now();
  const prevention = summarizePrevention(log, now);
  const stats = roomStats(log, prevention, {}, now);
  const derivedMs = performance.now() - t1;
  const t2 = performance.now();
  const tree = buildFileTree(collectFileStats({ touches: log.touches, plans: Object.values(log.plans), timeline: log.timeline }));
  const treeMs = performance.now() - t2;
  // One more frame on the full log: the steady-state per-feed cost (reduce + derived views).
  const t3 = performance.now();
  const reps = 50;
  for (let i = 0; i < reps; i++) {
    const extra = { ...evs[0], timestamp: now + 1 + i, user_id: 'late' };
    const l2 = reduceSessionLog(log, extra);
    const p2 = summarizePrevention(l2, now);
    roomStats(l2, p2, {}, now);
    buildFileTree(collectFileStats({ touches: l2.touches, plans: Object.values(l2.plans), timeline: l2.timeline }));
  }
  const frameMs = (performance.now() - t3) / reps;
  return {
    events: n, reduce_total_ms: +reduceMs.toFixed(1), reduce_per_event_us: +((reduceMs / n) * 1000).toFixed(1),
    derived_views_ms: +derivedMs.toFixed(2), file_tree_ms: +treeMs.toFixed(2), steady_frame_ms: +frameMs.toFixed(2),
    touches_kept: log.touches.length, timeline_kept: log.timeline.length, blocks: stats.blocks ?? null,
    tree_children: tree.children?.length ?? null,
  };
}

const sizes = process.argv.slice(2).map(Number).filter(Boolean);
const results = (sizes.length ? sizes : [1000, 10000]).map(run);
console.table(results);
mkdirSync(new URL('../../benchmarks/profile/', import.meta.url), { recursive: true });
writeFileSync(new URL('../../benchmarks/profile/dashboard_frames.json', import.meta.url), JSON.stringify({ node: process.version, results }, null, 2));
