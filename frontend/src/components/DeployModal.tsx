import { useRef } from 'react';
import { useDialogFocus } from '../dashboard/useDialogFocus';
import { copyText } from '../clipboard';
import { setupCommand } from '../relayUrl';

interface DeployModalProps {
  open: boolean;
  roomId: string;
  onClose: () => void;
  onCopied?: (label: string) => void;
}

/**
 * Quick copy of the one-line teammate setup. The invite view has the same
 * command with a step-by-step explanation; this is for someone who already
 * knows the drill and just wants the line.
 */
export default function DeployModal({
  open,
  roomId,
  onClose,
  onCopied,
}: DeployModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useDialogFocus(open, onClose, panelRef, closeRef);

  if (!open) return null;

  const command = setupCommand(roomId);

  const copy = async (text: string, label: string) => {
    if (await copyText(text)) onCopied?.(label);
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
        className="absolute inset-0 overlay-backdrop"
        aria-label="Close dialog"
        tabIndex={-1}
        onClick={onClose}
      />
      <div ref={panelRef} className="relative w-full max-w-lg dialog-panel rounded-2xl p-6 shadow-card animate-fade-up">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div>
            <h2
              id="deploy-modal-title"
              className="text-lg font-bold tracking-tight m-0"
            >
              Connect a teammate&apos;s agent
            </h2>
            <p className="text-[13px] text-on-muted mt-1 mb-0">
              One line installs Synergo and joins this room from their repo.
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="icon-btn p-1.5 shrink-0"
            onClick={onClose}
            title="Close"
            aria-label="Close"
          >
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">close</span>
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
                className="rounded-lg border border-outline-soft px-3 py-2 text-[13px] font-semibold text-on-muted hover:text-primary hover:border-primary/40 transition-colors"
                onClick={() => void copy(roomId, 'Room code')}
              >
                Copy
              </button>
            </div>
          </div>

          <div>
            <label className="section-label">Setup command</label>
            <pre className="mt-2 m-0 font-mono text-[11px] leading-relaxed text-on-muted bg-surface-deep border border-outline-soft rounded-lg px-3 py-3 whitespace-pre-wrap break-all">
              {command}
            </pre>
            <button
              type="button"
              className="deploy-btn mt-3 w-full rounded-xl py-2.5 text-[12px] font-bold tracking-wide"
              onClick={() => void copy(command, 'Setup command')}
            >
              Copy setup command
            </button>
          </div>

          <ol className="m-0 pl-4 text-[12px] text-on-muted space-y-1.5 list-decimal">
            <li>Open a terminal in the repository the agent works in.</li>
            <li>
              Paste the command: <code className="text-primary">pipx install synergo</code>{' '}
              once, then <code className="text-primary">synergo up</code> joins this
              room.
            </li>
            <li>Keep working — the agent shows up as a lane in the Synergo feed.</li>
          </ol>
        </div>
      </div>
    </div>
  );
}
