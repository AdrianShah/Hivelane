/** A unified diff, line-numbered and coloured (shared by lanes and the timeline). */
export default function DiffLines({ diff, maxHeight = 'max-h-64' }: { diff: string; maxHeight?: string }) {
  if (!diff.trim()) {
    return <div className="px-3 py-2.5 text-[11px] font-mono text-on-dim">(empty diff)</div>;
  }

  const lines = diff.replace(/\r\n/g, '\n').split('\n');
  let displayLine = 0;

  return (
    <div className={`py-1.5 ${maxHeight} overflow-auto custom-scrollbar`}>
      {lines.map((line, i) => {
        let cls = 'diff-line diff-meta';
        let showLn = true;
        let marker = '';

        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) {
          showLn = false;
        } else if (line.startsWith('@@')) {
          cls = 'diff-line diff-hunk';
          showLn = false;
          // Numbers follow the new file: "@@ -a,b +c,d @@" starts at line c.
          const match = /@@ -\d+(?:,\d+)? \+(\d+)/.exec(line) ?? /@@ -(\d+)/.exec(line);
          if (match) displayLine = Number(match[1]) - 1;
        } else if (line.startsWith('+')) {
          cls = 'diff-line diff-add';
          displayLine += 1;
          marker = 'added';
        } else if (line.startsWith('-')) {
          cls = 'diff-line diff-del';
          marker = 'removed';
        } else {
          displayLine += 1;
        }

        const ln = showLn && !line.startsWith('-') ? String(displayLine) : '';

        return (
          <div key={i} className={cls}>
            <span className="ln">{ln}</span>
            <span className="tx">
              {marker ? <span className="sr-only">{marker}: </span> : null}
              {line.length === 0 ? ' ' : line}
            </span>
          </div>
        );
      })}
    </div>
  );
}
