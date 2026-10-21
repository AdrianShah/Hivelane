import { useParams } from 'react-router-dom';
import App from '../App';
import { normalizeRoomCode } from '../relayUrl';

/**
 * One App per room: switching rooms remounts it, so no state (lanes, votes,
 * the e2e key) carries over from the previous room. /r/abcd-2345 and
 * /r/ABCD2345 are one room, so the canonical-link redirect does not remount.
 */
export default function RoomRoute() {
  const { code } = useParams<{ code: string }>();
  return <App key={normalizeRoomCode(code ?? '')} />;
}
