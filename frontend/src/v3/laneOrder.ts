import type { LaneView } from './model';

/**
 * Where a lane works, beside its agent: subagent type, worktree, branch.
 * A subagent drawn under its parent leaves out the worktree and branch the
 * parent row already shows; only a difference is worth the space.
 */
export function laneWhere(
  lane: Pick<LaneView, 'subagentType' | 'worktree' | 'branch'>,
  parent?: Pick<LaneView, 'worktree' | 'branch'>,
): string {
  const parts: string[] = [];
  if (lane.subagentType) parts.push(`${lane.subagentType} subagent`);
  if (lane.worktree && lane.worktree !== parent?.worktree) parts.push(`worktree ${lane.worktree}`);
  if (lane.branch && lane.branch !== lane.worktree && lane.branch !== parent?.branch) parts.push(lane.branch);
  return parts.map((part) => ` · ${part}`).join('');
}

const LANE_RANK = { blocked: 0, waiting: 1, conflict: 2, editing: 3, idle: 4, offline: 5 } as const;

function byUrgency(a: LaneView, b: LaneView): number {
  return LANE_RANK[a.state] - LANE_RANK[b.state] || a.id.localeCompare(b.id);
}

/**
 * Lanes in board order: main sessions by urgency, each followed by its own
 * subagents (also by urgency). A subagent whose parent is not on the board
 * stands on its own, so nothing is ever hidden.
 */
export function nestLanes(
  lanes: LaneView[],
  byId: Record<string, LaneView>,
): { lane: LaneView; nested: boolean }[] {
  const children = new Map<string, LaneView[]>();
  const roots: LaneView[] = [];
  for (const lane of lanes) {
    const parent = lane.parent && lane.parent !== lane.id && byId[lane.parent] ? lane.parent : '';
    if (parent) children.set(parent, [...(children.get(parent) ?? []), lane]);
    else roots.push(lane);
  }
  const out: { lane: LaneView; nested: boolean }[] = [];
  for (const root of roots.sort(byUrgency)) {
    out.push({ lane: root, nested: false });
    for (const child of (children.get(root.id) ?? []).sort(byUrgency)) out.push({ lane: child, nested: true });
  }
  return out;
}
