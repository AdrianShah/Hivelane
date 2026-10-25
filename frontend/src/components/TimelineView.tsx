import { useId, useMemo, useState, type ReactNode } from 'react';
import { scopeLabel, type FeedbackMap, type Vote } from '../detection';
import { isFiltering, matchesTimelineEntry, type DashboardFilters } from '../dashboard/filters';
import {
  OUTCOME_LABEL,
  type PreventionOutcome,
  type PreventionSummary,
} from '../dashboard/prevented';
import { clockTime, diffStat, relativeTime } from '../dashboard/people';
import { MAX_TIMELINE, diffFor, type SessionLog, type TimelineEntry } from '../dashboard/sessionLog';
import DecisionFeedbackButtons from './DecisionFeedbackButtons';
import DiffLines from './DiffLines';
import { AgentBadge, PersonName } from './PersonBits';

/**
 * Every warn, block and conflict this session, newest first, each expandable
 * into both sides' intents and diffs. The log keeps the latest 500 entries;
 * the list renders a page at a time so a long session stays responsive.
 */

interface TimelineViewProps {
  log: SessionLog;
  filters: DashboardFilters;
  prevention: PreventionSummary;
  feedback: FeedbackMap;
  votes: Record<string, Vote>;
  onVote: (decisionId: string, vote: Vote) => void;
  nowSeconds: number;
  /** The shared filter bar. */
  filterBar: ReactNode;
  /** The prevented-collisions counter card. */
  counter: ReactNode;
}

const PAGE = 60;

const KIND_META = {
  block: { label: 'Blocked', icon: 'block' },
  warn: { label: 'Warned', icon: 'warning' },
  conflict: { label: 'Conflict', icon: 'call_merge' },
} as const;

const OUTCOME_ICON: Record<PreventionOutcome, string> = {
  prevented: 'verified_user',
  'edited-anyway': 'edit_note',
  pending: 'hourglass_top',
  unknown: 'help',
};

const OUTCOME_HINT: Record<PreventionOutcome, string> = {
  prevented: 'The agent moved on to a different file within 2 minutes',
  'edited-anyway': 'The agent edited the same file next',
  pending: 'Waiting up to 2 minutes for the agent’s next edit',
  unknown: 'No edit by the agent within 2 minutes',
};

function Summary({ entry }: { entry: TimelineEntry }) {
  const [first, ...rest] = entry.users;
  const file = <code className="font-mono text-[12px] text-primary break-all">{entry.file_path}</code>;
  if (entry.decision) {
    return (
      <>
        <PersonName userId={first} className="font-semibold text-on-surface max-w-[12rem]" />{' '}
        <span className="text-on-muted">
          {entry.kind === 'block' ? 'was blocked from editing' : 'was warned before editing'}
        </span>{' '}
        {file}
        {rest.length > 0 ? (
          <>
            {' '}
            <span className="text-on-muted">while</span>{' '}
            {rest.map((u, i) => (
              <span key={u}>
                {i > 0 ? <span className="text-on-muted">, </span> : null}
                <PersonName userId={u} className="font-semibold text-on-surface max-w-[10rem]" />
              </span>
            ))}{' '}
            <span className="text-on-muted">{rest.length > 1 ? 'were' : 'was'} on it</span>
          </>
        ) : null}
      </>
    );
  }
  return (
    <>
      {entry.users.map((u, i) => (
        <span key={u}>
          {i > 0 ? <span className="text-on-muted">{i === entry.users.length - 1 ? ' and ' : ', '}</span> : null}
          <PersonName userId={u} className="font-semibold text-on-surface max-w-[10rem]" />
        </span>
      ))}{' '}
      <span className="text-on-muted">both changed</span> {file}
    </>
  );
}

