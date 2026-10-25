import { useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  forgetRelay,
  isStaticHost,
  relayHttpOrigin,
  relaySource,
  relayWsBase,
  rememberRelay,
  type RelaySource,
} from '../relayUrl';

const SOURCE_LABEL: Record<RelaySource, string> = {
  chosen: 'Picked by you, for this tab',
  build: 'Built into this site',
  default: 'Default: a relay on this computer',
  origin: 'The server this page came from',
};

type Health = 'checking' | 'waking' | 'online' | 'offline';

// A free-tier host (Render) sleeps when idle and takes up to a minute to wake;
// past this the pill says so instead of looking stuck.
const WAKING_AFTER_MS = 2500;
const GIVE_UP_AFTER_MS = 60_000;

const HEALTH_LABEL: Record<Health, string> = {
  checking: 'Connecting',
  waking: 'Waking relay',
  online: 'Relay online',
  offline: "Can't reach relay",
};

/** Asks the relay's /health once, and again whenever recheck() is called. */
function useRelayHealth(): [Health, () => void] {
  const [health, setHealth] = useState<Health>('checking');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const waking = window.setTimeout(() => setHealth('waking'), WAKING_AFTER_MS);
    const giveUp = window.setTimeout(() => controller.abort(), GIVE_UP_AFTER_MS);
    fetch(`${relayHttpOrigin()}/health`, { signal: controller.signal, cache: 'no-store' })
      .then((response) => setHealth(response.ok ? 'online' : 'offline'))
      .catch(() => {
        if (controller.signal.reason !== 'unmount') setHealth('offline');
      })
      .finally(() => {
        window.clearTimeout(waking);
        window.clearTimeout(giveUp);
      });
    return () => {
      window.clearTimeout(waking);
      window.clearTimeout(giveUp);
      controller.abort('unmount');
    };
  }, [attempt]);

  const recheck = useCallback(() => {
    setHealth('checking');
    setAttempt((n) => n + 1);
  }, []);
  return [health, recheck];
}

/**
 * The relay is the small server every teammate's daemon and this dashboard
 * connect to; rooms live on it. This pill shows which one is in use and
 * whether it answers, and opens a panel to point the dashboard at another.
 */
export default function RelayStatus({ onChange }: { onChange: () => void }) {
  const fieldId = useId();
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const relay = relayWsBase();
  const source = relaySource();
  // A static copy (GitHub Pages) with nothing baked in has no relay to try.
  const needsOne = isStaticHost() && source === 'default';
  const [health, recheck] = useRelayHealth();
  const [open, setOpen] = useState(needsOne);
  const [draft, setDraft] = useState(needsOne ? '' : relay);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const changed = () => {
    recheck();
    onChange();
  };

  const apply = (event: React.FormEvent) => {
    event.preventDefault();
    const chosen = rememberRelay(draft);
    if (!chosen) {
      setProblem('Not a relay URL. Use wss://relay.example.com (or https://, or ws:// for a local relay).');
      return;
    }
    setProblem(null);
    setDraft(chosen);
    setOpen(false);
    changed();
  };

  const reset = () => {
    forgetRelay();
    setProblem(null);
    setDraft(relayWsBase());
    changed();
  };

  const state = needsOne ? 'unset' : health;
  const host = relay.replace(/^wss?:\/\//, '').replace(/\/ws$/, '');

  return (
    <div className="relay" ref={rootRef}>
      <button
        type="button"
        className="relay__pill"
        data-state={state}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        title={needsOne ? 'Pick a relay' : `${HEALTH_LABEL[health]}: ${relay}`}
      >
        <span className="relay__dot" aria-hidden="true" />
        <span className="relay__pill-text">{needsOne ? 'Pick a relay' : HEALTH_LABEL[health]}</span>
        {!needsOne ? <span className="relay__host">{host}</span> : null}
      </button>

      {open ? (
        <div className="relay__panel" id={panelId} role="dialog" aria-label="Relay">
          <p className="relay__explain">
            The <strong>relay</strong> is the server your teammates&apos; agents and this page connect to. Rooms live
            on it, so everyone in a room must use the same one.
          </p>

          {!needsOne ? (
            <dl className="relay__facts">
              <div>
                <dt>Status</dt>
                <dd data-state={health}>
                  {HEALTH_LABEL[health]}
                  {health === 'waking' ? ' (free hosting sleeps when idle, up to a minute)' : ''}
                  {health === 'offline' ? ' (down, or not allowing this site in its CORS_ORIGINS)' : ''}
                  {health === 'offline' ? (
                    <button type="button" className="relay__link" onClick={recheck}>
                      Retry
                    </button>
                  ) : null}
                </dd>
              </div>
              <div>
                <dt>URL</dt>
                <dd>
                  <code>{relay}</code>
                </dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd>{SOURCE_LABEL[source]}</dd>
              </div>
            </dl>
          ) : null}

          <form className="relay__form" onSubmit={apply}>
            <label htmlFor={fieldId} className="relay__label">
              {needsOne ? 'Relay URL' : 'Use a different relay'}
            </label>
            <div className="relay__row">
              <input
                id={fieldId}
                className="relay__input"
                value={draft}
                onChange={(event) => {
                  setDraft(event.target.value);
                  if (problem) setProblem(null);
                }}
                placeholder="wss://relay.example.com"
                autoComplete="off"
                spellCheck={false}
                inputMode="url"
                aria-invalid={problem ? true : undefined}
                aria-describedby={`${fieldId}-hint`}
              />
              <button type="submit" className="relay__button" disabled={!draft.trim() || draft.trim() === relay}>
                Use
              </button>
            </div>
            <p id={`${fieldId}-hint`} className="relay__hint" data-problem={problem ? true : undefined}>
              {problem ??
                (needsOne ? (
                  <>
                    Paste your team&apos;s relay: the URL <code>hivelane relay</code> prints, or your host&apos;s.
                  </>
                ) : (
                  <>
                    Self-hosting? Run <code>hivelane relay</code> and paste the URL it prints.
                  </>
                ))}
            </p>
          </form>

          {source === 'chosen' ? (
            <button type="button" className="relay__link" onClick={reset}>
              Back to this site&apos;s relay
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
