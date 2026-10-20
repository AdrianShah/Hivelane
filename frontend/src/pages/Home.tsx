import { useId, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  forgetRelay,
  isStaticHost,
  isValidRoomCode,
  normalizeRoomCode,
  relaySource,
  relayWsBase,
  rememberRelay,
  roomPath,
  type RelaySource,
} from '../relayUrl';
import { createRoom } from '../roomAccess';
import './Home.css';

const SOURCE_LABEL: Record<RelaySource, string> = {
  chosen: 'chosen in this tab',
  build: 'set for this site',
  default: 'default: a relay on this computer',
  origin: 'this site',
};

/**
 * Which relay rooms are created on and joined through, and a field to pick
 * another. The GitHub Pages copy of the dashboard has no relay behind it, so
 * there the field is open from the start.
 */
function RelayPicker({ onChange }: { onChange: () => void }) {
  const fieldId = useId();
  const [relay, setRelay] = useState(() => relayWsBase());
  const [source, setSource] = useState<RelaySource>(() => relaySource());
  const needsOne = isStaticHost() && source === 'default';
  const [editing, setEditing] = useState(needsOne);
  const [draft, setDraft] = useState(needsOne ? '' : relay);
  const [problem, setProblem] = useState<string | null>(null);

  const refresh = () => {
    setRelay(relayWsBase());
    setSource(relaySource());
    onChange();
  };

  const apply = (event: React.FormEvent) => {
    event.preventDefault();
    const chosen = rememberRelay(draft);
    if (!chosen) {
      setProblem('That is not a relay URL. Use wss://relay.example.com (or https://, or ws:// for a local relay).');
      return;
    }
    setProblem(null);
    setDraft(chosen);
    setEditing(false);
    refresh();
  };

  const reset = () => {
    forgetRelay();
    setProblem(null);
    setDraft(relayWsBase());
    setEditing(false);
    refresh();
  };

  return (
    <div className="home__relay">
      {editing ? (
        <form className="home__relay-form" onSubmit={apply}>
          <label htmlFor={fieldId} className="home__relay-label">
            Relay URL
          </label>
          <div className="home__relay-row">
            <input
              id={fieldId}
              className="home__relay-input"
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value);
                if (problem) setProblem(null);
              }}
              placeholder="wss://relay.example.com"
              autoComplete="off"
              spellCheck={false}
              inputMode="url"
              aria-describedby={`${fieldId}-hint`}
              aria-invalid={problem ? true : undefined}
            />
            <button type="submit" className="home__relay-button" disabled={!draft.trim()}>
              Use relay
            </button>
            {!needsOne ? (
              <button type="button" className="home__relay-link" onClick={() => setEditing(false)}>
                Cancel
              </button>
            ) : null}
          </div>
          <p id={`${fieldId}-hint`} className="home__relay-hint">
            {problem ??
              (needsOne
                ? 'This copy of the dashboard has no relay of its own. Paste the URL of your team\'s relay (from synergo relay or your host).'
                : 'Rooms are created on, and joined through, this relay. Kept for this tab only.')}
          </p>
        </form>
      ) : (
        <p className="home__relay-current">
          <span className="home__relay-label">Relay</span>{' '}
          <code title={relay}>{relay}</code>{' '}
          <span className="home__relay-source">({SOURCE_LABEL[source]})</span>{' '}
          <button
            type="button"
            className="home__relay-link"
            onClick={() => {
              setDraft(relay);
              setEditing(true);
            }}
          >
            Change
          </button>
          {source === 'chosen' ? (
            <button type="button" className="home__relay-link" onClick={reset}>
              Reset
            </button>
          ) : null}
        </p>
      )}
    </div>
  );
}

