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
      <div className="home__content">
        <p className="home__eyebrow">Agent Viewer</p>
        <h1 className="home__headline">Watch every agent in the room, live.</h1>
        <p className="home__sub">
          Spin up a workspace, share the code with your team, and every watcher
          and dashboard that joins it shows up on the same board.
        </p>

        <div className="home__actions">
          <button
            type="button"
            className="home__create"
            onClick={createWorkspace}
            disabled={creating}
          >
            {creating ? 'Creating…' : 'Create workspace'}
          </button>

          <form className="home__join" onSubmit={joinWorkspace}>
            <input
              className="home__join-input"
              value={joinCode}
              onChange={(event) => setJoinCode(event.target.value)}
              placeholder="Enter room code"
              maxLength={6}
              autoCapitalize="characters"
              spellCheck={false}
            />
            <button className="home__join-button" type="submit" disabled={!joinCode.trim()}>
              Join
            </button>
          </form>
        </div>

        {error && <p className="home__error">{error}</p>}
      </div>
    </div>
  );
}
