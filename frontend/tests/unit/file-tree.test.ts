/**
 * The heat map's tree building, compaction, recency and visible rows.
 * Run: npm run test:unit
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildFileTree,
  collectFileStats,
  filesUnder,
  heatIntensity,
  visibleRows,
  type TreeNode,
} from '../../src/dashboard/fileTree.ts';
import {
  createSessionLog,
  reduceSessionLog,
  type SessionLogAction,
} from '../../src/dashboard/sessionLog.ts';
import { activity, decision, plan } from './fixtures.ts';

function log(actions: SessionLogAction[]) {
  return actions.reduce(reduceSessionLog, createSessionLog());
}

function shape(node: TreeNode): unknown {
  return node.kind === 'file' ? node.name : { [node.name]: node.children.map(shape) };
}

test('paths fold into a sorted tree, single-folder chains compacted', () => {
  const l = log([
    activity('sam', 1, ['src/components/Lane.tsx', 'src/components/Sidebar.tsx']),
    activity('ada', 2, ['src/App.tsx', 'README.md', 'server\\deep\\only\\room.py']),
  ]);
  const tree = buildFileTree(collectFileStats({ touches: l.touches, plans: [], timeline: [] }));
  assert.deepEqual(shape(tree), {
    '': [
      { 'server/deep/only': ['room.py'] },
      { src: [{ components: ['Lane.tsx', 'Sidebar.tsx'] }, 'App.tsx'] },
      'README.md',
    ],
  });
  assert.equal(tree.fileCount, 5);
  assert.equal(tree.lastAt, 2);
  const compacted = tree.children[0];
  assert.equal(compacted.path, 'server/deep/only');
});

test('file stats: newest editor wins, editors listed newest first, collisions counted', () => {
  const l = log([
    activity('sam', 1, ['a.ts']),
    activity('ada', 5, ['a.ts']),
    activity('sam', 3, ['b.ts']),
    decision('lin', 6, 'a.ts', 'block'),
  ]);
  const stats = collectFileStats({ touches: l.touches, plans: [], timeline: l.timeline });
  const a = stats.find((s) => s.path === 'a.ts');
  assert.equal(a?.last?.user_id, 'ada');
  assert.deepEqual(a?.editors, ['ada', 'sam']);
  assert.equal(a?.edits, 2);
  assert.equal(a?.collisions, 1);
});

test('planned files appear hollow until the planner edits them', () => {
  const l = log([plan('sam', 10, ['src/new.ts', 'src/old.ts']), activity('sam', 20, ['src/old.ts'])]);
  const stats = collectFileStats({
    touches: l.touches,
    plans: Object.values(l.plans),
    timeline: [],
  });
  const fresh = stats.find((s) => s.path === 'src/new.ts');
  const done = stats.find((s) => s.path === 'src/old.ts');
  assert.equal(fresh?.last, null);
  assert.deepEqual(fresh?.plannedBy, ['sam']);
  assert.deepEqual(done?.plannedBy, []);
});

test('decision-only files are part of the tree', () => {
  const l = log([decision('sam', 1, 'blocked/only.ts', 'block')]);
  const tree = buildFileTree(collectFileStats({ touches: [], plans: [], timeline: l.timeline }));
  assert.equal(tree.fileCount, 1);
  assert.equal(tree.children[0].name, 'blocked');
});

test('heat fades linearly over the hour', () => {
  assert.equal(heatIntensity(null, 100), 0);
  assert.equal(heatIntensity(100, 100), 1);
  assert.equal(heatIntensity(100, 100 + 1800), 0.5);
  assert.equal(heatIntensity(100, 100 + 3600), 0);
  assert.equal(heatIntensity(200, 100), 1); // clock skew: never above 1
});

test('collapsed folders hide their rows; folder strips list files newest first', () => {
  const l = log([
    activity('sam', 1, ['src/a.ts']),
    activity('sam', 2, ['src/lib/b.ts']),
    activity('sam', 3, ['top.ts']),
  ]);
  const tree = buildFileTree(collectFileStats({ touches: l.touches, plans: [], timeline: [] }));
  const open = visibleRows(tree, new Set());
  assert.deepEqual(
    open.map((r) => [r.node.name, r.depth]),
    [
      ['src', 0],
      ['lib', 1],
      ['b.ts', 2],
      ['a.ts', 1],
      ['top.ts', 0],
    ],
  );
  const closed = visibleRows(tree, new Set(['src']));
  assert.deepEqual(closed.map((r) => r.node.name), ['src', 'top.ts']);
  assert.deepEqual(filesUnder(tree.children[0]).map((s) => s.path), ['src/lib/b.ts', 'src/a.ts']);
});
