import { Navigate, useLocation, useParams } from 'react-router-dom';
import { roomPath } from '../relayUrl';

/**
 * Links shared before /r/:code existed (/room/:code) keep working, and land on
 * the canonical form so the address bar matches what Share copies.
 */
export default function LegacyRoomRedirect() {
  const { code } = useParams<{ code: string }>();
  // Keep ?view=/filters and the #k= e2e key: dropping the fragment here would
  // lose the room key of an old encrypted-room link.
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: code ? roomPath(code) : '/', search, hash }} replace />;
}
