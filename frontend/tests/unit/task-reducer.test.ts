/**
 * The dashboard's task list (src/dashboard/taskReducer.ts): task_update frames
 * folded newest-wins, the Tasks view's columns, and the lane card helpers.
 * Run: npm run test:unit.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_TRACKED_TASKS,
  applyTaskEvent,
  claimedBy,
  groupTasks,
  isReady,
  leaseLabel,
  taskResult,
} from '../../src/dashboard/taskReducer.ts';
import type { Task, TaskUpdate } from '../../src/types.ts';

const T0 = 1_700_000_000;

function task(id: string, fields: Partial<Task> = {}): Task {
  return {
    task_id: id,
    title: `task ${id}`,
    files: [],
    depends_on: [],
    status: 'open',
    created_by: 'sam',
    created_at: T0,
    updated_at: T0,
    notes: [],
    ...fields,
  };
}

const update = (t: Task): TaskUpdate => ({ type: 'task_update', task: t });

test('newer snapshots replace older ones, older ones are ignored', () => {
  let tasks = applyTaskEvent({}, update(task('T1', { status: 'claimed', claimer: 'ada', updated_at: T0 + 5 })));
  // A feed event queued before the dashboard joined arrives after its catch-up.
  tasks = applyTaskEvent(tasks, update(task('T1', { updated_at: T0 })));
  assert.equal(tasks.T1.status, 'claimed');
  tasks = applyTaskEvent(tasks, update(task('T1', { status: 'done', updated_at: T0 + 9 })));
  assert.equal(tasks.T1.status, 'done');
});

test('unsafe ids never reach the record', () => {
  const tasks = applyTaskEvent({}, update(task('__proto__')));
  assert.deepEqual(Object.keys(tasks), []);
  assert.equal(({} as Record<string, unknown>).status, undefined);
});

test('the oldest finished tasks go first past the cap', () => {
  let tasks: Record<string, Task> = {};
  tasks = applyTaskEvent(tasks, update(task('T0', { status: 'done' })));
  for (let i = 1; i <= MAX_TRACKED_TASKS; i += 1) tasks = applyTaskEvent(tasks, update(task(`T${i}`)));
  assert.equal(Object.keys(tasks).length, MAX_TRACKED_TASKS);
  assert.equal(tasks.T0, undefined);
  assert.ok(tasks.T1);
});

test('columns: ready, waiting on dependencies, claimed, done', () => {
  const tasks: Record<string, Task> = {
    T1: task('T1', { status: 'claimed', claimer: 'ada', claimed_at: T0 }),
    T2: task('T2', { depends_on: ['T1'] }),
    T3: task('T3'),
    T4: task('T4', { status: 'done', updated_at: T0 + 1 }),
    T5: task('T5', { status: 'cancelled', updated_at: T0 + 2 }),
    T6: task('T6', { depends_on: ['T4', 'T99'] }),
  };
  const groups = groupTasks(tasks);
  assert.deepEqual(groups.ready.map((t) => t.task_id), ['T3', 'T6']);
  assert.deepEqual(groups.waiting.map((t) => t.task_id), ['T2']);
  assert.deepEqual(groups.claimed.map((t) => t.task_id), ['T1']);
  assert.deepEqual(groups.closed.map((t) => t.task_id), ['T5', 'T4']);
  assert.equal(isReady(tasks.T2, tasks), false);
  assert.deepEqual(claimedBy(tasks, 'ada').map((t) => t.task_id), ['T1']);
  assert.deepEqual(claimedBy(tasks, 'ben'), []);
});

test('lease labels and results', () => {
  assert.equal(leaseLabel(task('T1', { lease_until: T0 + 18 * 60 + 30 }), T0), '18 min left');
  assert.equal(leaseLabel(task('T1', { lease_until: T0 + 3 * 3600 }), T0), '3 h left');
  assert.equal(leaseLabel(task('T1', { lease_until: T0 + 20 }), T0), 'under a minute left');
  assert.equal(leaseLabel(task('T1', { lease_until: T0 - 1 }), T0), 'lease lapsing');
  assert.equal(leaseLabel(task('T1'), T0), '');
  const done = task('T1', {
    status: 'done',
    notes: [
      { user_id: 'ada', text: 'halfway', timestamp: T0, kind: 'note' },
      { user_id: 'ada', text: 'PR #12', timestamp: T0 + 1, kind: 'done' },
    ],
  });
  assert.equal(taskResult(done), 'PR #12');
  assert.equal(taskResult(task('T2')), '');
});
