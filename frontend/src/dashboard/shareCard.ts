/**
 * The shareable "prevented collisions" card, drawn on a canvas in the
 * browser (no server, no dependency). Input is RoomStats only — counts and
 * agent kinds — so the image can never carry file names, prompts, people's
 * names or the room code.
 */
import { formatDuration, type RoomStats } from './stats.ts';

export const CARD_WIDTH = 1200;
export const CARD_HEIGHT = 630;

const INK = '#f8fafc';
const MUTED = '#94a3b8';
const DIM = '#64748b';
const TEAL = '#5eead4';
const LAVENDER = '#a5b4fc';
const ROSE = '#fb7185';
const AMBER = '#fbbf24';
const SANS = '"Geist", system-ui, sans-serif';
const MONO = '"Geist Mono", ui-monospace, monospace';

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function glow(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, color: string): void {
  const g = ctx.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, color);
  g.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, CARD_WIDTH, CARD_HEIGHT);
}

function tile(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  label: string,
  value: string,
  accent: string,
): void {
  roundRect(ctx, x, y, w, h, 18);
  ctx.fillStyle = 'rgba(255,255,255,0.045)';
  ctx.fill();
  ctx.strokeStyle = 'rgba(255,255,255,0.09)';
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.fillStyle = accent;
  roundRect(ctx, x + 20, y + 22, 6, 18, 3);
  ctx.fill();
  ctx.fillStyle = MUTED;
  ctx.font = `700 15px ${SANS}`;
  ctx.textBaseline = 'middle';
  ctx.fillText(label.toUpperCase(), x + 36, y + 31);
  ctx.fillStyle = INK;
  ctx.font = `700 38px ${MONO}`;
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(value, x + 20, y + h - 22);
}

