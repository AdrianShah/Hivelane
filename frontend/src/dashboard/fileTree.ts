/**
 * The heat map's repo tree: every file path the session has seen in edits,
 * plans or decisions, folded into directories. Pure, so the shape (and the
 * compaction of single-child folders) is unit-tested.
 */
import type { PlanUpdate } from '../types';
import { normPath, pathSegments } from './paths.ts';
import type { TimelineEntry, Touch } from './sessionLog.ts';

/** Recency fades to nothing over this window (the last hour). */
export const HEAT_WINDOW_SECONDS = 3600;

export interface FileStat {
  path: string;
  /** Most recent edit (after filters), or null for planned/decision-only files. */
  last: Touch | null;
  /** Edits seen this session. */
  edits: number;
  /** Distinct editors, most recent first. */
  editors: string[];
  /** People whose active plan lists this file and who have not edited it since. */
  plannedBy: string[];
  /** Warns, blocks and conflicts on this file. */
  collisions: number;
}

export interface TreeNode {
  /** Display name; compacted folders read "src/components". */
  name: string;
  /** Full path ('' for the root). */
  path: string;
  kind: 'dir' | 'file';
  children: TreeNode[];
  stat: FileStat | null;
  /** Newest edit at or below this node, seconds. */
  lastAt: number | null;
  /** Files at or below this node. */
  fileCount: number;
}

export interface FileStatInput {
  touches: Touch[];
  /** Active plans only (the caller drops expired ones and applies filters). */
  plans: PlanUpdate[];
  timeline: TimelineEntry[];
}

export function collectFileStats({ touches, plans, timeline }: FileStatInput): FileStat[] {
  const stats = new Map<string, FileStat>();
  const statFor = (rawPath: string): FileStat | null => {
    const path = normPath(rawPath);
    if (pathSegments(path).length === 0) return null;
    let stat = stats.get(path);
    if (!stat) {
      stat = { path, last: null, edits: 0, editors: [], plannedBy: [], collisions: 0 };
      stats.set(path, stat);
    }
    return stat;
  };

  // Newest edit per (person, file), for "planned but not edited since".
  const lastEditBy = new Map<string, number>();
  // Touches are ascending, so the last one written per file is the newest.
  for (const touch of touches) {
    const stat = statFor(touch.file_path);
    if (!stat) continue;
    stat.edits += 1;
    stat.last = touch;
    lastEditBy.set(`${touch.user_id}\u0000${stat.path}`, touch.timestamp);
  }
  for (let i = touches.length - 1; i >= 0; i -= 1) {
    const touch = touches[i];
    const stat = stats.get(normPath(touch.file_path));
    if (stat && !stat.editors.includes(touch.user_id)) stat.editors.push(touch.user_id);
  }
  for (const plan of plans) {
    for (const file of plan.files) {
      const stat = statFor(file);
      if (!stat) continue;
      const lastEdit = lastEditBy.get(`${plan.user_id}\u0000${stat.path}`);
      const editedSince = lastEdit != null && lastEdit >= plan.timestamp;
      if (!editedSince && !stat.plannedBy.includes(plan.user_id)) {
        stat.plannedBy.push(plan.user_id);
      }
    }
  }
  for (const entry of timeline) {
    const stat = statFor(entry.file_path);
    if (stat) stat.collisions += 1;
  }
  return [...stats.values()];
}

function sortTree(node: TreeNode): void {
  node.children.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const child of node.children) sortTree(child);
}

function aggregate(node: TreeNode): void {
  if (node.kind === 'file') {
    node.fileCount = 1;
    node.lastAt = node.stat?.last?.timestamp ?? null;
    return;
  }
  let count = 0;
  let lastAt: number | null = null;
  for (const child of node.children) {
    aggregate(child);
    count += child.fileCount;
    if (child.lastAt != null && (lastAt == null || child.lastAt > lastAt)) {
      lastAt = child.lastAt;
    }
  }
  node.fileCount = count;
  node.lastAt = lastAt;
}

/** Merge chains of single-folder directories ("src" > "components" → "src/components"). */
export function compactTree(node: TreeNode): TreeNode {
  if (node.kind === 'file') return node;
  let current = node;
  // Never fold the root itself: it has no name to join onto.
  if (current.path !== '') {
    while (current.children.length === 1 && current.children[0].kind === 'dir') {
      const only = current.children[0];
      current = { ...only, name: `${current.name}/${only.name}` };
    }
  }
  return { ...current, children: current.children.map(compactTree) };
}

export function buildFileTree(stats: FileStat[]): TreeNode {
  const root: TreeNode = {
    name: '',
    path: '',
    kind: 'dir',
    children: [],
    stat: null,
    lastAt: null,
    fileCount: 0,
  };
  const dirs = new Map<string, TreeNode>([['', root]]);
  for (const stat of stats) {
    const parts = pathSegments(stat.path);
    if (parts.length === 0) continue;
    let parent = root;
    for (let i = 0; i < parts.length - 1; i += 1) {
      const path = parts.slice(0, i + 1).join('/');
      let dir = dirs.get(path);
      if (!dir) {
        dir = {
          name: parts[i],
          path,
          kind: 'dir',
          children: [],
          stat: null,
          lastAt: null,
          fileCount: 0,
        };
        dirs.set(path, dir);
        parent.children.push(dir);
      }
      parent = dir;
    }
    parent.children.push({
      name: parts[parts.length - 1],
      path: parts.join('/'),
      kind: 'file',
      children: [],
      stat,
      lastAt: null,
      fileCount: 1,
    });
  }
  sortTree(root);
  aggregate(root);
  return compactTree(root);
}

/** 1 for an edit just now, fading linearly to 0 an hour later. */
export function heatIntensity(
  lastAt: number | null,
  nowSeconds: number,
  windowSeconds = HEAT_WINDOW_SECONDS,
): number {
  if (lastAt == null) return 0;
  const age = Math.max(0, nowSeconds - lastAt);
  return age >= windowSeconds ? 0 : 1 - age / windowSeconds;
}

export interface VisibleRow {
  node: TreeNode;
  depth: number;
}

/** The rows a tree view shows, depth-first, skipping collapsed folders' children. */
export function visibleRows(root: TreeNode, collapsed: ReadonlySet<string>): VisibleRow[] {
  const rows: VisibleRow[] = [];
  const walk = (node: TreeNode, depth: number) => {
    for (const child of node.children) {
      rows.push({ node: child, depth });
      if (child.kind === 'dir' && !collapsed.has(child.path)) walk(child, depth + 1);
    }
  };
  walk(root, 0);
  return rows;
}

/** Files at or below a node, newest edit first (for a folder's heat strip). */
export function filesUnder(node: TreeNode): FileStat[] {
  const out: FileStat[] = [];
  const walk = (n: TreeNode) => {
    if (n.kind === 'file' && n.stat) out.push(n.stat);
    for (const child of n.children) walk(child);
  };
  walk(node);
  return out.sort((a, b) => (b.last?.timestamp ?? -1) - (a.last?.timestamp ?? -1));
}
