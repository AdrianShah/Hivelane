import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  isValidRoomCode,
  normalizeRoomCode,
  roomPath,
} from '../relayUrl';
import { createRoom } from '../roomAccess';
import './Home.css';

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
        `Could not create a workspace (${detail}). Check that the relay is up and CORS allows this site.`,
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
          <span className="home__brand-mark" aria-hidden>
            S
          </span>
          Synergo
        </div>
      </header>

      <main className="home__hero">
        <div className="home__content">
          <p className="home__eyebrow">Multiplayer AI agent viewer</p>
          <h1 className="home__headline">
            Synergo
            <br />
            <em>See every agent, live.</em>
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
              {creating ? 'Creating workspace…' : 'Create workspace'}
            </button>

            <div className="home__divider">or join</div>

            <form className="home__join" onSubmit={joinWorkspace}>
              <input
                className="home__join-input"
                value={joinCode}
                onChange={(event) => {
                  setJoinCode(event.target.value);
                  if (error) setError(null);
                }}
                placeholder="ROOM CODE"
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

          {error ? <p className="home__error">{error}</p> : null}
        </div>
      </main>

      <footer className="home__footer">Synergo · live collaboration radar</footer>
    </div>
  );
}