function Side({
  log,
  entry,
  userId,
  role,
}: {
  log: SessionLog;
  entry: TimelineEntry;
  userId: string;
  role: string;
}) {
  const touch = diffFor(log, userId, entry.file_path, entry.lastSeen);
  const intent = entry.intents[userId] ?? log.intents[userId]?.prompt ?? null;
  const stat = touch ? diffStat(touch.diff) : null;
  return (
    <section className="surface-panel rounded-xl overflow-hidden min-w-0" aria-label={`${userId}, ${role}`}>
      <header className="flex items-center gap-2 flex-wrap px-3 py-2 border-b border-outline-soft/60">
        <PersonName userId={userId} className="text-[12.5px] font-semibold text-on-surface" />
        <AgentBadge agent={log.agents[userId]} />
        <span className="section-label ml-auto">{role}</span>
      </header>
      <div className="px-3 py-2.5">
        <p className="section-label m-0 mb-1">Intent at the time</p>
        <p className="m-0 text-[12.5px] text-on-surface leading-snug break-words">
          {intent ?? <span className="italic text-on-dim">No intent declared</span>}
        </p>
      </div>
      <div className="border-t border-outline-soft/60">
        {touch ? (
          <>
            <p className="m-0 px-3 pt-2 text-[10.5px] text-on-dim flex flex-wrap gap-x-2">
              <span>
                {touch.timestamp > entry.lastSeen ? 'Edit right after this' : 'Latest edit before this'},{' '}
                {clockTime(touch.timestamp)}
              </span>
              {stat ? (
                <span className="font-mono tabular-nums">
                  <span className="diff-stat-add">+{stat.added}</span>{' '}
                  <span className="diff-stat-del">−{stat.removed}</span>
                </span>
              ) : null}
              {touch.symbols.length > 0 ? (
                <span className="font-mono text-lavender break-all">{touch.symbols.join(', ')}</span>
              ) : null}
            </p>
            <div className="diff-shell m-2 rounded-lg overflow-hidden">
              <DiffLines diff={touch.diff} maxHeight="max-h-72" />
            </div>
          </>
        ) : (
          <p className="m-0 px-3 py-2.5 text-[12px] text-on-dim">
            No diff of this file from {userId} in this session.
          </p>
        )}
      </div>
    </section>
  );
}

