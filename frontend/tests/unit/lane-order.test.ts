import assert from 'node:assert/strict';
import test from 'node:test';

import { laneWhere, nestLanes } from '../../src/v3/laneOrder.ts';
import type { LaneView } from '../../src/v3/model.ts';

function lane(id: string, extra: Partial<LaneView> = {}): LaneView {
  return { id, state: 'idle', parent: '', subagentType: '', worktree: '', branch: '', ...extra } as LaneView;
}

test('subagents sit right under the chat that started them, by urgency', () => {
  const lanes = [
    lane('ada-explore-1', { parent: 'ada', subagentType: 'Explore' }),
    lane('sam', { state: 'editing' }),
    lane('ada'),
    lane('ada-plan-2', { parent: 'ada', state: 'blocked' }),
    lane('ada-ghost-merge', { worktree: 'ghost-merge' }),
  ];
  const byId = Object.fromEntries(lanes.map((l) => [l.id, l]));
  assert.deepEqual(
    nestLanes(lanes, byId).map(({ lane: l, nested }) => `${nested ? '  ' : ''}${l.id}`),
    ['sam', 'ada', '  ada-plan-2', '  ada-explore-1', 'ada-ghost-merge'],
  );
});

test('a subagent whose parent is not on the board still shows, on its own', () => {
  const lanes = [lane('ada-explore-1', { parent: 'gone' }), lane('loop', { parent: 'loop' })];
  const byId = Object.fromEntries(lanes.map((l) => [l.id, l]));
  assert.deepEqual(
    nestLanes(lanes, byId).map(({ nested }) => nested),
    [false, false],
  );
});

test('laneWhere names the subagent type, the worktree and a branch that differs from it', () => {
  assert.equal(laneWhere({ subagentType: 'Explore', worktree: 'ghost-merge', branch: 'feat/ghost-merge' }),
    ' · Explore subagent · worktree ghost-merge · feat/ghost-merge');
  assert.equal(laneWhere({ subagentType: '', worktree: 'main-fix', branch: 'main-fix' }), ' · worktree main-fix');
  assert.equal(laneWhere({ subagentType: '', worktree: '', branch: 'main' }), ' · main');
  assert.equal(laneWhere({ subagentType: '', worktree: '', branch: '' }), '');
  // Under its parent, a subagent only says what differs from the parent row.
  const parent = { worktree: 'ghost-merge', branch: 'feat/ghost-merge' };
  assert.equal(laneWhere({ subagentType: 'Explore', worktree: 'ghost-merge', branch: 'feat/ghost-merge' }, parent),
    ' · Explore subagent');
  assert.equal(laneWhere({ subagentType: 'Plan', worktree: 'agent-a7f4', branch: 'worktree-agent-a7f4' }, parent),
    ' · Plan subagent · worktree agent-a7f4 · worktree-agent-a7f4');
});
