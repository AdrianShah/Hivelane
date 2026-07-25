import type { LaneState } from '../types';
import './Lane.css';

interface LaneProps {
  userId: string;
  state: LaneState;
}

export default function Lane({ userId, state }: LaneProps) {
  const { caption, changes } = state;

  return (
    <article className="lane">
      <header className="lane__header">
        <h2 className="lane__username">{userId}</h2>
      </header>

      <p className="lane__caption">
        {caption ?? 'Waiting for activity…'}
      </p>

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
