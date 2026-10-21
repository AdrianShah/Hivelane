import { parseDiff, type DiffLineKind } from '../dashboard/people';

const KIND_CLASS: Record<DiffLineKind, string> = {
  meta: 'diff-meta',
  note: 'diff-meta',
  hunk: 'diff-hunk',
  add: 'diff-add',
  del: 'diff-del',
  context: 'diff-meta',
};

/** A unified diff, line-numbered and coloured (shared by lanes and the timeline). */
export default function DiffLines({ diff, maxHeight = 'max-h-64' }: { diff: string; maxHeight?: string }) {
  if (!diff.trim()) {
    return <div className="px-3 py-2.5 text-[11px] font-mono text-on-dim">(empty diff)</div>;
  }

  const lines = parseDiff(diff);

  return (
    <div className={`py-1.5 ${maxHeight} overflow-auto custom-scrollbar`}>
      {lines.map((line, i) => {
        const cls = `diff-line ${KIND_CLASS[line.kind]}`;
        const marker = line.kind === 'add' ? 'added' : line.kind === 'del' ? 'removed' : '';
        const ln = line.ln == null ? '' : String(line.ln);

        return (
          <div key={i} className={cls}>
            <span className="ln">{ln}</span>
            <span className="tx">
              {marker ? <span className="sr-only">{marker}: </span> : null}
              {line.text.length === 0 ? ' ' : line.text}
            </span>
          </div>
        );
      })}
    </div>
  );
}
