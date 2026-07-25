import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { normalizeRoomCode, relayHttpOrigin } from '../relayUrl';
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
      const response = await fetch(`${relayHttpOrigin()}/rooms`, { method: 'POST' });
      if (!response.ok) throw new Error(`Server returned ${response.status}`);
      const data = (await response.json()) as { room_id: string };
      navigate(`/room/${data.room_id}`);
    } catch {
      setError('Could not reach the relay. Is the server running?');
      setCreating(false);
    }
  };

  const joinWorkspace = (event: React.FormEvent) => {
    event.preventDefault();
    const code = normalizeRoomCode(joinCode);
    if (!code) return;
    navigate(`/room/${code}`);
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
            Share a room, connect watchers, and catch overlapping file edits
            before they become merge conflicts.
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
                onChange={(event) => setJoinCode(event.target.value)}
                placeholder="ROOM"
                maxLength={6}
                autoCapitalize="characters"
                spellCheck={false}
                aria-label="Room code"
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
