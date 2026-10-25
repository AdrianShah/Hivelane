import { useId } from 'react';
import { avatarSvg, markSvg, mascotSvg, type AvatarState, type MascotPose } from './mascot';

// The SVG strings are built from constants in ./mascot (no user input), so
// injecting them is safe; it keeps one drawing for the app and the docs.

/** The guard bee in one of its state poses. Decorative unless `label` is set. */
export function Mascot({ pose = 'clear', size = 96, label, className }: { pose?: MascotPose; size?: number; label?: string; className?: string }) {
  return (
    <span
      className={className}
      style={{ display: 'inline-block', width: size, height: size, lineHeight: 0 }}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      dangerouslySetInnerHTML={{ __html: mascotSvg(pose) }}
    />
  );
}

/** The bee as an avatar, animated per state by .bee-avatar.is-<state>. */
export function BeeAvatar({ state = 'idle', accent, className = '' }: { state?: AvatarState; accent: string; className?: string }) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  return (
    <span
      className={`bee-avatar is-${state} ${className}`}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: avatarSvg(state, accent, uid) }}
    />
  );
}

/** The hexagon logo mark. */
export function Mark({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <span
      className={className}
      style={{ display: 'inline-block', width: size, height: size, lineHeight: 0 }}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: markSvg() }}
    />
  );
}
