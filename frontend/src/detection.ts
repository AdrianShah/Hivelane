import type { DecisionFeedbackUpdate, DecisionScope, EditDecision } from './types';

/** Short, human label for the granularity a decision was made at. */
export const SCOPE_LABEL: Record<DecisionScope, string> = {
  symbol: 'same function',
  lines: 'nearby lines',
  file: 'same file',
  plan: 'planned',
  branch: 'other branch',
  intent: 'intent',
};

export const SCOPE_ORDER: DecisionScope[] = [
  'symbol',
  'lines',
  'file',
  'plan',
  'branch',
  'intent',
];

/** The relay's older decisions carry no scope: they were file-level. */
export function decisionScope(decision: EditDecision): DecisionScope {
  return decision.scope ?? 'file';
}

/**
 * Disjoint symbols/lines still warn, but with scope "symbol"/"lines" — the
 * label then reads as "different function" rather than "same function".
 */
export function scopeLabel(decision: EditDecision): string {
  const scope = decisionScope(decision);
  if (decision.decision === 'warn' && scope === 'symbol') return 'different function';
  if (decision.decision === 'warn' && scope === 'lines') return 'distant lines';
  return SCOPE_LABEL[scope];
}

export type Vote = 'useful' | 'noise';

export interface FeedbackTally {
  useful_count: number;
  noise_count: number;
}

export type FeedbackMap = Record<string, FeedbackTally>;

export function applyFeedbackUpdate(
  prev: FeedbackMap,
  update: DecisionFeedbackUpdate,
): FeedbackMap {
  const prior = prev[update.decision_id];
  if (
    prior &&
    prior.useful_count === update.useful_count &&
    prior.noise_count === update.noise_count
  ) {
    return prev;
  }
  return {
    ...prev,
    [update.decision_id]: {
      useful_count: update.useful_count,
      noise_count: update.noise_count,
    },
  };
}

export interface ScopeQuality {
  scope: DecisionScope;
  decisions: number;
  useful: number;
  noise: number;
}

/** Per-scope useful/noise totals over the decisions seen this session. */
export function aggregateQuality(
  scopes: Record<string, DecisionScope>,
  feedback: FeedbackMap,
): ScopeQuality[] {
  const rows = new Map<DecisionScope, ScopeQuality>(
    SCOPE_ORDER.map((scope) => [scope, { scope, decisions: 0, useful: 0, noise: 0 }]),
  );
  for (const [id, scope] of Object.entries(scopes)) {
    const row = rows.get(scope);
    if (!row) continue;
    row.decisions += 1;
    const tally = feedback[id];
    if (tally) {
      row.useful += tally.useful_count;
      row.noise += tally.noise_count;
    }
  }
  return [...rows.values()];
}
