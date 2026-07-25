import { useEffect } from 'react';

interface DeployModalProps {
  open: boolean;
  roomId: string;
  wsUrl: string;
  onClose: () => void;
}

function watcherCommand(roomId: string, wsUrl: string): string {
  // Strip trailing /ROOM so --room can be passed separately (matches watcher CLI).
  const base = wsUrl.replace(/\/[^/]+$/, '');
  return (
    `python watcher/watcher.py --path . --name <your-name> ` +
    `--room ${roomId} --server ${base} --agent cursor`
  );
}

export default function DeployModal({
  open,
  roomId,
  wsUrl,
  onClose,
}: DeployModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const command = watcherCommand(roomId, wsUrl);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const el = document.createElement('textarea');
      el.value = text;
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="deploy-modal-title"
    >
      <button
        type="button"
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        aria-label="Close dialog"
        onClick={onClose}
      />
      <div className="relative w-full max-w-lg lane-shell rounded-2xl p-6 shadow-card animate-fade-up">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div>
            <h2
              id="deploy-modal-title"
              className="text-lg font-bold tracking-tight m-0"
            >
              Deploy Agent
            </h2>
            <p className="text-[13px] text-on-muted mt-1 mb-0">
              Run a watcher on a teammate&apos;s machine to stream their IDE into
              this room.
            </p>
          </div>
          <button
            type="button"
            className="icon-btn p-1.5 shrink-0"
            onClick={onClose}
            title="Close"
          >
            <span className="material-symbols-outlined text-[20px]">close</span>
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="section-label">Room code</label>
            <div className="mt-2 flex items-center gap-2">
              <code className="flex-1 font-mono text-sm text-primary bg-surface-deep border border-outline-soft rounded-lg px-3 py-2">
                {roomId || '—'}
              </code>
              <button
                type="button"
                className="rounded-lg border border-outline-soft px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                onClick={() => void copy(roomId)}
              >
                Copy
              </button>
            </div>
          </div>

          <div>
            <label className="section-label">Watcher command</label>
            <pre className="mt-2 m-0 font-mono text-[11px] leading-relaxed text-on-muted bg-surface-deep border border-outline-soft rounded-lg px-3 py-3 whitespace-pre-wrap break-all">
              {command}
            </pre>
            <button
              type="button"
              className="deploy-btn mt-3 w-full rounded-xl py-2.5 text-[12px] font-bold tracking-wide"
              onClick={() => void copy(command)}
            >
              Copy watcher command
            </button>
          </div>

          <ol className="m-0 pl-4 text-[12px] text-on-muted space-y-1.5 list-decimal">
            <li>Activate the watcher venv in the repo.</li>
            <li>
              Paste the command and replace{' '}
              <code className="text-primary">&lt;your-name&gt;</code>.
            </li>
            <li>Save files in the watched path — lanes update live here.</li>
          </ol>
        </div>
      </div>
    </div>
  );
}
