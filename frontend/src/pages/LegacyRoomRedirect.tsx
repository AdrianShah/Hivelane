import { Navigate, useParams } from 'react-router-dom';
import { roomPath } from '../relayUrl';

/**
 * Links shared before /r/:code existed (/room/:code) keep working, and land on
 * the canonical form so the address bar matches what Share copies.
 */
export default function LegacyRoomRedirect() {
  const { code } = useParams<{ code: string }>();
  return <Navigate to={code ? roomPath(code) : '/'} replace />;
}
