/**
 * The Hivelane guard bee, drawn from primitives so it themes with the
 * dashboard and stays tiny. Real hives post guard bees at the entrance to
 * check every bee coming in; Hivelane checks every edit before it lands.
 *
 * The body is ink and paper with a single brand-accent stripe, so amber and
 * red stay reserved for warn / block state. Poses map to product states:
 *   clear   - flying along its lane (empty state, all clear)
 *   warn    - hovering with a "!" (someone is nearby)
 *   blocked - holding the stop paddle (an edit was held)
 *   waiting - holding a queue ticket (you're next in line)
 *
 * `mascotSvg` returns a self-contained SVG string, so the same drawing is
 * used by the dashboard (themed through CSS variables) and by
 * scripts/build-brand.mjs (fixed colours for the README and docs).
 */

export type MascotPose = 'clear' | 'warn' | 'blocked' | 'waiting';

export interface MascotPalette {
  ink: string;
  body: string;
  accent: string;
  wing: string;
  ok: string;
  warn: string;
  block: string;
}

/** Colours taken from the dashboard theme, so light and dark both work. */
export const THEME_PALETTE: MascotPalette = {
  ink: 'rgb(var(--c-on-surface))',
  body: 'rgb(var(--c-surface-card))',
  accent: 'rgb(var(--c-primary))',
  wing: 'rgb(var(--c-surface-raised))',
  ok: 'var(--ok-fg)',
  warn: 'var(--warn-fg)',
  block: 'var(--block-fg)',
};

export const LIGHT_PALETTE: MascotPalette = {
  ink: '#18181B',
  body: '#FFFFFF',
  accent: '#0062D1',
  wing: '#EEF1F5',
  ok: '#15703D',
  warn: '#915C00',
  block: '#C4231A',
};

export const DARK_PALETTE: MascotPalette = {
  ink: '#F5F5F7',
  body: '#141416',
  accent: '#3B95FF',
  wing: '#202022',
  ok: '#30D158',
  warn: '#FF9F0A',
  block: '#FF6B60',
};

// Pointy-top hexagon, centre (80, 96), radius 44.
const HEX = 'M80 52 118.1 74V118L80 140 41.9 118V74Z';

function eyes(pose: MascotPose, c: MascotPalette): string {
  if (pose === 'clear') {
    // Happy arcs.
    return (
      `<path d="M60 84q6-8 12 0M88 84q6-8 12 0" fill="none" stroke="${c.ink}" stroke-width="5" stroke-linecap="round"/>`
    );
  }
  const r = pose === 'warn' ? 7 : 6;
  const brows =
    pose === 'blocked'
      ? `<path d="M58 71l14 4M102 71l-14 4" fill="none" stroke="${c.ink}" stroke-width="4.5" stroke-linecap="round"/>`
      : '';
  return (
    brows +
    `<circle cx="66" cy="83" r="${r}" fill="${c.ink}"/><circle cx="94" cy="83" r="${r}" fill="${c.ink}"/>` +
    `<circle cx="68.2" cy="80.6" r="2" fill="${c.body}"/><circle cx="96.2" cy="80.6" r="2" fill="${c.body}"/>`
  );
}

function mouth(pose: MascotPose, c: MascotPalette): string {
  const d =
    pose === 'blocked' ? 'M73 95h14' : pose === 'warn' ? 'M75 96q5-4 10 0' : 'M73 93q7 6 14 0';
  return `<path d="${d}" fill="none" stroke="${c.ink}" stroke-width="4" stroke-linecap="round"/>`;
}

