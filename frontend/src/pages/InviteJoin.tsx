import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import Bee from '../components/Bee';
import Icon from '../components/Icon';
import { copyText } from '../clipboard';
import { parseKeyFragment, rememberRoomKey } from '../e2e';
import {
  RELAY_PARAM,
  agentJoinPrompt,
  basePath,
  invitePath,
  isValidRoomCode,
  joinCommand,
  normalizeRelayUrl,
  normalizeRoomCode,
  relayWsBase,
  rememberRelay,
  roomPath,
} from '../relayUrl';
import SiteLayout from '../site/SiteLayout';
import '../v3/v3.css';

const DEFAULT_CLI_RELAY = 'ws://localhost:8000/ws';

type CopiedTarget = 'agent' | 'command' | null;

/**
 * Invite landing page (`/j/:code`): reads `?relay=` and the `#key=` fragment
 * strictly client-side (fragments never leave the browser) and gives teammates
 * a one-click prompt for their coding agent, the raw `hivelane join` command,
 * and a link to open the live room dashboard.
 */
export default function InviteJoin() {
  const { code: rawCode } = useParams<{ code: string }>();
  const { search, hash } = useLocation();
  const roomId = normalizeRoomCode(rawCode ?? '');
  const valid = isValidRoomCode(roomId);

  const e2eKey = useMemo(() => parseKeyFragment(hash), [hash]);
  const queryRelay = useMemo(() => {
    const raw = new URLSearchParams(search).get(RELAY_PARAM);
    return raw ? normalizeRelayUrl(raw) : null;
  }, [search]);
  useEffect(() => {
    if (queryRelay) rememberRelay(queryRelay);
  }, [queryRelay]);
  const relayWs = queryRelay ?? relayWsBase();
  const nonDefaultRelay = relayWs !== DEFAULT_CLI_RELAY;

  const fullInviteUrl = useMemo(() => {
    if (typeof window === 'undefined' || !valid) return '';
    const url = new URL(`${window.location.origin}${basePath()}${invitePath(roomId)}`);
    const params = new URLSearchParams(search);
    if (nonDefaultRelay || params.has(RELAY_PARAM)) {
      url.searchParams.set(RELAY_PARAM, relayWs);
    }
    if (e2eKey) {
      url.hash = `key=${encodeURIComponent(e2eKey)}`;
    }
    return url.toString();
  }, [e2eKey, nonDefaultRelay, relayWs, roomId, search, valid]);

  const rawJoinCommand = useMemo(
    () => (fullInviteUrl ? joinCommand(fullInviteUrl, relayWs) : ''),
    [fullInviteUrl, relayWs],
  );
  const agentPrompt = useMemo(
    () => (fullInviteUrl ? agentJoinPrompt(fullInviteUrl, relayWs) : ''),
    [fullInviteUrl, relayWs],
  );

  const [copied, setCopied] = useState<CopiedTarget>(null);
  useEffect(() => {
    if (!copied) return undefined;
    const id = window.setTimeout(() => setCopied(null), 2400);
    return () => window.clearTimeout(id);
  }, [copied]);

  const handleCopy = async (kind: Exclude<CopiedTarget, null>, value: string) => {
    if (await copyText(value)) {
      setCopied(kind);
    }
  };

  if (!valid) {
    return (
      <SiteLayout title="Invalid invite link" minimal>
        <div className="v3-start v3-enter">
          <Bee state="idle" accent="var(--v3-accent)" size={68} />
          <h1 className="v3-headline v3-start__title">That invite link has an invalid room code.</h1>
          <p className="v3-sub v3-start__sub">
            Ask a teammate to run <code className="v3-mono">hivelane invite</code> again, or open the dashboard home to enter a code.
          </p>
          <div className="v3-start__actions">
            <Link to="/start" className="v3-btn v3-btn--primary v3-btn--pill">
              Open dashboard <Icon name="arrow" size={16} />
            </Link>
          </div>
        </div>
      </SiteLayout>
    );
  }

  return (
    <SiteLayout title={`Join room ${roomId}`} minimal>
      <div className="v3-start v3-invite v3-enter">
        <Bee state="idle" accent="var(--v3-accent)" size={68} />

        <div className="v3-invite__badges">
          <span className="v3-invite__badge v3-mono">Room {roomId}</span>
          {nonDefaultRelay ? (
            <span className="v3-invite__badge v3-mono" title={relayWs}>
              <Icon name="link" size={13} /> {relayWs}
            </span>
          ) : null}
          {e2eKey ? (
            <span className="v3-invite__badge v3-invite__badge--e2e">
              <Icon name="lock" size={13} /> End-to-end encrypted
            </span>
          ) : null}
        </div>

        <h1 className="v3-headline v3-start__title">
          Join room <span className="v3-mono">{roomId}</span>
        </h1>
        <p className="v3-sub v3-start__sub">
          Connects your repo&apos;s coding agent to this room on this machine—no{' '}
          <code className="v3-mono">.hivelane.toml</code> commit needed.
          {e2eKey ? ' Your room key stays in the URL fragment and is never sent to any server.' : ''}
        </p>

        <div className="v3-start__actions">
          <button
            type="button"
            className="v3-btn v3-btn--primary v3-btn--pill"
            onClick={() => void handleCopy('agent', agentPrompt)}
          >
            <Icon name={copied === 'agent' ? 'check' : 'copy'} size={15} />
            {copied === 'agent' ? 'Copied for your coding agent' : 'Copy for your coding agent'}
          </button>
          <Link
            to={{
              pathname: roomPath(roomId),
              search,
              hash: e2eKey ? `#k=${encodeURIComponent(e2eKey)}` : '',
            }}
            onClick={() => {
              if (e2eKey) rememberRoomKey(roomId, e2eKey);
            }}
            className="v3-btn v3-btn--secondary v3-btn--pill"
          >
            Open room dashboard <Icon name="arrow" size={15} />
          </Link>
        </div>

        <div className="v3-invite__panel">
          <div className="v3-invite__panel-head">
            <span className="v3-label">Or run in your repo</span>
            <span className="v3-label v3-faint">Installs hooks &amp; remembers this room locally</span>
          </div>
          <div className="v3-copy">
            <code>{rawJoinCommand}</code>
            <button
              type="button"
              className="v3-btn v3-btn--ghost v3-btn--sm"
              onClick={() => void handleCopy('command', rawJoinCommand)}
            >
              <Icon name={copied === 'command' ? 'check' : 'copy'} size={14} />
              {copied === 'command' ? 'Copied' : 'Copy'}
            </button>
          </div>
        </div>
      </div>
    </SiteLayout>
  );
}
