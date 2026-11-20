import type { ReactNode } from 'react';
import { Mascot } from '../brand/BrandArt';
import Bee from '../components/Bee';
import DetectionQuality from '../components/DetectionQuality';
import DiffLines from '../components/DiffLines';
import Icon from '../components/Icon';
import RoomAccessPanel from '../components/RoomAccessPanel';
import ThemeToggle from '../components/ThemeToggle';
import { aggregateQuality } from '../detection';
import type { LineRange } from '../dashboard/charts';
import { conflictKey } from '../dashboard/laneReducer';
import { LANE_STATE_LABEL } from '../dashboard/laneState';
import { agentLabel, clockTime, relativeTime } from '../dashboard/people';
import { expiryLabel, rttLabel } from '../dashboard/roomLife';
import { diffFor } from '../dashboard/sessionLog';
import { formatDuration } from '../dashboard/stats';
import { DEFAULT_ROOM_SETTINGS, describeSettingsSource, describeWindows } from '../roomSettings';
import type { ConflictAlert, DecisionScope, JoinRequest } from '../types';
import { OverlapBar } from './Charts';
import { CONN_LABEL, type HeldEdit, type LaneView, type V3Model } from './model';

/** A view's opening: a sentence, a line under it, and optional actions. */
export function PageHead({ title, sub, children }: { title: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <section className="v3-section" style={{ paddingTop: 30 }}>
      <h1 className="v3-headline">{title}</h1>
      {sub ? <p className="v3-sub">{sub}</p> : null}
      {children ? (
        <div className="v3-actions" style={{ marginTop: 16 }}>
          {children}
        </div>
      ) : null}
    </section>
  );
}

// --- conflicts: the resolve flow --------------------------------------------------

interface Dispute {
  key: string;
  file_path: string;
  users: string[];
  timestamp: number;
  verdict: string;
  explanation: string;
  held: HeldEdit | null;
  alert: ConflictAlert | null;
}

function disputes(model: V3Model): Dispute[] {
  const out = new Map<string, Dispute>();
  for (const held of model.held) {
    const users = [held.user_id, ...held.others.filter((u) => u !== held.user_id)];
    out.set(held.file_path, {
      key: `held:${held.decision_id}`,
      file_path: held.file_path,
      users,
      timestamp: held.timestamp,
      verdict: 'held',
      explanation: held.reason,
      held,
      alert: null,
    });
  }
  for (const alert of model.conflicts) {
    const existing = out.get(alert.file_path);
    if (existing) {
      existing.alert = alert;
      continue;
    }
    out.set(alert.file_path, {
      key: conflictKey(alert),
      file_path: alert.file_path,
      users: alert.users,
      timestamp: alert.timestamp,
      verdict: alert.verdict,
      explanation: alert.explanation,
      held: null,
      alert,
    });
  }
  return [...out.values()].sort((a, b) => (b.held ? 1 : 0) - (a.held ? 1 : 0) || b.timestamp - a.timestamp);
}

function Side({ model, lane, user, file, at }: { model: V3Model; lane: LaneView | undefined; user: string; file: string; at: number }) {
  const touch = diffFor(model.log, user, file, at);
  return (
    <div className="v3-side">
      <div className="flex items-center gap-3">
        <Bee state={lane?.state ?? 'offline'} accent={lane?.color ?? 'var(--v3-idle)'} size={30} />
        <div className="min-w-0">
          <p className="m-0 text-[13.5px] font-medium truncate">{user}</p>
          <p className="m-0 v3-label">
            {lane ? `${agentLabel(lane.agent)} · ${LANE_STATE_LABEL[lane.state]}` : 'left the room'}
          </p>
        </div>
      </div>
      <p className="m-0 text-[13px] leading-relaxed">
        {touch?.intent ?? lane?.intent ?? <span className="v3-faint">No intent shared.</span>}
      </p>
      {touch ? (
        <div className="diff-shell">
          <DiffLines diff={touch.diff} maxHeight="max-h-72" />
        </div>
      ) : (
        <p className="m-0 v3-label v3-faint">No diff yet: the check came before the edit.</p>
      )}
    </div>
  );
}

