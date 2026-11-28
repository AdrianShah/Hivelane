import { nestLanes } from './laneOrder';
import { useMemo, useState } from 'react';
import Icon from '../components/Icon';
import { histogram } from '../dashboard/stats';
import { headline } from '../dashboard/headline';
import { clockTime, relativeTime } from '../dashboard/people';
import { formatDuration } from '../dashboard/stats';
import { rttLabel } from '../dashboard/roomLife';
import { EditsChart, Sparkline } from './Charts';
import { LaneRow } from './LaneRow';
import SetupGuide from './SetupGuide';
import { CONN_LABEL, type ActivityItem, type LaneView, type V3Model } from './model';

const RAIL_ITEMS = 14;

/** People active per bucket across the session: the "active agents" trend. */
function activeTrend(model: V3Model, buckets = 24): number[] {
  const start = model.log.startedAt ?? model.nowSeconds - 60;
  const end = Math.max(model.nowSeconds, start + 1);
  const span = (end - start) / buckets;
  const sets = Array.from({ length: buckets }, () => new Set<string>());
  for (const t of model.log.touches) {
    const i = Math.min(buckets - 1, Math.max(0, Math.floor((t.timestamp - start) / span)));
    sets[i].add(t.user_id);
  }
  return sets.map((s) => s.size);
}

function StatCard({
  label,
  value,
  foot,
  trend,
  tone,
  onClick,
}: {
  label: string;
  value: string;
  foot: string;
  trend: number[];
  tone?: 'ink' | 'accent' | 'warn';
  onClick: () => void;
}) {
  return (
    <button type="button" className="v3-stat" onClick={onClick}>
      <span className="v3-label">{label}</span>
      <span className="v3-num">{value}</span>
      <span className="v3-stat__foot">
        <span className="v3-stat__delta">{foot}</span>
        <Sparkline values={trend} tone={tone} />
      </span>
    </button>
  );
}

function railItems(model: V3Model): (ActivityItem & { dot: string })[] {
  const fromLog = model.log.timeline.slice(0, RAIL_ITEMS).map((entry) => ({
    id: entry.key,
    kind: 'release' as const,
    dot: entry.kind === 'block' ? 'is-block' : 'is-warn',
    text:
      entry.kind === 'block'
        ? `${entry.users[0]} was held on ${entry.file_path}`
        : entry.kind === 'warn'
          ? `${entry.users[0]} was warned about ${entry.file_path}`
          : `${entry.users.join(' and ')} overlap on ${entry.file_path}`,
    timestamp: entry.timestamp,
  }));
  const fromRoom = model.activity.map((item) => ({
    ...item,
    dot:
      item.kind === 'join'
        ? 'is-live'
        : item.kind === 'leave'
          ? 'is-idle'
          : item.kind === 'reorder' || item.kind === 'ping'
            ? 'is-wait'
            : 'is-live',
  }));
  return [...fromLog, ...fromRoom].sort((a, b) => b.timestamp - a.timestamp).slice(0, RAIL_ITEMS);
}