function props(pose: MascotPose, c: MascotPalette): string {
  switch (pose) {
    case 'clear':
      // The lane it flies along, plus two motion ticks.
      return (
        `<path d="M14 152H146" stroke="${c.accent}" stroke-width="4" stroke-linecap="round" stroke-dasharray="2 12"/>` +
        `<path d="M18 92h12M14 106h12" stroke="${c.ink}" stroke-width="4" stroke-linecap="round" opacity=".35"/>`
      );
    case 'warn':
      return (
        `<path d="M80 12v12" stroke="${c.warn}" stroke-width="6" stroke-linecap="round"/>` +
        `<circle cx="80" cy="34" r="3.5" fill="${c.warn}"/>`
      );
    case 'blocked':
      // The stop paddle, held high.
      return (
        `<path d="M115 106l25-10" stroke="${c.ink}" stroke-width="6" stroke-linecap="round"/>` +
        `<path d="M141 98V50" stroke="${c.ink}" stroke-width="5" stroke-linecap="round"/>` +
        `<circle cx="141" cy="34" r="16" fill="${c.block}" stroke="${c.ink}" stroke-width="4"/>` +
        `<rect x="132" y="31" width="18" height="6" rx="2" fill="${c.body}"/>`
      );
    case 'waiting':
      // A deli-style queue ticket.
      return (
        `<path d="M116 108l14-6" stroke="${c.ink}" stroke-width="6" stroke-linecap="round"/>` +
        `<rect x="124" y="74" width="28" height="34" rx="5" fill="${c.body}" stroke="${c.ink}" stroke-width="4"/>` +
        `<path d="M131 84h14" stroke="${c.accent}" stroke-width="3" stroke-linecap="round"/>` +
        `<text x="138" y="102" text-anchor="middle" font-family="Geist, system-ui, sans-serif" font-weight="700" font-size="15" fill="${c.ink}">2</text>`
      );
  }
}

/** The full mascot as an SVG string (viewBox 0 0 160 160). */
export function mascotSvg(pose: MascotPose = 'clear', c: MascotPalette = THEME_PALETTE, title = 'Hivelane guard bee'): string {
  const id = `hl-hex-${pose}`;
  const tilt = pose === 'clear' ? ' transform="rotate(-6 80 96)"' : '';
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" role="img" aria-label="${title}">` +
    `<title>${title}</title>` +
    `<defs><clipPath id="${id}"><path d="${HEX}"/></clipPath></defs>` +
    (pose === 'clear' || pose === 'warn' ? props(pose, c) : '') +
    `<g${tilt}>` +
    // Wings sit behind the body.
    `<ellipse cx="34" cy="68" rx="27" ry="12" transform="rotate(-32 34 68)" fill="${c.wing}" stroke="${c.ink}" stroke-width="4.5"/>` +
    `<ellipse cx="126" cy="68" rx="27" ry="12" transform="rotate(32 126 68)" fill="${c.wing}" stroke="${c.ink}" stroke-width="4.5"/>` +
    // A vein in each wing so they read as wings, not ears.
    `<path d="M24 76 46 62M136 76 114 62" stroke="${c.ink}" stroke-width="3" stroke-linecap="round" opacity=".4"/>` +
    // Antennae.
    `<path d="M72 57C69 44 62 37 56 30M88 57C91 44 98 37 104 30" fill="none" stroke="${c.ink}" stroke-width="5" stroke-linecap="round"/>` +
    `<circle cx="55" cy="28" r="5.5" fill="${c.ink}"/><circle cx="105" cy="28" r="5.5" fill="${c.ink}"/>` +
    // Body: the hexagon with two stripes, the same shape as the logo mark.
    `<path d="${HEX}" fill="${c.body}"/>` +
    `<g clip-path="url(#${id})"><rect x="30" y="103" width="100" height="10" fill="${c.accent}"/><rect x="30" y="119" width="100" height="8" fill="${c.ink}"/></g>` +
    `<path d="${HEX}" fill="none" stroke="${c.ink}" stroke-width="6" stroke-linejoin="round"/>` +
    eyes(pose, c) +
    mouth(pose, c) +
    `</g>` +
    // Held props go in front of the wing.
    (pose === 'blocked' || pose === 'waiting' ? props(pose, c) : '') +
    `</svg>`
  );
}

