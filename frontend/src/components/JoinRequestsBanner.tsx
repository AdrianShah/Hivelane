import type { JoinRequest } from '../types';
import JoinRequestRow from './JoinRequestRow';

/**
 * Under-header strip an owner sees, whatever view is open, while anyone waits
 * to join. Requests the relay settles (another owner answered, the joiner
 * left, five minutes passed) disappear on their own via join_resolved.
 */

interface JoinRequestsBannerProps {
  requests: JoinRequest[];
  onDecide: (requestId: string, approve: boolean) => void;
}

export default function JoinRequestsBanner({ requests, onDecide }: JoinRequestsBannerProps) {
  if (requests.length === 0) return null;
  return (
    <div
      className="px-5 py-2.5 bg-lavender/10 border-b border-lavender/30 z-40"
      role="region"
      aria-label="Requests to join"
      aria-live="polite"
    >
      <div>
        <p className="section-label m-0">
          Requests to join ({requests.length})
        </p>
        <ul className="m-0 p-0 list-none divide-y divide-outline-soft/50">
          {requests.map((request) => (
            <JoinRequestRow key={request.request_id} request={request} onDecide={onDecide} />
          ))}
        </ul>
      </div>
    </div>
  );
}
