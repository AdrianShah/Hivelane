import { memo, useId, useState } from 'react';
import type { LaneState, PlanUpdate } from '../types';
import { decisionScope, scopeLabel, type FeedbackMap, type Vote } from '../detection';
import type { LaneStatus } from '../dashboard/laneStatus';
import { normPath } from '../dashboard/paths';
import { clockTime, diffStat, relativeTime } from '../dashboard/people';
import type { Touch } from '../dashboard/sessionLog';
import DecisionFeedbackButtons from './DecisionFeedbackButtons';
import DiffLines from './DiffLines';
import { AgentBadge, PersonAvatar, StatusPill } from './PersonBits';

interface LaneProps {
  userId: string;
  state: LaneState;
  inConflict?: boolean;
  highlightFile?: string | null;
  index?: number;
  /**
   * When the agent's daemon left the room (ms epoch), or null while it is
   * connected. An offline lane stays on the board, greyed out, so a teammate
   * dropping off is visible instead of their lane silently vanishing.
   */
  offlineSince?: number | null;
  /** The agent's active declared plan, if any. */
  plan?: PlanUpdate | null;
  /** Branch from the agent's latest activity; empty when unknown. */
  branch?: string;
  /** Live vote tallies by decision_id. */
  feedback?: FeedbackMap;
  /** This dashboard's own votes by decision_id. */
  votes?: Record<string, Vote>;
  onVote?: (decisionId: string, vote: Vote) => void;
  /** editing / idle / blocked / offline (see dashboard/laneStatus.ts). */
  status: LaneStatus;
  /** The person's latest edits, newest first (at most five are shown). */
  recentEdits: Touch[];
  /** Plan files (normalised) this person edited since declaring the plan. */
  editedPlanFiles: ReadonlySet<string>;
  /** Latest activity, relay seconds. */
  lastActivity: number | null;
  nowSeconds: number;
  /** When a file filter is active, diffs of other files are hidden. */
  fileFilter?: (path: string) => boolean;
}

const MAX_DIFFS = 5;

/**
 * Keys that survive a new edit arriving at the top of the list (an index
 * in the key shifted every key, collapsing an open diff and remounting rows).
 * A repeat of the same file and second gets a suffix.
 */