/**
 * Avatar states, one per lane state. The face carries the state; the
 * animation lives in CSS (.bee-avatar.is-<state> in index.css).
 *   editing  - happy, wings buzzing, bobbing (working)
 *   idle     - sleepy eyes, slow hover
 *   conflict - wide eyes, wobbling (overlapping edits)
 *   blocked  - cross brows, shaking
 *   offline  - eyes shut, grounded and greyed
 */
export type AvatarState = 'editing' | 'idle' | 'conflict' | 'blocked' | 'offline';

function avatarFace(state: AvatarState, c: MascotPalette): string {
  const line = (d: string, w = 5) =>
    `<path d="${d}" fill="none" stroke="${c.ink}" stroke-width="${w}" stroke-linecap="round"/>`;
  switch (state) {
    case 'editing':
      return eyes('clear', c) + mouth('clear', c);
    case 'idle':
      // Half-closed, drooping lids.
      return line('M59 84q7 5 14 0M87 84q7 5 14 0') + line('M75 95q5 2 10 0', 4);
    case 'conflict':
      return eyes('warn', c) + mouth('warn', c);
    case 'blocked':
      return eyes('blocked', c) + mouth('blocked', c);
    case 'offline':
      return line('M59 84h14M87 84h14') + line('M75 95h10', 4);
  }
}

/**
 * The bee as a lane avatar (viewBox cropped to the bee, no props). `accent`
 * is the person's colour, painted on the stripe, so each person stays
 * recognisable. `uid` keeps the clip-path id unique per instance.
 */
export function avatarSvg(state: AvatarState, accent: string, uid: string, c: MascotPalette = THEME_PALETTE): string {
  const id = `hl-av-${uid}`;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="4 16 152 128" aria-hidden="true">` +
    `<defs><clipPath id="${id}"><path d="${HEX}"/></clipPath></defs>` +
    `<g class="bee__body">` +
    `<g class="bee__wing bee__wing--l"><ellipse cx="34" cy="68" rx="27" ry="12" transform="rotate(-32 34 68)" fill="${c.wing}" stroke="${c.ink}" stroke-width="5"/></g>` +
    `<g class="bee__wing bee__wing--r"><ellipse cx="126" cy="68" rx="27" ry="12" transform="rotate(32 126 68)" fill="${c.wing}" stroke="${c.ink}" stroke-width="5"/></g>` +
    `<g class="bee__antennae"><path d="M72 57C69 44 62 37 56 30M88 57C91 44 98 37 104 30" fill="none" stroke="${c.ink}" stroke-width="5" stroke-linecap="round"/>` +
    `<circle cx="55" cy="28" r="6" fill="${c.ink}"/><circle cx="105" cy="28" r="6" fill="${c.ink}"/></g>` +
    `<path d="${HEX}" fill="${c.body}"/>` +
    `<g clip-path="url(#${id})"><rect x="30" y="103" width="100" height="11" fill="${accent}"/><rect x="30" y="119" width="100" height="8" fill="${c.ink}"/></g>` +
    `<path d="${HEX}" fill="none" stroke="${c.ink}" stroke-width="6" stroke-linejoin="round"/>` +
    avatarFace(state, c) +
    `</g></svg>`
  );
}

/** The 32 px logo mark: the hexagon with two stripes. */
export function markSvg(c: MascotPalette = THEME_PALETTE, title = 'Hivelane'): string {
  const hex = 'M16 3 27.26 9.5V22.5L16 29 4.74 22.5V9.5Z';
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="${title}">` +
    `<defs><clipPath id="hl-mark"><path d="${hex}"/></clipPath></defs>` +
    `<path d="${hex}" fill="${c.body}"/>` +
    `<g clip-path="url(#hl-mark)"><rect y="13" width="32" height="4" fill="${c.accent}"/><rect y="19.5" width="32" height="3" fill="${c.ink}"/></g>` +
    `<path d="${hex}" fill="none" stroke="${c.ink}" stroke-width="2.6" stroke-linejoin="round"/>` +
    `</svg>`
  );
}
