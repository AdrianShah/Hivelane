import { useState, type ReactNode } from 'react';
import { copyText } from '../clipboard';
import { upCommand } from '../relayUrl';
import type { RoomSettingsUpdate } from '../types';
import SharePanel from './SharePanel';

/**
 * The invite view: share this room, and connect a teammate's agent to it.
 * Agents integrate through their own hooks plus a local daemon, and
 * `hivelane up` sets up both, so all this view owes the user is the room, two
 * commands, and what they do.
 */

interface AddTeammateProps {
  roomId: string;
  settings: RoomSettingsUpdate | null;
  requireApproval?: boolean | null;
}

interface Step {
  label: string;
  command: string;
  detail: ReactNode;
}

function steps(roomId: string): Step[] {
  return [
    {
      label: 'Install Hivelane (once per machine)',
      command: 'pipx install hivelane',
      detail: (
        <>
          Needs Python 3.12 or newer. <code>uv tool install hivelane</code> works too.
        </>
      ),
    },
    {
      label: 'Join this room from your repo',
      command: upCommand(roomId),
      detail:
        'Run it in the repository your agent works in. It sets up the agent hooks and starts the Hivelane daemon for this room; your agent works exactly as before.',
    },
  ];
}

export default function AddTeammate({ roomId, settings, requireApproval = null }: AddTeammateProps) {
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (command: string) => {
    if (!(await copyText(command))) return;
    setCopied(command);
    window.setTimeout(() => setCopied(null), 1500);
  };

  return (
    <div className="p-6 lg:p-8 animate-fade-up">
      <h1 className="text-xl font-bold tracking-tight text-on-surface mb-1">
        Invite teammates
      </h1>
      <p className="text-[13px] text-on-muted mb-6">
        Each teammate joins once from their repo; their agent then shows up here as
        a lane.
      </p>

      <div className="space-y-5">
        <SharePanel roomId={roomId} settings={settings} requireApproval={requireApproval} />

        <section className="connect-panel">
          <header>
            <h2>Connect an agent step by step</h2>
            <p>Run these on each machine joining this room.</p>
          </header>

          <ol className="connect-steps">
            {steps(roomId).map((step, index) => (
              <li key={step.label}>
                <span className="connect-step-index">{index + 1}</span>
                <div className="connect-step-body">
                  <h3>{step.label}</h3>
                  <div className="connect-command">
                    <code>{step.command}</code>
                    <button type="button" onClick={() => void copy(step.command)}>
                      {copied === step.command ? 'Copied' : 'Copy'}
                    </button>
                  </div>
                  <p>{step.detail}</p>
                </div>
              </li>
            ))}
          </ol>

          <footer>
            <p>
              Check it worked with <code>hivelane status</code>. Conflict windows come
              from the repo&apos;s committed <code>.hivelane.toml</code>; the latest
              teammate to join applies them to the whole room.
            </p>
          </footer>
        </section>
      </div>
    </div>
  );
}
