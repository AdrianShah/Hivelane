import { useState } from 'react';
import { relayWsBase } from '../relayUrl';

/**
 * How to connect an agent to this room. Synergo used to spawn watcher processes
 * from here via a localhost companion service; agents now integrate through
 * their own hooks, so the only thing this panel owes the user is the two
 * commands and what they do.
 */

interface AddWatcherProps {
  roomId: string;
}

interface Step {
  label: string;
  command: string;
  detail: string;
}

function steps(roomId: string): Step[] {
  return [
    {
      label: 'Install the hooks (once per project)',
      command: 'synergo hook install claude-code',
      detail:
        'Adds Synergo to your agent’s hooks so it asks before editing a file. Your agent works exactly as before.',
    },
    {
      label: 'Join this room',
      command: `synergo join ${roomId} --name <your-name> --server ${relayWsBase()}`,
      detail:
        'Runs the local daemon that holds this room’s connection. Leave it running while you work.',
    },
  ];
}

export default function AddWatcher({ roomId }: AddWatcherProps) {
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(command);
      window.setTimeout(() => setCopied(null), 1500);
    } catch {
      setCopied(null);
    }
  };

  return (
    <section className="connect-panel">
      <header>
        <h2>Connect an agent</h2>
        <p>
          Run these on each machine joining this room. Requires{' '}
          <code>uv tool install synergo</code>.
        </p>
      </header>

      <ol className="connect-steps">
        {steps(roomId).map((step, index) => (
          <li key={step.label}>
            <span className="connect-step-index">{index + 1}</span>
            <div className="connect-step-body">
              <h3>{step.label}</h3>
              <div className="connect-command">
                <code>{step.command}</code>
                <button type="button" onClick={() => copy(step.command)}>
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
          Check it worked with <code>synergo status</code>. Teammates appear as lanes
          once their daemon joins.
        </p>
      </footer>
    </section>
  );
}
