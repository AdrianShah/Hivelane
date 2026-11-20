import { useId } from 'react';
import { avatarSvg, type AvatarState } from '../brand/mascot';

/**
 * The guard bee as an avatar, centred in a square box (size × size): the
 * avatar drawing is wider than it is tall, so it is fitted inside the box
 * rather than stretched. Ring colour and motion carry the state
 * (.v3-bee.is-<state> in index.css):
 *
 *   editing   green ring, bob + wing buzz
 *   idle      no colour, slow float
 *   conflict  amber ring, wobble
 *   blocked   red ring, shake
 *   offline   grey, still
 *   waiting   blue ring with a dot orbiting it
 *
 * All motion stops under prefers-reduced-motion. `accent` paints the bee's
 * stripe in the person's colour so people stay recognisable.
 */
export interface BeeProps {
  state?: AvatarState;
  accent: string;
  /** Box edge in px (default 32). */
  size?: number;
  /** Draw the state ring (default true). */
  ring?: boolean;
  /** Accessible name; decorative without one. */
  label?: string;
  className?: string;
}

export default function Bee({ state = 'idle', accent, size = 32, ring = true, label, className = '' }: BeeProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  return (
    <span
      className={`v3-bee is-${state}${ring ? ' has-ring' : ''} ${className}`}
      style={{ width: size, height: size }}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <span
        className="v3-bee__art bee-avatar"
        // Built from constants in brand/mascot.ts (no user input).
        dangerouslySetInnerHTML={{ __html: avatarSvg(state, accent, uid) }}
      />
      {state === 'waiting' && ring ? <span className="v3-bee__orbit" /> : null}
    </span>
  );
}
