import Bee from '../components/Bee';
import Icon from '../components/Icon';
import { INSTALL_COMMAND, roomLink, upCommand } from '../relayUrl';
import type { V3Actions } from './model';

/**
 * The empty room's inline four-step guide (it replaces the old onboarding
 * modal): install, join from the repo, invite the team, and the bee
 * listening for the first agent. A step ticks itself off once the room shows
 * it happened.
 */
export default function SetupGuide({
  roomId,
  connected,
  anyAgent,
  e2e,
  actions,
}: {
  roomId: string;
  connected: boolean;
  anyAgent: boolean;
  e2e: boolean;
  actions: V3Actions;
}) {
  const steps = [
    {
      title: 'Install Hivelane, once per machine',
      body: 'Needs Python 3.12 or newer. uv tool install works too.',
      command: INSTALL_COMMAND,
      done: anyAgent,
    },
    {
      title: 'Join this room from your repo',
      body: 'Run it where your agent works. It sets up the agent hooks and the Hivelane daemon; your agent works exactly as before.',
      command: upCommand(roomId),
      done: anyAgent,
    },
    {
      title: 'Bring the team in',
      body: e2e
        ? 'Send the link, and the room key separately (hivelane key show prints it).'
        : 'Anyone with the link or code can join, so share it with your team only.',
      command: roomLink(roomId),
      done: false,
    },
  ];
  return (
    <section className="v3-section" aria-labelledby="setup-title">
      <div className="v3-section__head">
        <p className="v3-title" id="setup-title">
          Get the first agent in
        </p>
        <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm" onClick={actions.openInvite}>
          <Icon name="invite" size={14} /> Invite
        </button>
      </div>
      <ol className="v3-steps">
        {steps.map((step, index) => (
          <li key={step.title} className={step.done ? 'is-done' : undefined}>
            <div>
              <h3>{step.title}</h3>
              <div className="v3-copy">
                <code>{step.command}</code>
                <button
                  type="button"
                  className="v3-btn v3-btn--ghost v3-btn--sm"
                  onClick={() => actions.copyText(index === 2 ? 'Room link' : 'Command', step.command)}
                >
                  <Icon name="copy" size={14} /> Copy
                </button>
              </div>
              <p>{step.body}</p>
            </div>
          </li>
        ))}
        <li className={anyAgent ? 'is-done' : undefined}>
          <div>
            <h3>Watch it here</h3>
            <div className="v3-listening" role="status" aria-live="polite">
              <Bee state={anyAgent ? 'editing' : connected ? 'waiting' : 'offline'} accent="var(--v3-accent)" size={40} />
              <div>
                <p className="m-0 font-medium">
                  {anyAgent
                    ? 'Your first agent is here.'
                    : connected
                      ? 'Listening for your first agent…'
                      : 'Connecting to the relay…'}
                </p>
                <p className="m-0 v3-label">Each agent shows up as a lane, with its files and intent.</p>
              </div>
            </div>
          </div>
        </li>
      </ol>
    </section>
  );
}