export default function Home() {
  const navigate = useNavigate();
  const [joinCode, setJoinCode] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped when the relay changes, so an old relay's error does not linger.
  const [, setRelayVersion] = useState(0);

  const createWorkspace = async () => {
    setCreating(true);
    setError(null);
    try {
      // Keeps the room's owner token in this browser (see roomAccess.ts).
      navigate(roomPath(await createRoom()));
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error';
      setError(
        `Could not create a room on ${relayWsBase()} (${detail}). Check that the relay is up, or pick another relay below.`,
      );
      setCreating(false);
    }
  };

  const joinWorkspace = (event: React.FormEvent) => {
    event.preventDefault();
    // Accepts "abcd-2345", "ABCD 2345" or a pasted room link.
    const code = normalizeRoomCode(joinCode);
    if (!code) return;
    if (!isValidRoomCode(code)) {
      setError(
        'Room codes are 8 letters and digits (rooms from before the switch have 6). Check the code and try again.',
      );
      return;
    }
    setError(null);
    navigate(roomPath(code));
  };

  return (
    <div className="home">
      <header className="home__nav">
        <div className="home__brand" aria-label="Synergo">
          <span className="home__brand-mark" aria-hidden="true">
            S
          </span>
          Synergo
        </div>
        <span className="home__nav-note">Live collaboration radar</span>
      </header>

      <main className="home__hero">
        <div className="home__content">
          <p className="home__eyebrow">
            <span className="home__eyebrow-dot" aria-hidden="true" />
            Multiplayer AI agent viewer
          </p>
          <h1 className="home__headline">
            See every agent, <em>live.</em>
          </h1>
          <p className="home__sub">
            Create a room, run <code>synergo up</code> in each teammate&apos;s
            repo, and catch overlapping file edits before they become merge
            conflicts.
          </p>

          <div className="home__actions">
            <button
              type="button"
              className="home__create"
              onClick={() => void createWorkspace()}
              disabled={creating}
            >
              {creating ? 'Creating room…' : 'Create room'}
              <span aria-hidden="true" className="home__create-arrow">
                →
              </span>
            </button>

            <form className="home__join" onSubmit={joinWorkspace}>
              <input
                className="home__join-input"
                value={joinCode}
                onChange={(event) => {
                  setJoinCode(event.target.value);
                  if (error) setError(null);
                }}
                placeholder="Room code"
                maxLength={200}
                autoCapitalize="characters"
                autoComplete="off"
                spellCheck={false}
                aria-label="Room code or room link"
              />
              <button
                className="home__join-button"
                type="submit"
                disabled={!joinCode.trim()}
              >
                Join
              </button>
            </form>
          </div>

          {error ? (
            <p className="home__error" role="alert">
              {error}
            </p>
          ) : null}

          <RelayPicker
            onChange={() => {
              setError(null);
              setRelayVersion((n) => n + 1);
            }}
          />
        </div>

        {/* Illustrative preview of the dashboard: two lanes colliding on one file. */}
        <figure className="home__preview" aria-label="Example: two agents editing the same file">
          <div className="home__lane">
            <div className="home__lane-head">
              <span className="home__avatar">MR</span>
              <div>
                <strong>Maya R.</strong>
                <span>Claude Code · feat/billing</span>
              </div>
              <span className="home__live">live</span>
            </div>
            <p className="home__intent">Moving invoice totals into a shared helper</p>
            <ul className="home__files">
              <li>
                src/billing/totals.ts <b>+42 −8</b>
              </li>
              <li className="is-hot">
                src/api/invoice.ts <b>+6 −3</b>
              </li>
            </ul>
          </div>
          <div className="home__lane home__lane--offset">
            <div className="home__lane-head">
              <span className="home__avatar home__avatar--alt">TO</span>
              <div>
                <strong>Tomás O.</strong>
                <span>Codex · fix/rounding</span>
              </div>
              <span className="home__live">live</span>
            </div>
            <p className="home__intent">Rounding tax per line instead of per invoice</p>
            <ul className="home__files">
              <li className="is-hot">
                src/api/invoice.ts <b>+11 −4</b>
              </li>
            </ul>
          </div>
          <figcaption className="home__alert">
            Both agents are editing <code>src/api/invoice.ts</code>
          </figcaption>
        </figure>
      </main>

      <footer className="home__footer">
        <span>Synergo</span>
        <span>Live collaboration radar for AI agents</span>
      </footer>
    </div>
  );
}