function TimelineItem({
  entry,
  log,
  outcome,
  feedback,
  votes,
  onVote,
  nowSeconds,
}: {
  entry: TimelineEntry;
  log: SessionLog;
  outcome: PreventionOutcome | undefined;
  feedback: FeedbackMap;
  votes: Record<string, Vote>;
  onVote: (decisionId: string, vote: Vote) => void;
  nowSeconds: number;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const meta = KIND_META[entry.kind];
  const decision = entry.decision;
  const iso = new Date(entry.timestamp * 1000).toISOString();
  const roles = (index: number) =>
    decision ? (index === 0 ? 'requested the edit' : 'already editing') : 'editing';

  return (
    <li className="timeline-item pb-3">
      <span className={`timeline-dot is-${entry.kind}`} aria-hidden="true" />
      <article className="timeline-card rounded-xl min-w-0">
        <div className="flex flex-wrap items-center gap-1.5 px-3.5 pt-3">
          <span className={`kind-badge is-${entry.kind}`}>
            <span className="material-symbols-outlined" aria-hidden="true">
              {meta.icon}
            </span>
            {meta.label}
          </span>
          {decision ? (
            <span className="scope-tag" title="How close the edits were">
              {scopeLabel(decision)}
            </span>
          ) : entry.conflict ? (
            <span className="scope-tag" title="The relay's verdict">
              {entry.conflict.verdict}
            </span>
          ) : null}
          {outcome ? (
            <span className={`outcome-tag is-${outcome}`} title={OUTCOME_HINT[outcome]}>
              <span className="material-symbols-outlined" aria-hidden="true">
                {OUTCOME_ICON[outcome]}
              </span>
              {OUTCOME_LABEL[outcome]}
            </span>
          ) : null}
          {entry.repeats > 1 ? (
            <span className="text-[10.5px] text-on-dim">×{entry.repeats}</span>
          ) : null}
          <time
            dateTime={iso}
            className="ml-auto text-[11px] text-on-dim tabular-nums"
            title={new Date(entry.timestamp * 1000).toLocaleString()}
          >
            {clockTime(entry.timestamp)} · {relativeTime(entry.timestamp, nowSeconds)}
          </time>
        </div>
        <p className="m-0 px-3.5 mt-2 text-[13px] leading-relaxed break-words">
          <Summary entry={entry} />
        </p>
        {decision?.reason || entry.conflict?.explanation ? (
          <p className="m-0 px-3.5 mt-1 text-[12px] text-on-muted leading-snug break-words">
            {decision?.reason || entry.conflict?.explanation}
          </p>
        ) : null}
        {decision?.suggestion ? (
          <p className="m-0 px-3.5 mt-1 text-[12px] text-on-muted leading-snug break-words flex gap-1.5">
            <span className="material-symbols-outlined text-[15px] text-primary shrink-0" aria-hidden="true">
              lightbulb
            </span>
            <span>{decision.suggestion}</span>
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 px-3.5 pt-2 pb-3">
          <button
            type="button"
            className="filter-chip"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((v) => !v)}
          >
            <span
              className={`material-symbols-outlined text-[15px] transition-transform ${open ? 'rotate-90' : ''}`}
              aria-hidden="true"
            >
              chevron_right
            </span>
            {open ? 'Hide' : 'Compare'} intents and diffs
          </button>
          {decision?.decision_id ? (
            <DecisionFeedbackButtons
              decisionId={decision.decision_id}
              tally={feedback[decision.decision_id]}
              voted={votes[decision.decision_id]}
              onVote={onVote}
            />
          ) : null}
        </div>
        {open ? (
          <div id={panelId} className="timeline-compare px-3.5 pb-3.5">
            {entry.users.slice(0, 4).map((user, i) => (
              <Side key={user} log={log} entry={entry} userId={user} role={roles(i)} />
            ))}
          </div>
        ) : null}
      </article>
    </li>
  );
}

export default function TimelineView({
  log,
  filters,
  prevention,
  feedback,
  votes,
  onVote,
  nowSeconds,
  filterBar,
  counter,
}: TimelineViewProps) {
  const [limit, setLimit] = useState(PAGE);
  const entries = useMemo(
    () => log.timeline.filter((e) => matchesTimelineEntry(e, filters, log.agents)),
    [log.timeline, log.agents, filters],
  );
  const shown = entries.slice(0, limit);
  const filtering = isFiltering(filters);

  return (
    <div className="view-page animate-fade-up">
      <div>
        <h1 className="view-title m-0">Collision timeline</h1>
        <p className="view-sub">
          Every warning, block and overlap this session, newest first. Expand one to compare both
          sides.
        </p>
        {counter}
        {filterBar}
        {/* Live only while filtering (a result count); new entries alone must not chatter. */}
        <p className="text-[11.5px] text-on-dim m-0 mb-3" aria-live={filtering ? 'polite' : 'off'}>
          {filtering
            ? `${entries.length} of ${log.timeline.length} entries match`
            : `${log.timeline.length} entries`}
          {log.timeline.length >= MAX_TIMELINE ? ` · keeping the latest ${MAX_TIMELINE}` : ''}
        </p>
        {shown.length === 0 ? (
          <div className="lane-shell rounded-2xl p-6">
            <p className="text-[14px] text-on-surface font-medium m-0">
              {filtering ? 'Nothing matches these filters' : 'No collisions yet'}
            </p>
            <p className="text-[13px] text-on-muted mt-2 mb-0">
              {filtering
                ? 'Clear a filter or widen the search.'
                : 'When Hivelane warns or blocks an edit, or two agents change the same file, it shows up here.'}
            </p>
          </div>
        ) : (
          <ol className="timeline-list max-w-[1100px]" aria-label="Collisions, newest first">
            {shown.map((entry) => (
              <TimelineItem
                key={entry.key}
                entry={entry}
                log={log}
                outcome={prevention.outcomes[entry.key]}
                feedback={feedback}
                votes={votes}
                onVote={onVote}
                nowSeconds={nowSeconds}
              />
            ))}
          </ol>
        )}
        {entries.length > shown.length ? (
          <button
            type="button"
            className="filter-chip mt-2"
            onClick={() => setLimit((n) => n + PAGE)}
          >
            Show {Math.min(PAGE, entries.length - shown.length)} more ({entries.length - shown.length} left)
          </button>
        ) : null}
      </div>
    </div>
  );
}
