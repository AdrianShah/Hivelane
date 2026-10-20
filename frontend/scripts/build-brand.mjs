// Writes the static brand assets in docs/brand/ from src/brand/mascot.ts, so
// the README and docs use exactly the drawing the dashboard renders.
//
//   node frontend/scripts/build-brand.mjs
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DARK_PALETTE, LIGHT_PALETTE, markSvg, mascotSvg } from '../src/brand/mascot.ts';

const out = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'docs', 'brand');
mkdirSync(out, { recursive: true });

const poses = ['clear', 'warn', 'blocked', 'waiting'];
for (const pose of poses) {
  writeFileSync(join(out, `mascot-${pose}.svg`), mascotSvg(pose, LIGHT_PALETTE) + '\n');
  writeFileSync(join(out, `mascot-${pose}-dark.svg`), mascotSvg(pose, DARK_PALETTE) + '\n');
}
writeFileSync(join(out, 'mark.svg'), markSvg(LIGHT_PALETTE) + '\n');
writeFileSync(join(out, 'mark-dark.svg'), markSvg(DARK_PALETTE) + '\n');
console.log(`wrote ${poses.length * 2 + 2} files to ${out}`);
