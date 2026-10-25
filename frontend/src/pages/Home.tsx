import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  isValidRoomCode,
  normalizeRoomCode,
  relayWsBase,
  roomPath,
} from '../relayUrl';
import { createRoom } from '../roomAccess';
import { BeeAvatar, Mark, Mascot } from '../brand/BrandArt';
import { personColor } from '../dashboard/people';
import ThemeToggle from '../components/ThemeToggle';
import { DOCS_URL } from '../links';
import './Home.css';

const REPO_URL = 'https://github.com/AdrianShah/Hivelane';

export default function Home() {
  const navigate = useNavigate();
  const [joinCode, setJoinCode] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createWorkspace = async () => {
    setCreating(true);
    setError(null);
    try {
      // Keeps the room's owner token in this browser (see roomAccess.ts).
      navigate(roomPath(await createRoom()));
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error';
      setError(
        `Could not create a room on ${relayWsBase()} (${detail}). The relay may be waking up: try again in a moment.`,
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
        <div className="home__brand" aria-label="Hivelane">
          <Mark size={22} className="home__brand-mark" />
          Hivelane
        </div>
        <nav className="home__nav-end" aria-label="Site">
          <a className="home__nav-link" href={DOCS_URL}>
            Docs
          </a>
          <a className="home__nav-link" href={REPO_URL}>
            GitHub
          </a>
          <ThemeToggle />
        </nav>
      </header>

      <main className="home__hero">
        <div className="home__content">
          <Mascot pose="clear" size={88} className="home__mascot" />
          <h1 className="home__headline">
            See every agent, <em>live.</em>
          </h1>
          <p className="home__sub">
            Create a room, run <code>hivelane up</code> in each teammate&apos;s
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

          <p className="home__steps">
            New here? Follow the <a href={`${DOCS_URL}quick-start/`}>two-minute setup</a>.
          </p>
        </div>

        {/* Illustrative preview of the dashboard: two lanes colliding on one file. */}
        <figure className="home__preview" aria-label="Example: two agents editing the same file">
          <div className="home__lane">
            <div className="home__lane-head">
              <BeeAvatar className="home__avatar" state="editing" accent={personColor('Maya R.')} />
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
              <BeeAvatar className="home__avatar" state="conflict" accent={personColor('Tomás O.')} />
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
    </div>
  );
}
