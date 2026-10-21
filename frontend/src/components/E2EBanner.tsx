import { useState } from 'react';
import type { E2EState } from '../e2e';

/**
 * The end-to-end encryption strip under the header: what this dashboard can
 * read in an encrypted room, and a field to paste the room key into. The key
 * stays in this tab (sessionStorage for this room); it is never sent anywhere.
 */

interface E2EBannerProps {
  state: E2EState;
  hasKey: boolean;
  onKey: (passphrase: string) => void;
  onForget: () => void;
}

const MESSAGES: Record<E2EState, string> = {
  off: 'End-to-end encrypted room.',
  locked: 'Encrypted room — paste the room key to view files, diffs and prompts.',
  ok: 'End-to-end encrypted: decrypted in this browser; the relay only sees hashes.',
  'bad-key': "Can't decrypt this room with that key. Check it with your team and paste it again.",
  unsupported:
    'This page cannot decrypt: browsers only allow it on https:// or localhost. Open the dashboard over https.',
};

export default function E2EBanner({ state, hasKey, onKey, onForget }: E2EBannerProps) {
  const [draft, setDraft] = useState('');
  const shownState: E2EState = state === 'off' && !hasKey ? 'locked' : state;
  const needsKey = shownState === 'locked' || shownState === 'bad-key';
  const tone = shownState === 'ok' ? 'bg-emerald/10 border-emerald/30' : 'bg-amber/10 border-amber/30';

  return (
    <div className={`px-5 py-2.5 border-b z-40 ${tone}`} role="status" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-3 max-w-[1600px]">
        <p className="text-[13px] text-on-surface m-0">
          <span aria-hidden="true">🔒 </span>
          {MESSAGES[shownState]}
        </p>
        {needsKey ? (
          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              const key = draft.trim();
              if (key) {
                onKey(key);
                setDraft('');
              }
            }}
          >
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              aria-label="Room key"
              placeholder="Room key"
              className="rounded-lg px-3 py-1.5 text-[12px] font-mono bg-surface border border-white/10 text-on-surface"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" className="deploy-btn rounded-xl px-4 py-1.5 text-[11px] font-bold shrink-0">
              Decrypt
            </button>
          </form>
        ) : hasKey ? (
          <button
            type="button"
            className="text-[11px] text-on-muted hover:text-on-surface underline shrink-0"
            onClick={onForget}
          >
            Forget key in this tab
          </button>
        ) : null}
      </div>
    </div>
  );
}
