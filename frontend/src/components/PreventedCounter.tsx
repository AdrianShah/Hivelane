import { useEffect, useId, useRef, useState } from 'react';
import { useDialogFocus } from '../dashboard/useDialogFocus';
import { PREVENTION_WINDOW_SECONDS, type PreventionSummary } from '../dashboard/prevented';
import {
  canCopyImage,
  copyCanvasImage,
  downloadCanvasPng,
  drawShareCard,
} from '../dashboard/shareCard';
import type { RoomStats } from '../dashboard/stats';

/**
 * "Hivelane prevented N collisions this session", with how it is counted and
 * a shareable card. The heuristic itself is documented (and tested) in
 * dashboard/prevented.ts.
 */

interface PreventedCounterProps {
  prevention: PreventionSummary;
  stats: RoomStats;
  onShare: () => void;
}

export function PreventedCounter({ prevention, stats, onShare }: PreventedCounterProps) {
  const n = prevention.prevented;
  return (
    <section className="prevented-card rounded-2xl p-4 sm:p-5 mb-5" aria-labelledby="prevented-title">
      <div className="flex flex-wrap items-center gap-4 sm:gap-6">
        <p className="prevented-number m-0 text-[44px] sm:text-[56px]" aria-hidden="true">
          {n}
        </p>
        <div className="min-w-[min(100%,15rem)] flex-1">
          <h2 id="prevented-title" className="m-0 text-[15px] sm:text-[16px] font-bold text-on-surface tracking-tight">
            Hivelane prevented {n} collision{n === 1 ? '' : 's'} this session
          </h2>
          <p className="m-0 mt-1 text-[12px] text-on-muted">
            {stats.blocks} block{stats.blocks === 1 ? '' : 's'} · {stats.warns} warning
            {stats.warns === 1 ? '' : 's'} · {stats.conflicts} overlap{stats.conflicts === 1 ? '' : 's'}
            {prevention.pending ? ` · ${prevention.pending} still being watched` : ''}
            {prevention.editedAnyway ? ` · ${prevention.editedAnyway} edited anyway` : ''}
          </p>
          <details className="mt-1.5 text-[12px] text-on-muted">
            <summary className="cursor-pointer text-on-dim hover:text-on-surface w-fit">
              How is this counted?
            </summary>
            <p className="m-0 mt-1.5 max-w-[60ch] leading-relaxed">
              A warning or block counts as prevented when the warned agent&apos;s next edit, within{' '}
              {PREVENTION_WINDOW_SECONDS / 60} minutes, is to a different file. Retries of the same
              blocked edit count once; an agent that edits the same file anyway, or goes quiet, is
              not counted.
            </p>
          </details>
        </div>
        <button type="button" className="deploy-btn rounded-xl px-4 py-2 text-[12px] font-bold shrink-0" onClick={onShare}>
          <span className="material-symbols-outlined text-[16px] mr-1 align-[-3px]" aria-hidden="true">
            ios_share
          </span>
          Share card
        </button>
      </div>
    </section>
  );
}

function describeCard(s: RoomStats): string {
  return `Hivelane session card: ${s.prevented} collisions prevented, ${s.blocks} blocks, ${s.warns} warnings, ${s.conflicts} overlaps, ${s.people} agents.`;
}

interface ShareCardDialogProps {
  open: boolean;
  stats: RoomStats;
  onClose: () => void;
  onNotice: (message: string) => void;
}

export function ShareCardDialog({ open, stats, onClose, onNotice }: ShareCardDialogProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const [busy, setBusy] = useState(false);
  // The card is a snapshot: drawn when the dialog opens, not on every frame.
  const [snapshot, setSnapshot] = useState<RoomStats | null>(null);
  const latestStats = useRef(stats);

  useEffect(() => {
    latestStats.current = stats;
  });

  useEffect(() => {
    if (open) setSnapshot(latestStats.current);
  }, [open]);

  useEffect(() => {
    if (!open || !snapshot || !canvasRef.current) return;
    const canvas = canvasRef.current;
    let cancelled = false;
    drawShareCard(canvas, snapshot);
    // Redraw once web fonts are in, so the PNG uses them.
    void document.fonts?.ready.then(() => {
      if (!cancelled) drawShareCard(canvas, snapshot);
    });
    return () => {
      cancelled = true;
    };
  }, [open, snapshot]);

  const panelRef = useRef<HTMLDivElement>(null);
  useDialogFocus(open, onClose, panelRef, closeRef);

  if (!open) return null;

  const download = async () => {
    if (!canvasRef.current) return;
    setBusy(true);
    try {
      await downloadCanvasPng(canvasRef.current, 'hivelane-session.png');
      onNotice('Card downloaded');
    } catch {
      onNotice('Could not create the image');
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!canvasRef.current) return;
    setBusy(true);
    const ok = await copyCanvasImage(canvasRef.current);
    setBusy(false);
    onNotice(ok ? 'Card copied as an image' : 'Copying images is not supported here, download it instead');
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-3 sm:p-6" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <button type="button" className="absolute inset-0 overlay-backdrop" aria-label="Close dialog" tabIndex={-1} onClick={onClose} />
      <div ref={panelRef} className="relative w-full max-w-3xl dialog-panel rounded-2xl p-4 sm:p-6 animate-fade-up max-h-full overflow-y-auto custom-scrollbar">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div>
            <h2 id={titleId} className="text-lg font-bold tracking-tight m-0">
              Share this session
            </h2>
            <p className="text-[12.5px] text-on-muted mt-1 mb-0">
              Counts only, no file names, prompts, names or room code. Rendered in your browser.
            </p>
          </div>
          <button ref={closeRef} type="button" className="icon-btn p-1.5 shrink-0" onClick={onClose} aria-label="Close">
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
              close
            </span>
          </button>
        </div>
        <canvas
          ref={canvasRef}
          className="share-canvas"
          role="img"
          aria-label={describeCard(snapshot ?? stats)}
        />
        <div className="flex flex-wrap gap-2 mt-4">
          <button type="button" className="deploy-btn rounded-xl px-4 py-2 text-[12px] font-bold" onClick={() => void download()} disabled={busy}>
            Download PNG
          </button>
          {canCopyImage() ? (
            <button
              type="button"
              className="rounded-xl border border-outline-soft px-4 py-2 text-[12px] font-bold text-on-muted hover:text-on-surface hover:border-lavender/40 transition-colors"
              onClick={() => void copy()}
              disabled={busy}
            >
              Copy image
            </button>
          ) : (
            <span className="text-[12px] text-on-dim self-center">
              Copying images needs https and a supporting browser; download works everywhere.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
