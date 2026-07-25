import type { ConflictAlert } from '../types';
import './ConflictBanner.css';

interface ConflictBannerProps {
  conflicts: ConflictAlert[];
  onDismiss: (filePath: string) => void;
  onDismissAll: () => void;
}

function conflictKey(conflict: ConflictAlert): string {
  return conflict.file_path;
}

export default function ConflictBanner({
  conflicts,
  onDismiss,
  onDismissAll,
}: ConflictBannerProps) {
  if (conflicts.length === 0) return null;

  return (
    <section className="conflicts" aria-live="polite">
      <header className="conflicts__header">
        <span className="conflicts__icon" aria-hidden="true" />
        <h2 className="conflicts__title">Active conflicts</h2>
        <span className="conflicts__count">{conflicts.length}</span>
        <button
          type="button"
          className="conflicts__dismiss-all"
          onClick={onDismissAll}
        >
          Dismiss all
        </button>
      </header>

      <ul className="conflicts__list">
        {conflicts.map((conflict) => (
          <li
            key={conflictKey(conflict)}
            className={`conflicts__item conflicts__item--${conflict.verdict}`}
          >
            <div className="conflicts__item-top">
              <span className={`conflicts__verdict conflicts__verdict--${conflict.verdict}`}>
                {conflict.verdict}
              </span>
              <code className="conflicts__file">{conflict.file_path}</code>
              <button
                type="button"
                className="conflicts__dismiss"
                aria-label={`Dismiss conflict on ${conflict.file_path}`}
                onClick={() => onDismiss(conflict.file_path)}
              >
                Dismiss
              </button>
            </div>

            <div className="conflicts__users">
              {conflict.users.map((user) => (
                <span key={user} className="conflicts__user">
                  {user}
                </span>
              ))}
            </div>

            <p className="conflicts__explanation">{conflict.explanation}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