/** Paint the card at `scale`× (2 for a crisp PNG). The canvas is resized. */
export function drawShareCard(
  canvas: HTMLCanvasElement,
  stats: RoomStats,
  { scale = 2, date = new Date() }: { scale?: number; date?: Date } = {},
): void {
  canvas.width = CARD_WIDTH * scale;
  canvas.height = CARD_HEIGHT * scale;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);

  // Background: deep navy, two soft glows, a faint dot grid.
  const bg = ctx.createLinearGradient(0, 0, CARD_WIDTH, CARD_HEIGHT);
  bg.addColorStop(0, '#070b14');
  bg.addColorStop(1, '#0e1522');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, CARD_WIDTH, CARD_HEIGHT);
  glow(ctx, 1080, 40, 520, 'rgba(94,234,212,0.16)');
  glow(ctx, 120, 640, 520, 'rgba(165,180,252,0.14)');
  ctx.fillStyle = 'rgba(148,163,184,0.10)';
  for (let x = 24; x < CARD_WIDTH; x += 32) {
    for (let y = 24; y < CARD_HEIGHT; y += 32) {
      ctx.beginPath();
      ctx.arc(x, y, 1.1, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // Brand.
  roundRect(ctx, 64, 56, 44, 44, 12);
  ctx.fillStyle = 'rgba(94,234,212,0.16)';
  ctx.fill();
  ctx.strokeStyle = 'rgba(94,234,212,0.45)';
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.fillStyle = TEAL;
  ctx.font = `700 22px ${MONO}`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('S', 86, 79);
  ctx.textAlign = 'left';
  ctx.fillStyle = INK;
  ctx.font = `800 28px ${SANS}`;
  ctx.fillText('Synergo', 124, 72);
  ctx.fillStyle = MUTED;
  ctx.font = `500 16px ${SANS}`;
  ctx.fillText('Multiplayer radar for AI coding agents', 124, 96);

  ctx.textAlign = 'right';
  ctx.fillStyle = DIM;
  ctx.font = `600 16px ${MONO}`;
  ctx.fillText(
    date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }).toUpperCase(),
    CARD_WIDTH - 64,
    78,
  );
  ctx.textAlign = 'left';

  // Hero number.
  const hero = String(stats.prevented);
  const heroSize = hero.length > 3 ? 150 : 190;
  ctx.font = `700 ${heroSize}px ${MONO}`;
  ctx.textBaseline = 'alphabetic';
  const heroWidth = ctx.measureText(hero).width;
  const heroGrad = ctx.createLinearGradient(64, 180, 64 + heroWidth, 360);
  heroGrad.addColorStop(0, TEAL);
  heroGrad.addColorStop(1, LAVENDER);
  ctx.fillStyle = heroGrad;
  ctx.fillText(hero, 58, 340);

  ctx.fillStyle = INK;
  ctx.font = `800 40px ${SANS}`;
  ctx.fillText(stats.prevented === 1 ? 'collision prevented' : 'collisions prevented', 64, 398);
  ctx.fillStyle = MUTED;
  ctx.font = `500 21px ${SANS}`;
  const agents = stats.people === 1 ? '1 agent' : `${stats.people} agents`;
  ctx.fillText(
    `${agents} · ${formatDuration(stats.sessionSeconds)} · ${stats.edits} edits across ${stats.filesTouched} files`,
    64,
    436,
  );

  // Stat tiles (right column).
  const tx = 700;
  const tw = 208;
  const th = 104;
  tile(ctx, tx, 150, tw, th, 'Blocks', String(stats.blocks), ROSE);
  tile(ctx, tx + tw + 20, 150, tw, th, 'Warnings', String(stats.warns), AMBER);
  tile(ctx, tx, 150 + th + 18, tw, th, 'Overlaps', String(stats.conflicts), LAVENDER);
  const votes = stats.usefulVotes + stats.noiseVotes;
  tile(
    ctx,
    tx + tw + 20,
    150 + th + 18,
    tw,
    th,
    'Rated useful',
    votes ? `${Math.round((stats.usefulVotes / votes) * 100)}%` : '—',
    TEAL,
  );

  // Activity strip: edits per bucket as bars, collisions as dots above them.
  const sx = 64;
  const sy = 488;
  const sw = CARD_WIDTH - 128;
  const sh = 64;
  ctx.fillStyle = DIM;
  ctx.font = `700 13px ${SANS}`;
  ctx.fillText('SESSION ACTIVITY', sx, sy - 12);
  const buckets = stats.editHistogram.length || 1;
  const maxEdits = Math.max(1, ...stats.editHistogram);
  const gap = 6;
  const bw = (sw - gap * (buckets - 1)) / buckets;
  for (let i = 0; i < buckets; i += 1) {
    const value = stats.editHistogram[i] ?? 0;
    const h = value ? Math.max(6, (value / maxEdits) * sh) : 3;
    const x = sx + i * (bw + gap);
    roundRect(ctx, x, sy + sh - h, bw, h, 3);
    ctx.fillStyle = value ? 'rgba(94,234,212,0.55)' : 'rgba(148,163,184,0.18)';
    ctx.fill();
    const collisions = stats.collisionHistogram[i] ?? 0;
    if (collisions) {
      ctx.beginPath();
      ctx.arc(x + bw / 2, sy + sh - h - 10, Math.min(7, 3 + collisions), 0, Math.PI * 2);
      ctx.fillStyle = ROSE;
      ctx.fill();
    }
  }

  // Footer: agent kinds (labels only) and the tagline.
  ctx.fillStyle = MUTED;
  ctx.font = `600 16px ${SANS}`;
  ctx.textBaseline = 'middle';
  const kinds = stats.agentKinds.slice(0, 5).join('  ·  ');
  ctx.fillText(kinds ? `Agents: ${kinds}` : 'Agents coordinating live', 64, 594);
  ctx.textAlign = 'right';
  ctx.fillStyle = DIM;
  ctx.fillText('synergo · see every agent, live', CARD_WIDTH - 64, 594);
  ctx.textAlign = 'left';
}

export function canvasToPng(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('toBlob failed'))), 'image/png');
  });
}

export function canCopyImage(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext &&
    typeof ClipboardItem !== 'undefined' &&
    typeof navigator.clipboard?.write === 'function'
  );
}

/** Copy the card to the clipboard as an image (Chrome, Edge, Safari). */
export async function copyCanvasImage(canvas: HTMLCanvasElement): Promise<boolean> {
  if (!canCopyImage()) return false;
  try {
    // Safari wants the promise handed to ClipboardItem synchronously.
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': canvasToPng(canvas) })]);
    return true;
  } catch {
    return false;
  }
}

export async function downloadCanvasPng(canvas: HTMLCanvasElement, filename: string): Promise<void> {
  const blob = await canvasToPng(canvas);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
