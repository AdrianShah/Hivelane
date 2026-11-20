import { useEffect, useRef, useState } from 'react';
import Bee from '../components/Bee';
import { headline } from '../dashboard/headline';
import type { V3Model } from './model';

/**
 * The notch: a black pill at the top of the page with the bee, the agent
 * count and one status (held / overlap / all clear). Hover or click opens it
 * to the headline sentence with Review and Ping. Driven by the room's
 * conflicts and held edits.
 */
export default function Notch({ model }: { model: V3Model }) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const line = headline({ online: model.onlineIds.length, conflicts: model.conflicts, held: model.held });
  const status =
    line.tone === 'block'
      ? { dot: 'is-block is-pulse', label: `${model.held.length} held` }
      : line.tone === 'warn'
        ? { dot: 'is-warn is-pulse', label: model.conflicts.length === 1 ? 'Overlap' : `${model.conflicts.length} overlaps` }
        : line.tone === 'clear'
          ? { dot: 'is-live', label: 'All clear' }
          : { dot: 'is-idle', label: 'Listening' };
  const bee = line.tone === 'block' ? 'blocked' : line.tone === 'warn' ? 'conflict' : line.tone === 'clear' ? 'editing' : 'idle';
  // Who a ping goes to: the held member, else the first person in the overlap.
  const target = model.held[0]?.others[0] ?? model.conflicts[0]?.users.find((u) => model.onlineIds.includes(u));
  const problem = line.tone === 'block' || line.tone === 'warn';

  useEffect(() => {
    if (!pinned) return;
    const away = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setPinned(false);
        setOpen(false);
      }
    };
    const esc = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setPinned(false);
        setOpen(false);
      }
    };
    window.addEventListener('pointerdown', away);
    window.addEventListener('keydown', esc);
    return () => {
      window.removeEventListener('pointerdown', away);
      window.removeEventListener('keydown', esc);
    };
  }, [pinned]);

  return (
    <div
      ref={ref}
      className={`v3-notch${open ? ' is-open' : ''}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => {
        if (!pinned) setOpen(false);
      }}
    >
      <button
        type="button"
        className="v3-notch__bar"
        aria-expanded={open}
        aria-controls="v3-notch-body"
        onClick={() => {
          setPinned((p) => !p);
          setOpen((o) => !(o && pinned));
        }}
      >
        <Bee state={bee} accent="#3b95ff" size={30} ring={false} />
        <span>
          {model.onlineIds.length} <span className="v3-notch__count">{model.onlineIds.length === 1 ? 'agent' : 'agents'}</span>
        </span>
        <span className="v3-notch__status" role="status" aria-live="polite">
          <span className={`v3-dot ${status.dot}`} aria-hidden="true" />
          {status.label}
        </span>
      </button>
      <div className="v3-notch__body" id="v3-notch-body" hidden={!open}>
        <p className="v3-notch__sentence">{line.text}</p>
        {problem ? (
          <div className="v3-actions">
            <button
              type="button"
              className="v3-btn v3-btn--primary v3-btn--sm"
              onClick={() => {
                model.actions.navigate('conflicts');
                setOpen(false);
                setPinned(false);
              }}
            >
              Review
            </button>
            {target && model.isOwner ? (
              <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm" onClick={() => model.actions.ping(target)}>
                Ping {target}
              </button>
            ) : null}
          </div>
        ) : (
          <p className="m-0 text-[12px] opacity-60">
            {model.onlineIds.length ? 'Nobody is in anyone else’s way.' : 'Invite a teammate to get started.'}
          </p>
        )}
      </div>
    </div>
  );
}