export function ActivityRail({ model }: { model: V3Model }) {
  const items = railItems(model);
  return (
    <section className="v3-section" aria-labelledby="activity-title">
      <div className="v3-section__head">
        <p className="v3-title" id="activity-title">
          Activity
        </p>
        <button type="button" className="v3-btn v3-btn--ghost v3-btn--sm" onClick={() => model.actions.navigate('timeline')}>
          Timeline <Icon name="arrow" size={13} />
        </button>
      </div>
      {items.length === 0 ? (
        <p className="v3-label v3-faint">Joins, overlaps and held edits show up here as they happen.</p>
      ) : (
        <ul className="v3-feed">
          {items.map((item) => (
            <li key={item.id}>
              <span className={`v3-dot ${item.dot}`} />
              <div>
                <span>{item.text}</span>
                <time>
                  {clockTime(item.timestamp)} · {relativeTime(item.timestamp, model.nowSeconds)}
                </time>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function PreventedCard({ model }: { model: V3Model }) {
  const { prevention, stats } = model;
  return (
    <section className="v3-section v3-prevented" aria-labelledby="prevented-title">
      <p className="v3-title" id="prevented-title">
        Collisions prevented
      </p>
      <span className="v3-num">{prevention.prevented}</span>
      <p className="v3-label m-0">
        {prevention.prevented === 1 ? 'An agent backed off' : 'Times an agent backed off'} after a warning or a hold
        {stats.sessionSeconds > 0 ? `, over ${formatDuration(stats.sessionSeconds)}` : ''}.
        {prevention.pending > 0 ? ` ${prevention.pending} still deciding.` : ''}
      </p>
      <div className="v3-actions">
        <button
          type="button"
          className="v3-btn v3-btn--secondary v3-btn--sm"
          onClick={model.actions.openShare}
          disabled={stats.edits === 0}
        >
          <Icon name="share" size={14} /> Share card
        </button>
      </div>
    </section>
  );
}

export default function DashboardView({ model }: { model: V3Model }) {
  const { actions } = model;
  const [openLane, setOpenLane] = useState<string | null>(null);
  const line = headline({ online: model.onlineIds.length, conflicts: model.conflicts, held: model.held });
  const lanesById = useMemo(
    () => Object.fromEntries(model.lanes.map((lane) => [lane.id, lane])) as Record<string, LaneView>,
    [model.lanes],
  );
  const heldBy = useMemo(() => new Map(model.held.map((h) => [h.user_id, h])), [model.held]);
  const start = model.log.startedAt ?? model.nowSeconds - 60;
  const end = Math.max(model.nowSeconds, start + 1);
  const collisionTrend = model.stats.collisionHistogram;
  const rtt = rttLabel(model.rttMs);
  const seen = Object.keys(model.log.agents).length;
  const ordered = nestLanes(model.lanes, lanesById);
  const toneDot = { empty: 'is-idle', clear: 'is-live', warn: 'is-warn is-pulse', block: 'is-block is-pulse' }[line.tone];

  return (
    <div className="v3-grid">
      <div className="v3-main v3-enter">
        <section className="v3-section" style={{ paddingTop: 34, paddingBottom: 30 }}>
          <h1 className="v3-headline">
            <span className={`v3-dot v3-headline__tone ${toneDot}`} aria-hidden="true" />
            {line.text}
          </h1>
          <p className="v3-sub">
            Room <span className="v3-mono">{model.roomId}</span> · relay {CONN_LABEL[model.status].toLowerCase()}
            {seen > 0 ? ` · ${seen} ${seen === 1 ? 'person' : 'people'} this session` : ''}
          </p>
          <div className="v3-actions" style={{ marginTop: 18 }}>
            {line.tone === 'block' || line.tone === 'warn' ? (
              <button type="button" className="v3-btn v3-btn--primary" onClick={() => actions.navigate('conflicts')}>
                Review {line.tone === 'block' ? 'held edits' : 'overlaps'}
              </button>
            ) : (
              <button type="button" className="v3-btn v3-btn--primary" onClick={actions.openInvite}>
                <Icon name="invite" size={15} /> Invite teammates
              </button>
            )}
            <button type="button" className="v3-btn v3-btn--secondary" onClick={actions.copyLink}>
              <Icon name="link" size={15} /> Copy room link
            </button>
          </div>
        </section>

        <div className="v3-stats">
          <StatCard
            label="Active agents"
            value={String(model.onlineIds.length).padStart(2, '0')}
            foot={seen > model.onlineIds.length ? `${seen} seen this session` : 'all of the session'}
            trend={activeTrend(model)}
            onClick={() => actions.navigate('agents')}
          />
          <StatCard
            label="Total diffs"
            value={String(model.totalDiffs).padStart(2, '0')}
            foot={`${model.stats.edits} edits · ${model.stats.filesTouched} files`}
            trend={histogram(
              model.log.touches.map((t) => t.timestamp),
              start,
              end,
              24,
            )}
            tone="accent"
            onClick={() => actions.navigate('agents')}
          />
          <StatCard
            label="Active conflicts"
            value={String(model.conflicts.length + model.held.length).padStart(2, '0')}
            foot={`${model.stats.blocks} held · ${model.stats.warns} warned`}
            trend={collisionTrend}
            tone="warn"
            onClick={() => actions.navigate('conflicts')}
          />
          <StatCard
            label="Relay"
            value={rtt ? rtt.replace(' ms', '') : '--'}
            foot={rtt ? `ms · ${CONN_LABEL[model.status]}` : CONN_LABEL[model.status]}
            trend={model.rttHistory}
            onClick={() => actions.navigate('settings')}
          />
        </div>

        <section className="v3-section">
          <EditsChart
            log={model.log}
            nowSeconds={model.nowSeconds}
            onDot={(dot) => actions.navigate(dot.kind === 'conflict' ? 'conflicts' : 'timeline')}
          />
        </section>

        {model.lanes.length === 0 ? (
          <SetupGuide
            roomId={model.roomId}
            connected={model.status === 'connected'}
            anyAgent={false}
            e2e={model.settings?.e2e ?? false}
            actions={actions}
            shareUrl={model.shareUrl}
          />
        ) : (
          <section className="v3-section" aria-labelledby="lanes-title">
            <div className="v3-section__head">
              <p className="v3-title" id="lanes-title">
                Lanes
              </p>
              <span className="v3-label">last 30 min</span>
            </div>
            <ul className="v3-lanes">
              {ordered.map(({ lane, nested }) => (
                <LaneRow
                  key={lane.id}
                  nested={nested}
                  lane={lane}
                  log={model.log}
                  conflicts={model.conflicts}
                  lanes={lanesById}
                  held={heldBy.get(lane.id)}
                  open={openLane === lane.id}
                  onToggle={(id) => setOpenLane((cur) => (cur === id ? null : id))}
                  isOwner={model.isOwner}
                  nowSeconds={model.nowSeconds}
                  actions={actions}
                />
              ))}
            </ul>
          </section>
        )}
      </div>
      <aside className="v3-rail" aria-label="Room activity">
        <ActivityRail model={model} />
        <PreventedCard model={model} />
      </aside>
    </div>
  );
}
