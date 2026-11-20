import { memo } from 'react';
import Bee from '../components/Bee';
import Icon from '../components/Icon';
import { laneBuckets, type LineRange } from '../dashboard/charts';
import { LANE_STATE_LABEL } from '../dashboard/laneState';
import { diffFor, type SessionLog } from '../dashboard/sessionLog';
import type { ConflictAlert } from '../types';
import { agentLabel, relativeTime } from '../dashboard/people';
import { LaneBars, OverlapBar } from './Charts';
import type { HeldEdit, LaneView, V3Actions } from './model';

function rangesFor(lane: LaneView | undefined, file: string, log: SessionLog, now: number): LineRange[] {
  if (!lane) return [];
  const change = lane.changes.find((c) => c.file_path === file);
  return (change?.line_ranges ?? diffFor(log, lane.id, file, now)?.line_ranges ?? []) as LineRange[];
}

/** Who this lane overlaps with on its current file, if anyone (a live overlap alert first). */
function overlapPartner(
  lane: LaneView,
  lanes: Record<string, LaneView>,
  file: string | null,
  conflicts: ConflictAlert[],
): LaneView | undefined {
  if (!file) return undefined;
  const alert = conflicts.find((c) => c.file_path === file && c.users.includes(lane.id));
  const named = alert?.users.find((u) => u !== lane.id);
  if (named && lanes[named]) return lanes[named];
  return Object.values(lanes).find(
    (other) => other.id !== lane.id && other.offlineSince == null && other.changes.some((c) => c.file_path === file),
  );
}

interface LaneRowProps {
  lane: LaneView;
  log: SessionLog;
  conflicts: ConflictAlert[];
  lanes: Record<string, LaneView>;
  held: HeldEdit | undefined;
  open: boolean;
  onToggle: (id: string) => void;
  isOwner: boolean;
  nowSeconds: number;
  actions: V3Actions;
}

/**
 * One lane: bee, name, app, current file, the last half hour as 24 bars,
 * a status dot and a chevron. Opening it shows the intent, where its edit
 * overlaps a teammate's (line ranges) and what can be done about it.
 */
function LaneRowImpl({ lane, log, conflicts, lanes, held, open, onToggle, isOwner, nowSeconds, actions }: LaneRowProps) {
  const alert = conflicts.find((c) => c.users.includes(lane.id));
  const file = held?.file_path ?? alert?.file_path ?? lane.currentFile;
  const partner = overlapPartner(lane, lanes, file, conflicts);
  const counts = laneBuckets(lane.editTimes, nowSeconds);
  const detailId = `lane-detail-${lane.id}`;
  const stateLabel = LANE_STATE_LABEL[lane.state];
  const intent = lane.intent ?? lane.plan?.summary ?? lane.caption;
  const problem = lane.state === 'blocked' || lane.state === 'waiting' || lane.state === 'conflict';

  return (
    <li id={`lane-${lane.id}`}>
      <button
        type="button"
        className="v3-lane-row"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => onToggle(lane.id)}
      >
        <Bee state={lane.state} accent={lane.color} size={32} />
        <span className="v3-lane-row__who">
          <span className="v3-lane-row__name">{lane.id}</span>
          <span className="v3-lane-row__app">
            {agentLabel(lane.agent)}
            {lane.branch ? ` · ${lane.branch}` : ''}
          </span>
        </span>
        <span className="v3-lane-row__file" title={file ?? undefined}>
          {file ? <bdi>{file}</bdi> : <span className="v3-faint">no edits yet</span>}
        </span>
        <LaneBars counts={counts} label={`${lane.editTimes.length} edits this session`} />
        <span className="inline-flex items-center gap-2 text-[12px] v3-muted" title={stateLabel}>
          <span className={`v3-dot is-${lane.state}${problem ? ' is-pulse' : ''}`} />
          <span className="hidden lg:inline">{stateLabel}</span>
        </span>
        <Icon name="chevron" size={14} className="v3-lane-row__chev" />
      </button>

      {open ? (
        <div className="v3-lane-detail" id={detailId}>
          {intent ? (
            <p className="v3-lane-detail__intent">{intent}</p>
          ) : (
            <p className="v3-lane-detail__intent v3-faint">No intent shared yet.</p>
          )}
          <p className="v3-label">
            Last active {relativeTime(lane.derived.lastActivity, nowSeconds)}
            {held ? ` · held: ${held.reason || `waiting on ${held.others.join(', ')}`}` : ''}
          </p>
          {partner && file ? (
            <OverlapBar
              mine={rangesFor(lane, file, log, nowSeconds)}
              theirs={rangesFor(partner, file, log, nowSeconds)}
              mineLabel={lane.id}
              theirsLabel={partner.id}
            />
          ) : null}
          <div className="v3-actions">
            {held && isOwner ? (
              <button type="button" className="v3-btn v3-btn--primary v3-btn--sm" onClick={() => actions.releaseHold(held)}>
                <Icon name="release" size={14} /> Release hold
              </button>
            ) : null}
            {partner && file && isOwner ? (
              <button
                type="button"
                className="v3-btn v3-btn--secondary v3-btn--sm"
                onClick={() => actions.goFirst(file, lane.id)}
              >
                <Icon name="first" size={14} /> Let {lane.id} go first
              </button>
            ) : null}
            {isOwner && lane.offlineSince == null ? (
              <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm" onClick={() => actions.ping(lane.id)}>
                <Icon name="ping" size={14} /> Ping
              </button>
            ) : null}
            <button type="button" className="v3-btn v3-btn--ghost v3-btn--sm" onClick={() => actions.showLane(lane.id)}>
              Open in feed <Icon name="arrow" size={14} />
            </button>
          </div>
          {!isOwner && (held || partner) ? (
            <p className="v3-label v3-faint">
              Only the room owner can release holds or pick who goes first (the browser that created the room).
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export const LaneRow = memo(LaneRowImpl);
