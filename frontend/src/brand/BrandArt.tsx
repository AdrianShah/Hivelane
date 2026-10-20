import { markSvg, mascotSvg, type MascotPose } from './mascot';

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
