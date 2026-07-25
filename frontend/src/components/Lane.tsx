import type { LaneState } from '../types';
import './Lane.css';

interface LaneProps {
  userId: string;
  state: LaneState;
  inConflict?: boolean;
}

export default function Lane({ userId, state, inConflict = false }: LaneProps) {
  const { caption, changes, prompt } = state;
  const activePrompt = prompt?.trim() ? prompt.trim() : null;

  return (
    <article className={`lane${inConflict ? ' lane--conflict' : ''}`}>
      <header className="lane__header">
        <h2 className="lane__username">{userId}</h2>
        {inConflict && <span className="lane__badge">conflict</span>}
      </header>

      <p className="lane__caption">
        {caption ?? 'Waiting for activity…'}
      </p>

      {activePrompt ? (
        <blockquote className="lane__prompt">
          <span className="lane__prompt-label">Active prompt</span>
          <p className="lane__prompt-text">{activePrompt}</p>
        </blockquote>
      ) : null}

      {changes.length === 0 ? (
        <p className="lane__empty">No file changes yet.</p>
      ) : (
        <ul className="lane__files">
          {changes.map((change) => (
            <li key={change.file_path} className="lane__file">
              <details>
                <summary className="lane__file-path">{change.file_path}</summary>
                <pre className="lane__diff">{change.diff || '(empty diff)'}</pre>
              </details>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
