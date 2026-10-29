import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  isValidRoomCode,
  normalizeRoomCode,
  relayWsBase,
  roomPath,
} from '../relayUrl';
import { createRoom } from '../roomAccess';
import { Mascot } from '../brand/BrandArt';
import { DOCS_URL } from '../links';
import SiteLayout from '../site/SiteLayout';
import LanePreview from '../site/LanePreview';
import './Home.css';

/** Create or join a room. The landing page links here as "Open dashboard". */
export default function Start() {
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
    <SiteLayout title="Open the dashboard">
      <section className="home__hero">
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

        <LanePreview />
      </section>
    </SiteLayout>
  );
}
