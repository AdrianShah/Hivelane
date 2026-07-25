import { useEffect, useId, useState } from 'react';
import type { FileChange, LaneState } from '../types';
import ArchitectureMap from './ArchitectureMap';

interface LaneProps {
  userId: string;
  state: LaneState;
  inConflict?: boolean;
  highlightFile?: string | null;
  showArchitectureMap?: boolean;
  index?: number;
}

const AVATAR_COLORS = [
  'from-teal-400/90 to-cyan-600',
  'from-indigo-400/90 to-violet-600',
  'from-emerald-400/90 to-teal-700',
  'from-sky-400/90 to-blue-600',
  'from-fuchsia-400/80 to-purple-700',
];

function avatarGradient(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function initials(name: string): string {
  const parts = name.trim().split(/[\s_-]+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function watchBadge(changes: FileChange[]): string {
  if (changes.length === 0) return 'WORKSPACE';
  const path = changes[0].file_path.replace(/\\/g, '/');
  const parts = path.split('/').filter(Boolean);
  if (parts.length >= 2) {
    return `${parts[0]}/${parts[1]}`.toUpperCase();
  }
  return (parts[0] ?? 'WORKSPACE').toUpperCase();
}

function relativeTime(ts: number | null): string {
  if (ts == null) return 'just now';
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) {
    const m = Math.floor(delta / 60);
    return `${m}m ago`;
  }
  const h = Math.floor(delta / 3600);
  return `${h}h ago`;
}

function shortHash(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) hash = (hash * 33 + seed.charCodeAt(i)) | 0;
  return (Math.abs(hash) >>> 0).toString(16).slice(0, 6);
}

function DiffLines({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return (
      <div className="px-3 py-2.5 text-[11px] font-mono text-on-dim">(empty diff)</div>
    );
  }

  const lines = diff.replace(/\r\n/g, '\n').split('\n');
  let displayLine = 0;

  return (
    <div className="py-1.5 max-h-64 overflow-auto custom-scrollbar">
      {lines.map((line, i) => {
        let cls = 'diff-line diff-meta';
        let showLn = true;

        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) {
          cls = 'diff-line diff-meta';
          showLn = false;
        } else if (line.startsWith('@@')) {
          cls = 'diff-line diff-hunk';
          showLn = false;
          const match = /@@ \-(\d+)/.exec(line);
          if (match) displayLine = Number(match[1]);
        } else if (line.startsWith('+')) {
          cls = 'diff-line diff-add';
          displayLine += 1;
        } else if (line.startsWith('-')) {
          cls = 'diff-line diff-del';
        } else {
          displayLine += 1;
        }

        const ln =
          showLn && !line.startsWith('-') ? String(displayLine) : showLn ? '' : '';

        return (
          <div key={i} className={cls}>
            <span className="ln">{ln}</span>
            <span className="tx">{line.length === 0 ? ' ' : line}</span>
          </div>
        );
      })}
    </div>
  );
}

