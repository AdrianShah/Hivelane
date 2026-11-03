import { useRef, useState } from 'react';
import { Mascot } from '../brand/BrandArt';
import { copyText } from '../clipboard';
import { useDialogFocus } from '../dashboard/useDialogFocus';
import { DOCS_URL } from '../links';
import { roomLink, setupCommand } from '../relayUrl';

const DONE_KEY = 'hivelane.onboarding.done';

/** True until this browser has finished or skipped the walkthrough once. */
export function shouldOnboard(): boolean {
  try {
    return window.localStorage.getItem(DONE_KEY) == null;
  } catch {
    return true;
  }
}

function markOnboarded(): void {
  try {
    window.localStorage.setItem(DONE_KEY, '1');
  } catch {
    // Private window: the tour shows again next time, which is harmless.
  }
}

interface OnboardingProps {
  open: boolean;
  roomId: string;
  onClose: () => void;
}

/**
 * First-room walkthrough: what a room is, how to connect your own agent, how
 * to bring the team in, and what the board will show. Opened once per browser
 * after creating a room, and again from the empty room's "Take the tour".
 */
export default function Onboarding({ open, roomId, onClose }: OnboardingProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const [step, setStep] = useState(0);
  const [copied, setCopied] = useState<string | null>(null);

  const finish = () => {
    markOnboarded();
    setStep(0);
    onClose();
  };
  useDialogFocus(open, finish, panelRef, nextRef);

  if (!open) return null;

  const copy = async (label: string, text: string) => {
    if (await copyText(text)) {
      setCopied(label);
      window.setTimeout(() => setCopied((c) => (c === label ? null : c)), 1500);
    }
  };

  const code = (label: string, text: string) => (
    <div className="onb__code">
      <code>{text}</code>
      <button type="button" onClick={() => void copy(label, text)}>
        {copied === label ? 'Copied' : 'Copy'}
      </button>
    </div>
  );

  const steps = [
    {
      title: 'Your room is ready',
      body: (
        <>
          <p>
            A room is where your team&apos;s agents meet. Every agent connected to it checks here before writing a
            file, and shows up on this board as a lane.
          </p>
          <p className="onb__room">
            Room code <strong>{roomId}</strong>
          </p>
          <p className="onb__note">Rooms last seven days. No account needed; this browser keeps the owner key.</p>
        </>
      ),
    },
    {
      title: 'Connect your agent',
      body: (
        <>
          <p>In a terminal, inside the repository you work in, run:</p>
          {code('setup', setupCommand(roomId))}
          <p className="onb__note">
            It installs the CLI, adds hooks for Claude Code, Codex, Cursor or Gemini CLI, and starts a small
            background daemon. Then commit the new <code>.hivelane.toml</code>.
          </p>
        </>
      ),
    },
    {
      title: 'Bring in your team',
      body: (
        <>
          <p>
            Once <code>.hivelane.toml</code> is committed, teammates just run <code>hivelane up</code> after pulling.
            Or send them the room link:
          </p>
          {code('link', roomLink(roomId))}
          <p className="onb__note">Anyone with the code can join, so share it with your team only.</p>
        </>
      ),
    },
    {
      title: 'Watch it work',
      body: (
        <>
          <ul className="onb__list">
            <li>
              <strong>Lanes</strong> show each agent&apos;s intent, plan and latest diffs.
            </li>
            <li>
              <strong>Warn or block</strong> appears when two agents reach for the same file or function.
            </li>
            <li>
              <strong>Timeline and heat map</strong> show where the team has been working.
            </li>
          </ul>
          <p className="onb__note">
            Press <kbd>?</kbd> any time for keyboard shortcuts, or read the{' '}
            <a href={`${DOCS_URL}quick-start/`}>quick start</a>.
          </p>
        </>
      ),
    },
  ];

  const last = step === steps.length - 1;
  const current = steps[step];

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby="onb-title">
      <button type="button" className="absolute inset-0 overlay-backdrop" aria-label="Close" tabIndex={-1} onClick={finish} />
      <div ref={panelRef} className="onb relative w-full max-w-lg dialog-panel rounded-2xl shadow-card animate-fade-up">
        <div className="onb__head">
          <Mascot pose="clear" size={44} />
          <span className="onb__count">
            Step {step + 1} of {steps.length}
          </span>
          <button type="button" className="onb__skip" onClick={finish}>
            Skip
          </button>
        </div>
        <div className="onb__progress" aria-hidden="true">
          {steps.map((s, i) => (
            <span key={s.title} className={i <= step ? 'is-on' : ''} />
          ))}
        </div>
        <h2 id="onb-title">{current.title}</h2>
        <div className="onb__body">{current.body}</div>
        <div className="onb__foot">
          {step > 0 ? (
            <button type="button" className="onb__back" onClick={() => setStep(step - 1)}>
              Back
            </button>
          ) : (
            <span />
          )}
          <button ref={nextRef} type="button" className="onb__next" onClick={() => (last ? finish() : setStep(step + 1))}>
            {last ? 'Go to my room' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  );
}
