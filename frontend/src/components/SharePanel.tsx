import { useState } from 'react';
import { copyText } from '../clipboard';
import { roomLink, setupCommand } from '../relayUrl';
import {
  DEFAULT_ROOM_SETTINGS,
  describeSettingsSource,
  describeWindows,
} from '../roomSettings';
import type { RoomSettingsUpdate } from '../types';

/**
 * Everything a teammate needs to join this room, each one click to copy: the
 * code (to type), the dashboard link (to watch), and the CLI line (to connect
 * their agent). Also states the conflict windows the relay is enforcing, live
 * from room_settings, so nobody has to guess what "blocked" means here.
 */

interface SharePanelProps {
  roomId: string;
  /** Latest room_settings from the relay; null until the first one arrives. */
  settings: RoomSettingsUpdate | null;
  /** The room's join policy (room_policy_update); null until known. */
  requireApproval?: boolean | null;
}

type CopyState = { label: string; ok: boolean } | null;

export default function SharePanel({ roomId, settings, requireApproval = null }: SharePanelProps) {
  const [copyState, setCopyState] = useState<CopyState>(null);

  const copy = async (label: string, text: string) => {
    const ok = await copyText(text);
    setCopyState({ label, ok });
    window.setTimeout(
      () => setCopyState((current) => (current?.label === label ? null : current)),
      1500,
    );
  };

  const buttonText = (label: string, idle: string) => {
    if (copyState?.label !== label) return idle;
    return copyState.ok ? 'Copied' : 'Copy failed';
  };

  const link = roomLink(roomId);
  const command = setupCommand(roomId);

  return (
    <section className="share-panel" aria-labelledby="share-panel-title">
      <header>
        <h2 id="share-panel-title">Share this room</h2>
        <p>
          {requireApproval
            ? 'People with the code can ask to join; an owner approves each request.'
            : 'Anyone with the code can join, so share it with your team only.'}
        </p>
        {settings?.e2e ? (
          <p>
            <span aria-hidden="true">🔒 </span>
            This room is end-to-end encrypted. Send the room key separately from this link
            (<code>synergo key show</code> prints it): teammates run{' '}
            <code>synergo key set {roomId || 'CODE'}</code>,
            viewers add <code>#k=&lt;key&gt;</code> to the link or paste the key when asked.
          </p>
        ) : null}
      </header>

      <div className="share-code">
        <div className="min-w-0">
          <span className="section-label">Room code</span>
          <code className="share-code__value">{roomId || '—'}</code>
        </div>
        <button
          type="button"
          className="deploy-btn rounded-xl px-4 py-2 text-[12px] font-bold shrink-0"
          onClick={() => void copy('code', roomId)}
          disabled={!roomId}
        >
          {buttonText('code', 'Copy code')}
        </button>
      </div>

      <div className="share-row">
        <span className="section-label">Room link</span>
        <div className="connect-command">
          <code>{link}</code>
          <button type="button" onClick={() => void copy('link', link)}>
            {buttonText('link', 'Copy link')}
          </button>
        </div>
      </div>

      <div className="share-row">
        <span className="section-label">Teammate setup (run in their repo)</span>
        <div className="connect-command">
          <code>{command}</code>
          <button type="button" onClick={() => void copy('command', command)}>
            {buttonText('command', 'Copy')}
          </button>
        </div>
      </div>

      <div className="share-settings">
        <span className="material-symbols-outlined text-[18px] text-lavender" aria-hidden>
          timer
        </span>
        <div>
          <p className="share-settings__windows">
            {describeWindows(settings ?? DEFAULT_ROOM_SETTINGS)}
          </p>
          <p className="share-settings__source">{describeSettingsSource(settings)}</p>
        </div>
      </div>
    </section>
  );
}
