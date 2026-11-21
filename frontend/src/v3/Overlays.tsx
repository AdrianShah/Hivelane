import { useCallback, useRef, useState, type ReactNode } from 'react';
import Icon from '../components/Icon';
import { matchCommands } from '../dashboard/commands';
import { useDialogFocus } from '../dashboard/useDialogFocus';
import { INSTALL_COMMAND, roomLink, setupCommand, upCommand } from '../relayUrl';
import { DEFAULT_ROOM_SETTINGS, describeSettingsSource, describeWindows } from '../roomSettings';
import type { RoomSettingsUpdate } from '../types';
import type { MenuCommand } from './menuCommands';
import type { Toast } from './useToasts';

export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div className="v3-toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className="v3-toast">
          <Icon name="check" size={15} />
          {toast.message}
          <button type="button" className="v3-icon-btn" aria-label="Dismiss" onClick={() => onDismiss(toast.id)}>
            <Icon name="close" size={13} />
          </button>
        </div>
      ))}
    </div>
  );
}

// --- command menu ---------------------------------------------------------------

/** ⌘K / Ctrl+K: jump to a view or run an action. */
export function CommandMenu({
  open,
  getCommands,
  onClose,
}: {
  open: boolean;
  getCommands: () => MenuCommand[];
  onClose: () => void;
}) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const panel = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const close = useCallback(() => {
    setQuery('');
    setActive(0);
    onClose();
  }, [onClose]);
  useDialogFocus(open, close, panel, input);
  if (!open) return null;
  const results = matchCommands(getCommands(), query);

  const run = (command: MenuCommand | undefined) => {
    if (!command) return;
    close();
    command.run();
  };

  return (
    <>
      <div className="v3-overlay" onClick={close} aria-hidden="true" />
      <div className="v3-cmd" ref={panel} role="dialog" aria-modal="true" aria-label="Command menu">
        <div className="v3-cmd__input">
          <Icon name="search" size={17} />
          <input
            ref={input}
            value={query}
            placeholder="Jump to a view or run an action"
            aria-label="Command"
            aria-controls="v3-cmd-list"
            aria-activedescendant={results[active] ? `cmd-${results[active].id}` : undefined}
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') {
                event.preventDefault();
                setActive((i) => Math.min(results.length - 1, i + 1));
              } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setActive((i) => Math.max(0, i - 1));
              } else if (event.key === 'Enter') {
                event.preventDefault();
                run(results[active]);
              }
            }}
          />
          <span className="v3-kbd">esc</span>
        </div>
        <div className="v3-cmd__list" id="v3-cmd-list" role="listbox">
          {results.length === 0 ? <p className="v3-cmd__empty">Nothing matches “{query}”.</p> : null}
          {results.map((command, index) => {
            const heading = !query && results[index - 1]?.group !== command.group ? command.group : null;
            return (
              <div key={command.id}>
                {heading ? <p className="v3-menu__label">{heading}</p> : null}
                <button
                  id={`cmd-${command.id}`}
                  type="button"
                  role="option"
                  aria-selected={index === active}
                  className="v3-menu__item"
                  onMouseMove={() => setActive(index)}
                  onClick={() => run(command)}
                >
                  <Icon name={command.icon} size={15} />
                  {command.label}
                  {command.hint ? <span className="v3-faint">{command.hint}</span> : null}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}

// --- invite -------------------------------------------------------------------

function CopyRow({ label, value, onCopy }: { label: string; value: string; onCopy: (label: string, value: string) => void }) {
  return (
    <div className="grid gap-1.5 grid-cols-[minmax(0,1fr)]">
      <span className="v3-label">{label}</span>
      <div className="v3-copy">
        <code>{value}</code>
        <button type="button" className="v3-btn v3-btn--ghost v3-btn--sm" onClick={() => onCopy(label, value)}>
          <Icon name="copy" size={14} /> Copy
        </button>
      </div>
    </div>
  );
}

/**
 * Everything a teammate needs to join (merges the old SharePanel and
 * AddTeammate): the code, the link, the one-line setup, the two steps, the
 * join policy and the conflict windows in force.
 */
export function InviteContent({
  roomId,
  settings,
  requireApproval,
  onCopy,
}: {
  roomId: string;
  settings: RoomSettingsUpdate | null;
  requireApproval: boolean | null;
  onCopy: (label: string, value: string) => void;
}) {
  return (
    <div className="grid gap-6 grid-cols-[minmax(0,1fr)]">
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <span className="v3-label">Room code</span>
          <div className="v3-code-big">{roomId || '-'}</div>
        </div>
        <button type="button" className="v3-btn v3-btn--primary" onClick={() => onCopy('Room link', roomLink(roomId))}>
          <Icon name="link" size={15} /> Copy link
        </button>
      </div>
      <p className="v3-sub m-0">
        {requireApproval
          ? 'People with the code can ask to join; an owner approves each request.'
          : 'Anyone with the code can join, so share it with your team only.'}
        {settings?.e2e
          ? ` This room is end-to-end encrypted: send the room key separately (hivelane key show prints it); teammates run hivelane key set ${roomId}.`
          : ''}
      </p>
      <CopyRow label="Room link" value={roomLink(roomId)} onCopy={onCopy} />
      <CopyRow label="One-line setup (run in their repo)" value={setupCommand(roomId)} onCopy={onCopy} />
      <div className="grid gap-3 grid-cols-[minmax(0,1fr)]">
        <span className="v3-title">Or step by step</span>
        <ol className="v3-steps">
          <li>
            <div>
              <h3>Install Hivelane, once per machine</h3>
              <CopyRow label="" value={INSTALL_COMMAND} onCopy={() => onCopy('Install command', INSTALL_COMMAND)} />
              <p>Needs Python 3.12 or newer.</p>
            </div>
          </li>
          <li>
            <div>
              <h3>Join this room from the repo</h3>
              <CopyRow label="" value={upCommand(roomId)} onCopy={() => onCopy('Command', upCommand(roomId))} />
              <p>Sets up the agent hooks and the daemon. Check it with hivelane status.</p>
            </div>
          </li>
        </ol>
      </div>
      <div className="v3-listening">
        <Icon name="timer" size={18} />
        <div>
          <p className="m-0 text-[13px]">{describeWindows(settings ?? DEFAULT_ROOM_SETTINGS)}</p>
          <p className="m-0 v3-label">{describeSettingsSource(settings)}</p>
        </div>
      </div>
    </div>
  );
}

/** The invite sheet: InviteContent in a drawer from the right. */
export function InviteSheet({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);
  useDialogFocus(open, onClose, panel, closeBtn);
  if (!open) return null;
  return (
    <>
      <div className="v3-overlay" onClick={onClose} aria-hidden="true" />
      <div className="v3-drawer" ref={panel} role="dialog" aria-modal="true" aria-labelledby="invite-title">
        <div className="v3-drawer__head">
          <p className="v3-title" id="invite-title">
            Invite teammates
          </p>
          <button ref={closeBtn} type="button" className="v3-icon-btn" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={16} />
          </button>
        </div>
        <div style={{ padding: 24, minWidth: 0 }}>{children}</div>
      </div>
    </>
  );
}