export default function Lane({
  userId,
  state,
  inConflict = false,
  highlightFile = null,
  showArchitectureMap = false,
  index = 0,
}: LaneProps) {
  const { caption, changes, prompt, timestamp, agent } = state;
  const activePrompt = prompt?.trim() ? prompt.trim() : null;
  const showAgent = agent && agent !== 'unknown';
  const badge = watchBadge(changes);
  const baseId = useId();
  const [openFiles, setOpenFiles] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (!highlightFile) return;
    setOpenFiles((prev) => ({ ...prev, [highlightFile]: true }));
  }, [highlightFile]);

  const isOpen = (path: string) =>
    openFiles[path] ?? (changes.length === 1 || highlightFile === path);

  const toggle = (path: string) => {
    setOpenFiles((prev) => ({
      ...prev,
      [path]: !isOpen(path),
    }));
  };

  const commitId = shortHash(
    `${userId}-${timestamp ?? 0}-${changes.map((c) => c.file_path).join(',')}`,
  );

  return (
    <article
      id={`lane-${userId}`}
      className={`lane-shell rounded-2xl flex flex-col min-h-[440px] overflow-hidden animate-fade-up ${
        inConflict ? 'in-conflict' : ''
      }`}
      style={{ animationDelay: `${index * 60}ms` }}
    >
      <header className="flex items-center gap-3 px-5 py-4 border-b border-outline-soft/80 bg-white/[0.015]">
        <div className="relative shrink-0">
          <div
            className={`w-10 h-10 rounded-full bg-gradient-to-br ${avatarGradient(userId)} flex items-center justify-center text-[11px] font-bold text-white ring-2 ring-white/10`}
          >
            {initials(userId)}
          </div>
          <span
            className={`absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full border-2 border-surface-card ${
              inConflict ? 'bg-error' : 'bg-emerald shadow-live'
            }`}
            title={inConflict ? 'Conflict' : 'Online'}
          />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-semibold text-on-surface truncate tracking-tight">
            {userId}
          </h2>
          <p className="text-[11px] text-on-dim mt-0.5">
            {inConflict
              ? 'Editing contested file'
              : showAgent
                ? agent
                : 'Agent online'}
          </p>
        </div>
        <span className="font-mono text-[10px] font-semibold tracking-[0.06em] text-primary bg-primary/8 border border-primary/20 px-2.5 py-1 rounded-md uppercase truncate max-w-[42%]">
          {badge}
        </span>
      </header>

      <div className="flex flex-col gap-3.5 p-5 flex-1">
        <section className="prompt-panel rounded-xl p-3.5">
          <div className="flex items-center gap-2 mb-2.5">
            <span className="material-symbols-outlined text-primary text-[16px]">terminal</span>
            <h3 className="section-label m-0">Active Prompt</h3>
          </div>
          <p className="m-0 font-mono text-[12.5px] text-on-muted leading-relaxed whitespace-pre-wrap break-words">
            {activePrompt ? (
              <span className="text-slate-300">&ldquo;{activePrompt}&rdquo;</span>
            ) : (
              <span className="italic text-on-dim">No prompt captured yet</span>
            )}
          </p>
        </section>

        <section className="analysis-panel rounded-xl p-3.5 flex-1 flex flex-col gap-3.5">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-lavender text-[16px]">bolt</span>
            <h3 className="section-label m-0">AI Co-Pilot Analysis</h3>
          </div>
          <p className="m-0 text-[13.5px] text-on-surface/95 leading-relaxed font-medium">
            {caption ?? 'Waiting for AI summary of recent edits…'}
          </p>

          {changes.length === 0 ? (
            <p className="text-xs text-on-dim m-0">No file changes yet.</p>
          ) : (
            <ul className="list-none m-0 p-0 flex flex-col gap-2.5">
              {changes.map((change) => {
                const open = isOpen(change.file_path);
                const panelId = `${baseId}-${change.file_path}`;
                const highlighted = highlightFile === change.file_path;

                return (
                  <li
                    key={change.file_path}
                    className={`diff-shell rounded-xl overflow-hidden transition-shadow ${
                      highlighted ? 'ring-1 ring-amber/50 shadow-[0_0_20px_rgba(251,191,36,0.12)]' : ''
                    }`}
                  >
                    <button
                      type="button"
                      className="w-full flex items-center gap-2 px-3.5 py-2.5 text-left hover:bg-white/[0.025] transition-colors border-b border-outline/70"
                      aria-expanded={open}
                      aria-controls={panelId}
                      onClick={() => toggle(change.file_path)}
                    >
                      <span
                        className={`material-symbols-outlined text-[16px] text-on-dim transition-transform duration-150 ${
                          open ? 'rotate-90' : ''
                        }`}
                      >
                        chevron_right
                      </span>
                      <code className="font-mono text-[11.5px] text-primary truncate flex-1">
                        {change.file_path}
                      </code>
                      <span className="text-[10px] font-medium text-on-dim uppercase tracking-wider">
                        Diff
                      </span>
                    </button>
                    {open ? (
                      <div id={panelId}>
                        <DiffLines diff={change.diff} />
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      <footer className="mt-auto border-t border-outline-soft/80 px-5 py-3.5 bg-black/20">
        {showArchitectureMap ? (
          <ArchitectureMap />
        ) : (
          <p className="m-0 text-[11px] text-on-dim flex items-start gap-2 leading-relaxed">
            <span className="material-symbols-outlined text-[15px] text-lavender/80 mt-px">
              commit
            </span>
            <span>
              Committed to <span className="text-lavender font-medium">@development</span>{' '}
              <span className="font-mono text-on-muted/90">#{commitId}</span>
              <span className="text-on-dim"> · {relativeTime(timestamp)}</span>
            </span>
          </p>
        )}
      </footer>
    </article>
  );
}
