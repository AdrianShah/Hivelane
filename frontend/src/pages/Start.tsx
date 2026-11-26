import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Bee from '../components/Bee';
import Icon from '../components/Icon';
import { livenessLabel, type RoomLiveness } from '../dashboard/roomLife';
import { DOCS_URL } from '../links';
import { isValidRoomCode, normalizeRoomCode, relayWsBase, roomPath } from '../relayUrl';
import { createRoom, ownedRooms, refreshOwnedRooms } from '../roomAccess';
import SiteLayout from '../site/SiteLayout';
import '../v3/v3.css';

const FLOW = [
  { title: 'Create a room', body: 'A code your team shares. It lasts a week, or until 30 minutes after everyone leaves.' },
  { title: 'Tell your agent', body: 'Paste the room’s prompt into any coding agent: it installs Hivelane and joins. Or run hivelane up yourself.' },
  { title: 'Watch the lanes', body: 'Every agent shows up live; overlapping edits are caught before they land.' },
];

/** Create or join a room (v3 start page). The landing page links here as "Open dashboard". */
export default function Start() {
  const navigate = useNavigate();
  const [joinCode, setJoinCode] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mine] = useState(() => ownedRooms());
  // What the relay says about each owned room; rooms it reports gone are
  // hidden (and their tokens dropped, see refreshOwnedRooms).
  const [liveness, setLiveness] = useState<Record<string, RoomLiveness>>({});
  const [checking, setChecking] = useState(() => mine.length > 0);
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    if (!mine.length) return undefined;
    let cancelled = false;
    void refreshOwnedRooms(mine, (code, result) => {
      if (!cancelled) setLiveness((prev) => ({ ...prev, [code]: result }));
    }).finally(() => {
      if (!cancelled) {
        setChecking(false);
        setNow(Date.now() / 1000);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [mine]);

  const visible = mine.filter((code) => liveness[code]?.state !== 'gone');

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

        {visible.length ? (
          <div className="v3-start__rooms">
            <span className="v3-label">Your rooms</span>
            <div className="v3-actions">
              {visible.map((code) => {
                const status = liveness[code];
                const label = livenessLabel(status, now) ?? (checking && !status ? 'Checking…' : null);
                return (
                  <button
                    key={code}
                    type="button"
                    className="v3-btn v3-btn--secondary v3-btn--sm v3-start__room"
                    onClick={() => navigate(roomPath(code))}
                  >
                    <span className="v3-mono">{code}</span>
                    {label ? (
                      <span className={status?.state === 'alive' ? 'v3-start__room-live' : 'v3-faint'}>{label}</span>
                    ) : null}
                  </button>
                );
              })}
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
