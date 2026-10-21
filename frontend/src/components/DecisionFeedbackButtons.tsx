import type { FeedbackTally, Vote } from '../detection';

interface Props {
  decisionId: string;
  tally?: FeedbackTally;
  voted?: Vote;
  onVote: (decisionId: string, vote: Vote) => void;
}

/** Useful / noise vote pair for one warn/block decision (one vote each). */
export default function DecisionFeedbackButtons({ decisionId, tally, voted, onVote }: Props) {
  const disabled = voted != null;
  const useful = tally?.useful_count ?? 0;
  const noise = tally?.noise_count ?? 0;
  return (
    <span className="feedback-pair" role="group" aria-label="Rate this decision">
      <button
        type="button"
        className={`feedback-btn ${voted === 'useful' ? 'is-chosen' : ''}`}
        disabled={disabled}
        aria-pressed={voted === 'useful'}
        aria-label={`Mark useful (${useful} so far)`}
        title={disabled ? 'You already rated this decision' : 'This warning was useful'}
        onClick={() => onVote(decisionId, 'useful')}
      >
        <span className="material-symbols-outlined text-[13px]" aria-hidden="true">
          thumb_up
        </span>
        <span>useful</span>
        <span className="tabular-nums">{useful}</span>
      </button>
      <button
        type="button"
        className={`feedback-btn ${voted === 'noise' ? 'is-chosen' : ''}`}
        disabled={disabled}
        aria-pressed={voted === 'noise'}
        aria-label={`Mark noise (${noise} so far)`}
        title={disabled ? 'You already rated this decision' : 'This warning was noise'}
        onClick={() => onVote(decisionId, 'noise')}
      >
        <span className="material-symbols-outlined text-[13px]" aria-hidden="true">
          thumb_down
        </span>
        <span>noise</span>
        <span className="tabular-nums">{noise}</span>
      </button>
    </span>
  );
}