export function ConflictsView({ model }: { model: V3Model }) {
  const { actions } = model;
  const list = disputes(model);
  const lanes = Object.fromEntries(model.lanes.map((l) => [l.id, l]));
  if (list.length === 0) {
    return (
      <div className="v3-grid v3-grid--single">
        <div className="v3-main v3-enter">
          <section className="v3-empty">
            <Mascot pose="clear" size={96} />
            <h1 className="v3-headline" style={{ textAlign: 'center' }}>
              All clear.
            </h1>
            <p className="v3-sub" style={{ textAlign: 'center' }}>
              Nobody is in anyone else&apos;s file. Overlaps and held edits show up here, side by side, with what to do
              about them.
            </p>
            <button type="button" className="v3-btn v3-btn--secondary" onClick={() => actions.navigate('history')}>
              <Icon name="history" size={15} /> Conflict history
            </button>
          </section>
        </div>
      </div>
    );
  }
  return (
    <div className="v3-grid v3-grid--single">
      <div className="v3-main v3-enter">
        <PageHead
          title={
            <>
              <span className="v3-dot v3-headline__tone is-warn is-pulse" aria-hidden="true" />
              {list.length === 1 ? 'One file needs a decision.' : `${list.length} files need a decision.`}
            </>
          }
          sub={
            model.isOwner
              ? 'Release a hold, pick who goes first, or ping an agent. The relay applies it at once and tells the agents.'
              : 'Only the room owner can resolve these (the browser that created the room). You can still see both sides.'
          }
        />
        {list.map((d) => {
          const [a, b] = d.users;
          // The edit each side made to this file (the session log keeps it
          // even after the lane has moved on to another file).
          const ranges = (user: string) =>
            ((diffFor(model.log, user, d.file_path, d.timestamp)?.line_ranges ??
              lanes[user]?.changes.find((c) => c.file_path === d.file_path)?.line_ranges ??
              []) as LineRange[]);
          return (
            <section key={d.key} className="v3-section v3-conflict" aria-label={`Conflict on ${d.file_path}`}>
              <div className="v3-section__head">
                <div className="min-w-0">
                  <p className="v3-title v3-mono truncate">{d.file_path}</p>
                  <p className="v3-label" style={{ marginTop: 4 }}>
                    <span className={`v3-dot ${d.held ? 'is-block' : 'is-warn'}`} aria-hidden="true" />{' '}
                    {d.held
                      ? `${d.held.user_id}'s edit is held${d.held.queue_position ? ` (#${d.held.queue_position} in line)` : ''}`
                      : d.verdict === 'overlapping'
                        ? 'Overlapping edits'
                        : 'Related edits'}{' '}
                    · {relativeTime(d.timestamp, model.nowSeconds)}
                  </p>
                </div>
                {d.alert ? (
                  <button type="button" className="v3-icon-btn" aria-label="Hide this alert" onClick={() => actions.dismissConflict(d.alert!)}>
                    <Icon name="close" size={15} />
                  </button>
                ) : null}
              </div>
              {d.explanation ? <p className="v3-sub" style={{ marginTop: -4, marginBottom: 14 }}>{d.explanation}</p> : null}
              <div className="v3-sides">
                {a ? <Side model={model} lane={lanes[a]} user={a} file={d.file_path} at={d.timestamp} /> : null}
                {b ? <Side model={model} lane={lanes[b]} user={b} file={d.file_path} at={d.timestamp} /> : null}
              </div>
              {a && b ? (
                <div style={{ marginTop: 14 }}>
                  <OverlapBar mine={ranges(a)} theirs={ranges(b)} mineLabel={a} theirsLabel={b} />
                </div>
              ) : null}
              <div className="v3-actions" style={{ marginTop: 16 }}>
                {d.held ? (
                  <button
                    type="button"
                    className="v3-btn v3-btn--primary"
                    disabled={!model.isOwner}
                    onClick={() => actions.releaseHold(d.held!)}
                  >
                    <Icon name="release" size={15} /> Release {d.held.user_id}&apos;s hold
                  </button>
                ) : null}
                {d.users.slice(0, 2).map((user) => (
                  <button
                    key={user}
                    type="button"
                    className="v3-btn v3-btn--secondary"
                    disabled={!model.isOwner || !model.onlineIds.includes(user)}
                    onClick={() => actions.goFirst(d.file_path, user)}
                  >
                    <Icon name="first" size={15} /> {user} goes first
                  </button>
                ))}
                {d.users.slice(0, 2).map((user) => (
                  <button
                    key={`ping-${user}`}
                    type="button"
                    className="v3-btn v3-btn--ghost"
                    disabled={!model.isOwner || !model.onlineIds.includes(user)}
                    onClick={() => actions.ping(user, `${d.file_path} is contested; check the Hivelane dashboard`)}
                  >
                    <Icon name="ping" size={15} /> Ping {user}
                  </button>
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

// --- history ---------------------------------------------------------------------

export function HistoryView({ model }: { model: V3Model }) {
  const rows = model.log.timeline;
  const hours = model.settings?.history_hours ?? 0;
  return (
    <div className="v3-grid v3-grid--single">
      <div className="v3-main v3-enter">
        <PageHead
          title={rows.length ? `${rows.length} collisions this session.` : 'No collisions this session.'}
          sub={
            hours > 0
              ? `The relay keeps ${hours} h of history for this room: Replay plays it back.`
              : 'This list covers what this dashboard saw since it opened. The relay keeps no history for this room.'
          }
        >
          {hours > 0 ? (
            <button type="button" className="v3-btn v3-btn--secondary" onClick={() => model.actions.navigate('replay')}>
              <Icon name="replay" size={15} /> Open replay
            </button>
          ) : null}
        </PageHead>
        {rows.length ? (
          <section className="v3-section" style={{ overflowX: 'auto' }}>
            <table className="v3-table" style={{ minWidth: 560 }}>
              <thead>
                <tr>
                  <th>When</th>
                  <th>What</th>
                  <th>Who</th>
                  <th>File</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((entry) => (
                  <tr key={entry.key}>
                    <td className="v3-mono v3-muted" style={{ fontSize: 12 }}>
                      {clockTime(entry.timestamp)}
                    </td>
                    <td>
                      <span className="inline-flex items-center gap-2">
                        <span className={`v3-dot ${entry.kind === 'block' ? 'is-block' : 'is-warn'}`} aria-hidden="true" />
                        {entry.kind === 'block' ? 'Held' : entry.kind === 'warn' ? 'Warned' : 'Overlap'}
                        {entry.repeats > 1 ? <span className="v3-faint">×{entry.repeats}</span> : null}
                      </span>
                    </td>
                    <td>{entry.users.join(' · ')}</td>
                    <td className="v3-mono" style={{ fontSize: 12 }}>
                      {entry.file_path}
                    </td>
                    <td className="v3-muted">{model.prevention.outcomes[entry.key] ?? (entry.kind === 'conflict' ? '' : 'unknown')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ) : null}
      </div>
    </div>
  );
}

// --- analytics ----------------------------------------------------------------------

export function AnalyticsView({ model, scopes }: { model: V3Model; scopes: Record<string, DecisionScope> }) {
  const { stats } = model;
  const figures: [string, string][] = [
    ['Agents online', String(model.onlineIds.length)],
    ['People this session', String(stats.people)],
    ['Edits', String(stats.edits)],
    ['Files touched', String(stats.filesTouched)],
    ['Held edits', String(stats.blocks)],
    ['Warnings', String(stats.warns)],
    ['Overlaps', String(stats.conflicts)],
    ['Prevented', String(stats.prevented)],
  ];
  return (
    <div className="v3-grid v3-grid--single">
      <div className="v3-main v3-enter">
        <PageHead
          title={`${stats.edits} edits across ${stats.filesTouched} files${stats.sessionSeconds ? ` in ${formatDuration(stats.sessionSeconds)}` : ''}.`}
          sub={stats.agentKinds.length ? `Agents seen: ${stats.agentKinds.join(', ')}.` : 'Session metrics for this room.'}
        />
        <div className="v3-stats">
          {figures.map(([label, value]) => (
            <div key={label} className="v3-stat" style={{ cursor: 'default' }}>
              <span className="v3-label">{label}</span>
              <span className="v3-num v3-num--sm">{value}</span>
            </div>
          ))}
        </div>
        <section className="v3-section">
          <DetectionQuality rows={aggregateQuality(scopes, model.feedback)} />
        </section>
      </div>
    </div>
  );
}

// --- settings -------------------------------------------------------------------------

export function SettingsView({
  model,
  joinRequests,
  onSetRequireApproval,
  onDecide,
}: {
  model: V3Model;
  joinRequests: JoinRequest[];
  onSetRequireApproval: (value: boolean) => void;
  onDecide: (requestId: string, approve: boolean) => void;
}) {
  const { actions } = model;
  const expiry = expiryLabel(model.settings?.expires_at, model.nowSeconds);
  const rows: [string, ReactNode][] = [
    ['Room', <span key="room" className="v3-mono">{model.roomId}</span>],
    ['Relay', <span key="relay" className="v3-mono break-all">{model.wsUrl}</span>],
    [
      'Status',
      <span key="status" className="inline-flex items-center gap-2">
        <span className={`v3-dot ${model.status === 'connected' ? 'is-live' : 'is-warn'}`} aria-hidden="true" />
        {CONN_LABEL[model.status]}
        {rttLabel(model.rttMs) ? <span className="v3-faint v3-mono">{rttLabel(model.rttMs)}</span> : null}
      </span>,
    ],
    ['Room code', expiry ? `${expiry} (codes last a week from creation)` : 'Expiry not reported by this relay'],
    [
      'Conflict windows',
      <span key="windows">
        {describeWindows(model.settings ?? DEFAULT_ROOM_SETTINGS)}
        <span className="block v3-label">{describeSettingsSource(model.settings)}</span>
      </span>,
    ],
  ];
  return (
    <div className="v3-grid v3-grid--single">
      <div className="v3-main v3-enter">
        <PageHead title="Settings for this dashboard." sub="How it connects, how it looks, and who may join." />
        <section className="v3-section">
          <table className="v3-table">
            <tbody>
              {rows.map(([label, value]) => (
                <tr key={label}>
                  <td className="v3-muted" style={{ width: 180 }}>
                    {label}
                  </td>
                  <td>{value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="v3-section">
          <p className="v3-title" style={{ marginBottom: 12 }}>
            Appearance
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <ThemeToggle showLabels />
            <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm hidden md:inline-flex" onClick={actions.openShortcuts}>
              <Icon name="keyboard" size={14} /> Keyboard shortcuts <span className="v3-kbd">?</span>
            </button>
          </div>
        </section>
        <section className="v3-section md:hidden">
          <p className="v3-title" style={{ marginBottom: 12 }}>
            More views
          </p>
          <div className="v3-actions">
            {(
              [
                ['tasks', 'Tasks'],
                ['heatmap', 'Heat map'],
                ['history', 'History'],
                ['replay', 'Replay'],
                ['analytics', 'Analytics'],
                ['notifications', 'Notifications'],
                ['invite', 'Invite'],
              ] as const
            ).map(([id, label]) => (
              <button key={id} type="button" className="v3-btn v3-btn--secondary v3-btn--sm" onClick={() => actions.navigate(id)}>
                {label}
              </button>
            ))}
          </div>
        </section>
        <section className="v3-section">
          <RoomAccessPanel
            isOwner={model.isOwner}
            requireApproval={model.requireApproval}
            requests={joinRequests}
            connected={model.status === 'connected'}
            onSetRequireApproval={onSetRequireApproval}
            onDecide={onDecide}
          />
        </section>
        <section className="v3-section">
          <div className="v3-actions">
            <button type="button" className="v3-btn v3-btn--secondary" onClick={actions.copyLink}>
              <Icon name="link" size={15} /> Copy room link
            </button>
            <button type="button" className="v3-btn v3-btn--secondary" onClick={actions.newRoom}>
              <Icon name="plus" size={15} /> New room
            </button>
            <button type="button" className="v3-btn v3-btn--ghost" onClick={actions.leaveRoom}>
              <Icon name="logout" size={15} /> Leave room
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}
