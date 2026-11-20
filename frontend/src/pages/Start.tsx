import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Bee from '../components/Bee';
import Icon from '../components/Icon';
import { DOCS_URL } from '../links';
import { isValidRoomCode, normalizeRoomCode, relayWsBase, roomPath } from '../relayUrl';
import { createRoom, ownedRooms } from '../roomAccess';
import SiteLayout from '../site/SiteLayout';
import '../v3/v3.css';

const FLOW = [
  { title: 'Create a room', body: 'A code your team shares. It lasts a week.' },
  { title: 'Run hivelane up', body: 'In each repo an agent works in: hooks and a daemon, nothing else changes.' },
  { title: 'Watch the lanes', body: 'Every agent shows up live; overlapping edits are caught before they land.' },
];

/** Create or join a room (v3 start page). The landing page links here as "Open dashboard". */
export default function Start() {
  const navigate = useNavigate();
  const [joinCode, setJoinCode] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mine] = useState(() => ownedRooms());

  const createWorkspace = async () => {
    setCreating(true);
    setError(null);
    try {
      // Keeps the room's owner token in this browser (see roomAccess.ts).
      navigate(roomPath(await createRoom()), { state: { welcome: true } });
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error';
      setError(`Could not create a room on ${relayWsBase()} (${detail}). The relay may be waking up: try again in a moment.`);
      setCreating(false);
    }
  };

  const joinWorkspace = (event: React.FormEvent) => {
    event.preventDefault();
    // Accepts "abcd-2345", "ABCD 2345" or a pasted room link.
    const code = normalizeRoomCode(joinCode);
    if (!code) return;
    if (!isValidRoomCode(code)) {
      setError('Room codes are 8 letters and digits (rooms from before the switch have 6). Check the code and try again.');
      return;
    }
    setError(null);
    navigate(roomPath(code));
  };

  return (
    <SiteLayout title="Open the dashboard" minimal>
      <div className="v3-start v3-enter">
        <Bee state={creating ? 'editing' : 'idle'} accent="var(--v3-accent)" size={72} />
        <h1 className="v3-headline v3-start__title">Create a room, then bring your team&apos;s agents into it.</h1>
        <p className="v3-sub v3-start__sub">
          Hivelane watches every agent&apos;s edits and steps in before two of them change the same code.
        </p>

        <div className="v3-start__actions">
          <button
            type="button"
            className="v3-btn v3-btn--primary v3-btn--pill"
            onClick={() => void createWorkspace()}
            disabled={creating}
          >
            {creating ? 'Creating room…' : 'Create room'} <Icon name="arrow" size={16} />
          </button>
          <form className="v3-start__join" onSubmit={joinWorkspace}>
            <input
              value={joinCode}
              onChange={(event) => {
                setJoinCode(event.target.value);
                if (error) setError(null);
              }}
              placeholder="Room code or link"
              maxLength={200}
              autoCapitalize="characters"
              autoComplete="off"
              spellCheck={false}
              aria-label="Room code or room link"
            />
            <button className="v3-btn v3-btn--secondary v3-btn--pill" type="submit" disabled={!joinCode.trim()}>
              Join
            </button>
          </form>
        </div>

        {error ? (
          <p className="v3-start__error" role="alert">
            {error}
          </p>
        ) : null}

        {mine.length ? (
          <div className="v3-start__rooms">
            <span className="v3-label">Your rooms</span>
            <div className="v3-actions">
              {mine.map((code) => (
                <button key={code} type="button" className="v3-btn v3-btn--secondary v3-btn--sm v3-mono" onClick={() => navigate(roomPath(code))}>
                  {code}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <ol className="v3-start__flow">
          {FLOW.map((step, index) => (
            <li key={step.title}>
              <span className="v3-mono v3-faint">0{index + 1}</span>
              <strong>{step.title}</strong>
              <span className="v3-muted">{step.body}</span>
            </li>
          ))}
        </ol>

        <p className="v3-label">
          New here? Follow the <a href={`${DOCS_URL}quick-start/`}>two-minute setup</a>.
        </p>
      </div>
    </SiteLayout>
  );
}
