import type { LaneState } from '../types';

interface LaneProps {
  userId: string;
  state: LaneState;
  inConflict?: boolean;
}

export default function Lane({ userId, state, inConflict = false }: LaneProps) {
  const { caption, changes, prompt } = state;
  const activePrompt = prompt?.trim() ? prompt.trim() : null;

  return (
    <article
      className={`glass-panel rounded-2xl p-5 flex flex-col gap-3 min-h-[200px] transition-all ${
        inConflict
          ? 'neon-border-error border-error/40'
          : 'border border-outline-variant/20'
      }`}
    >
      <header className="flex items-center gap-2">
        <div className="w-8 h-8 rounded-lg bg-primary-container/40 border border-primary/20 flex items-center justify-center">
          <span className="material-symbols-outlined text-primary text-lg">smart_toy</span>
        </div>
        <h2 className="font-mono text-sm font-bold text-primary tracking-tight lowercase">
          {userId}
        </h2>
        {inConflict ? (
          <span className="ml-auto px-2.5 py-0.5 rounded-full bg-error/15 text-error text-[9px] font-black uppercase tracking-widest border border-error/30">
            Conflict
          </span>
        ) : (
          <span className="ml-auto w-1.5 h-1.5 rounded-full bg-tertiary status-glow" />
        )}
      </header>

      <p className="text-base font-bold text-on-surface leading-snug">
        {caption ?? 'Waiting for activity…'}
      </p>

      {activePrompt ? (
        <blockquote className="bg-surface-container-lowest/80 border border-primary/15 border-l-[3px] border-l-primary rounded-lg px-3 py-2.5 m-0">
          <span className="block text-[9px] font-black uppercase tracking-[0.15em] text-primary mb-1">
            Active prompt
          </span>
          <p className="m-0 font-mono text-xs text-on-surface-variant whitespace-pre-wrap break-words leading-relaxed">
            {activePrompt}
          </p>
        </blockquote>
      ) : null}

      {changes.length === 0 ? (
        <p className="text-xs text-on-surface-variant/70 m-0">No file changes yet.</p>
      ) : (
        <ul className="list-none m-0 p-0 flex flex-col gap-1.5">
          {changes.map((change) => (
            <li
              key={change.file_path}
              className="rounded-lg overflow-hidden border border-outline-variant/25 bg-surface-container-low/60"
            >
              <details>
                <summary className="cursor-pointer px-3 py-2 font-mono text-xs text-on-surface-variant hover:text-primary hover:bg-primary/5 transition-colors select-none">
                  {change.file_path}
                </summary>
                <pre className="m-0 px-3 py-2.5 border-t border-outline-variant/20 bg-surface-container-lowest text-[11px] font-mono text-secondary leading-relaxed whitespace-pre-wrap break-words max-h-60 overflow-auto custom-scrollbar">
                  {change.diff || '(empty diff)'}
                </pre>
              </details>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