function editKeys(edits: Touch[]): string[] {
  const seen = new Map<string, number>();
  return edits.map((edit) => {
    const base = `${edit.file_path}@${edit.timestamp}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return n === 0 ? base : `${base}~${n}`;
  });
}

/** Memoized: the dashboard re-renders on every relay frame, a lane only when its props change. */
export default memo(Lane);

function Lane({
  userId,
  state,
  inConflict = false,
  highlightFile = null,
  index = 0,
  offlineSince = null,
  plan = null,
  branch = '',
  feedback = {},
  votes = {},
  onVote,
  status,
  recentEdits,
  editedPlanFiles,
  lastActivity,
  nowSeconds,
  fileFilter,
}: LaneProps) {
  const { caption, prompt, intent, decisions, agent, changes, timestamp } = state;
  const activeIntent = intent?.trim() || null;
  const activePrompt = prompt?.trim() || null;
  const baseId = useId();
  const [openEdits, setOpenEdits] = useState<Record<string, boolean>>({});

  // Before the session log has an edit (e.g. a lane restored from catch-up),
  // fall back to the lane's latest changes.
  const edits: Touch[] = (
    recentEdits.length > 0
      ? recentEdits
      : changes.map((c) => ({
          user_id: userId,
          agent,
          file_path: normPath(c.file_path),
          timestamp: timestamp ?? nowSeconds,
          symbols: c.symbols ?? [],
          diff: c.diff,
          caption,
          intent,
        }))
  )
    .filter((edit) => (fileFilter ? fileFilter(edit.file_path) : true))
    .slice(0, MAX_DIFFS);
  const keys = editKeys(edits);

  // "View overlap" highlights a file: its diff opens unless toggled by hand.
  const highlight = highlightFile ? normPath(highlightFile) : null;

  const offline = offlineSince != null;
  const lastSeen = offline
    ? `offline since ${clockTime(offlineSince / 1000)} (${relativeTime(offlineSince / 1000, nowSeconds)})`
    : lastActivity != null
      ? `active ${relativeTime(lastActivity, nowSeconds)}`
      : 'no activity yet';
  const dotClass = offline
    ? 'bg-on-dim'
    : status === 'blocked' || inConflict
      ? 'bg-error'
      : status === 'editing'
        ? 'bg-emerald status-dot-live'
        : 'bg-amber';

  const planFiles = plan?.files ?? [];
  const planDone = planFiles.filter((f) => editedPlanFiles.has(normPath(f))).length;

  return (
    <article
      id={`lane-${userId}`}
      className={`lane-shell rounded-2xl flex flex-col overflow-hidden animate-fade-up ${
        offline ? 'is-offline' : inConflict ? 'in-conflict' : ''
      }`}
      style={{ animationDelay: `${Math.min(index, 8) * 60}ms` }}
      aria-labelledby={`${baseId}-name`}
      aria-describedby={`${baseId}-seen`}
    >
      <header className="lane-header flex items-start gap-3 px-4 sm:px-5 py-3.5">
        <PersonAvatar
          userId={userId}
          dotClass={dotClass}
          state={offline ? 'offline' : status === 'blocked' ? 'blocked' : inConflict ? 'conflict' : status}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h2
              id={`${baseId}-name`}
              className="text-[15px] font-semibold text-on-surface truncate tracking-tight m-0"
            >
              {userId}
            </h2>
            <StatusPill status={status} />
            {inConflict && !offline ? (
              <span className="agent-status-pill is-conflict" title="Overlapping edits right now">
                <span className="material-symbols-outlined" aria-hidden="true">
                  warning
                </span>
                Conflict
              </span>
            ) : null}
          </div>
          <div className="mt-1.5 flex items-center gap-1.5 flex-wrap text-[11px] text-on-dim min-w-0">
            <AgentBadge agent={agent} />
            {branch ? (
              <span className="branch-badge" title={`Branch: ${branch}`}>
                <span className="material-symbols-outlined text-[13px]" aria-hidden="true">
                  call_split
                </span>
                <span className="sr-only">Branch </span>
                <span className="truncate">{branch}</span>
              </span>
            ) : null}
            <span id={`${baseId}-seen`} className={offline ? 'lane-offline-note' : ''}>
              {lastSeen}
            </span>
          </div>
        </div>
      </header>

      <div className="flex flex-col gap-3 p-4 sm:p-5 flex-1 min-w-0">
        <section aria-label="Current intent" className="min-w-0">
          <p className="section-label m-0 mb-1 flex items-center gap-1">
            <span className="material-symbols-outlined text-[14px] text-lavender" aria-hidden="true">
              my_location
            </span>
            Working on
          </p>
          {activeIntent || activePrompt ? (
            <p
              className="m-0 text-[13px] text-on-surface leading-snug line-clamp-3 break-words"
              title={activeIntent ?? activePrompt ?? undefined}
            >
              {activeIntent ?? activePrompt}
            </p>
          ) : (
            <p className="m-0 text-[12.5px] italic text-on-dim">No intent declared yet</p>
          )}
          {caption ? (
            <p className="m-0 mt-1.5 text-[12px] text-on-muted leading-snug flex gap-1.5 min-w-0">
              <span className="material-symbols-outlined text-[14px] text-primary shrink-0" aria-hidden="true">
                bolt
              </span>
              <span className="break-words min-w-0">{caption}</span>
            </p>
          ) : null}
        </section>

        {plan ? (
          <section className="plan-panel rounded-xl p-3 min-w-0" aria-label="Plan checklist">
            <div className="flex items-center gap-1.5 text-[11.5px] min-w-0">
              <span className="material-symbols-outlined text-[14px] text-primary shrink-0" aria-hidden="true">
                checklist
              </span>
              <span className="text-on-muted truncate flex-1" title={plan.summary}>
                {plan.summary || 'Declared plan'}
              </span>
              {planFiles.length > 0 ? (
                <span className="font-mono text-[10.5px] text-on-dim shrink-0 tabular-nums">
                  {planDone}/{planFiles.length} edited
                </span>
              ) : null}
            </div>
            {planFiles.length > 0 ? (
              <ul className="list-none m-0 mt-2 p-0 flex flex-col gap-1">
                {planFiles.map((file) => {
                  const edited = editedPlanFiles.has(normPath(file));
                  return (
                    <li key={file} className="flex items-center gap-2 text-[11.5px] min-w-0">
                      <span className={`plan-check ${edited ? 'is-edited' : ''}`} aria-hidden="true">
                        {edited ? (
                          <span className="material-symbols-outlined text-[12px] leading-none" aria-hidden="true">check</span>
                        ) : null}
                      </span>
                      <code
                        className={`font-mono truncate ${edited ? 'text-on-dim line-through decoration-1' : 'text-on-muted'}`}
                      >
                        {file}
                      </code>
                      <span className="sr-only">{edited ? '(edited)' : '(planned, not edited yet)'}</span>
                    </li>
                  );
                })}
              </ul>
            ) : null}
            {plan.symbols.length > 0 ? (
              <p className="m-0 mt-1.5 text-[10.5px] text-on-dim font-mono truncate">
                symbols: {plan.symbols.join(', ')}
              </p>
            ) : null}
          </section>
        ) : null}

        {decisions.length > 0 ? (
          <ul className="list-none m-0 p-0 flex flex-wrap gap-1.5" aria-label="Recent Hivelane decisions">
            {decisions.map((decision) => {
              const blocked = decision.decision === 'block';
              const others = decision.others.length ? `${decision.others.join(', ')} editing` : null;
              return (
                <li
                  key={`${decision.file_path}-${decision.timestamp}`}
                  className={`decision-chip ${blocked ? 'is-block' : 'is-warn'}`}
                  title={decision.reason || undefined}
                >
                  <span className="material-symbols-outlined text-[13px]" aria-hidden="true">
                    {blocked ? 'block' : 'warning'}
                  </span>
                  <span className="font-semibold">{blocked ? 'blocked' : 'warned'}</span>
                  <span className="text-on-dim" aria-hidden="true">
                    ·
                  </span>
                  <code className="font-mono">{decision.file_path}</code>
                  <span className={`scope-tag scope-${decisionScope(decision)}`} title={`Scope: ${decisionScope(decision)}`}>
                    {scopeLabel(decision)}
                  </span>
                  {others ? <span className="text-on-dim">— {others}</span> : null}
                  {decision.decision_id && onVote ? (
                    <DecisionFeedbackButtons
                      decisionId={decision.decision_id}
                      tally={feedback[decision.decision_id]}
                      voted={votes[decision.decision_id]}
                      onVote={onVote}
                    />
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}

        <section aria-label="Latest diffs" className="min-w-0">
          <p className="section-label m-0 mb-1.5 flex items-center gap-1">
            <span className="material-symbols-outlined text-[14px] text-primary" aria-hidden="true">
              difference
            </span>
            Last {MAX_DIFFS} diffs
          </p>
          {edits.length === 0 ? (
            <p className="text-[12px] text-on-dim m-0">No file changes yet.</p>
          ) : (
            <ul className="list-none m-0 p-0 flex flex-col gap-1.5">
              {edits.map((edit, i) => {
                const key = keys[i];
                const highlighted = highlight === edit.file_path;
                const open = openEdits[key] ?? highlighted;
                const panelId = `${baseId}-diff-${i}`;
                const { added, removed } = diffStat(edit.diff);
                return (
                  <li
                    key={key}
                    className={`diff-shell rounded-lg overflow-hidden ${highlighted ? 'highlight-ring' : ''}`}
                  >
                    <button
                      type="button"
                      className="diff-toggle w-full flex items-center gap-2 px-3 py-2 text-left transition-colors min-w-0"
                      aria-expanded={open}
                      aria-controls={panelId}
                      onClick={() =>
                        setOpenEdits((prev) => {
                          // Only keys still listed: the map no longer grows forever.
                          const next: Record<string, boolean> = {};
                          for (const k of keys) if (k in prev) next[k] = prev[k];
                          next[key] = !open;
                          return next;
                        })
                      }
                    >
                      <span
                        className={`material-symbols-outlined text-[16px] text-on-dim transition-transform duration-150 ${
                          open ? 'rotate-90' : ''
                        }`}
                        aria-hidden="true"
                      >
                        chevron_right
                      </span>
                      <code className="font-mono text-[11.5px] text-primary truncate flex-1 min-w-0">
                        {edit.file_path}
                      </code>
                      {edit.symbols.length > 0 ? (
                        <span
                          className="symbol-list font-mono text-[10.5px] text-lavender truncate max-w-[35%] hidden sm:inline"
                          title={`Symbols: ${edit.symbols.join(', ')}`}
                        >
                          {edit.symbols.join(', ')}
                        </span>
                      ) : null}
                      <span className="font-mono text-[10px] tabular-nums shrink-0">
                        <span className="diff-stat-add">+{added}</span>{' '}
                        <span className="diff-stat-del">−{removed}</span>
                      </span>
                      <span className="text-[10px] text-on-dim shrink-0 tabular-nums">
                        {relativeTime(edit.timestamp, nowSeconds)}
                      </span>
                    </button>
                    {open ? (
                      <div id={panelId}>
                        {edit.symbols.length > 0 ? (
                          <p className="m-0 px-3 pt-2 text-[10.5px] font-mono text-lavender break-words">
                            symbols: {edit.symbols.join(', ')}
                          </p>
                        ) : null}
                        <DiffLines diff={edit.diff} />
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    </article>
  );
}
