import { useCallback, useEffect, useState } from 'react';
import {
  WEBHOOK_EVENTS,
  WEBHOOK_KINDS,
  addWebhook,
  deleteWebhook,
  guessKind,
  listWebhooks,
  loadPrefs,
  permissionState,
  requestPermission,
  savePrefs,
  showNotice,
  webhookUrlProblem,
  type NotificationPrefs,
  type PermissionState,
  type WebhookEvent,
  type WebhookInfo,
  type WebhookKind,
} from '../notifications';
import { relayHttpOrigin } from '../relayUrl';
import { ownerToken } from '../roomAccess';

/**
 * The Notifications view: browser notifications for this room (anyone's
 * blocks, only those involving you, conflict alerts), and for the room's
 * owner the relay's Slack / Discord / generic webhooks.
 */

interface NotificationsSettingsProps {
  roomId: string;
  /** The room is end-to-end encrypted (webhooks then carry no paths or names). */
  e2e?: boolean;
}

const EVENT_LABELS: Record<WebhookEvent, string> = {
  block: 'Blocked edits',
  warn: 'Warnings',
  conflict_alert: 'Conflict alerts',
  join_request: 'Join requests',
};

const KIND_LABELS: Record<WebhookKind, string> = {
  slack: 'Slack',
  discord: 'Discord',
  generic: 'Generic JSON',
};

function Switch({
  id,
  on,
  label,
  disabled,
  onChange,
}: {
  id: string;
  on: boolean;
  label: string;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={`relative shrink-0 w-10 h-6 rounded-full border transition-colors disabled:opacity-50 ${
        on ? 'bg-primary/30 border-primary/50' : 'bg-white/[0.04] border-outline-soft'
      }`}
    >
      <span
        className={`absolute top-[3px] w-4 h-4 rounded-full transition-all ${
          on ? 'left-[19px] bg-primary' : 'left-[3px] bg-on-dim'
        }`}
      />
    </button>
  );
}

function BrowserNotifications({ roomId }: { roomId: string }) {
  const [permission, setPermission] = useState<PermissionState>(() => permissionState());
  const [prefs, setPrefs] = useState<NotificationPrefs>(() => loadPrefs(roomId));

  useEffect(() => {
    setPrefs(loadPrefs(roomId));
  }, [roomId]);

  const update = (next: Partial<NotificationPrefs>) => {
    const merged = { ...prefs, ...next };
    setPrefs(merged);
    savePrefs(roomId, merged);
  };

  const enable = async (on: boolean) => {
    if (on && permission !== 'granted') {
      // Only ever from this click: browsers ignore prompts nobody asked for.
      const answer = await requestPermission();
      setPermission(answer);
      if (answer !== 'granted') return;
    }
    update({ enabled: on });
  };

  const unsupported = permission === 'unsupported';
  const denied = permission === 'denied';

  return (
    <div className="lane-shell rounded-2xl p-5 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <label className="section-label" htmlFor="notify-room">
            Browser notifications
          </label>
          <p className="text-[13px] text-on-surface mt-2 mb-0">
            {unsupported
              ? 'This browser does not support notifications.'
              : denied
                ? 'Notifications are blocked for this site. Allow them in the browser’s site settings, then turn this on.'
                : prefs.enabled && permission === 'granted'
                  ? `On for room ${roomId} while this tab is open.`
                  : `Get a desktop notification when something needs you in room ${roomId}.`}
          </p>
        </div>
        <Switch
          id="notify-room"
          on={prefs.enabled && permission === 'granted'}
          label="Browser notifications for this room"
          disabled={unsupported || denied}
          onChange={(on) => void enable(on)}
        />
      </div>

      <fieldset
        className="border-0 p-0 m-0 space-y-2"
        disabled={!prefs.enabled || permission !== 'granted'}
      >
        <legend className="text-[12px] text-on-muted mb-1">Edit blocks</legend>
        {(
          [
            ['anyone', 'Anyone in the room'],
            ['me', 'Only blocks involving me'],
            ['off', 'None'],
          ] as const
        ).map(([value, text]) => (
          <label key={value} className="flex items-center gap-2 text-[13px] text-on-surface">
            <input
              type="radio"
              name={`notify-blocks-${roomId}`}
              checked={prefs.blocks === value}
              onChange={() => update({ blocks: value })}
            />
            {text}
          </label>
        ))}
        {prefs.blocks === 'me' ? (
          <label className="block text-[12px] text-on-muted pl-6">
            Your name in this room (as <code className="font-mono">synergo up</code> joined)
            <input
              type="text"
              value={prefs.myName}
              maxLength={20}
              placeholder="e.g. ada"
              onChange={(event) => update({ myName: event.target.value })}
              className="mt-1 block w-full max-w-[16rem] rounded-lg border border-outline-soft bg-transparent px-2.5 py-1.5 font-mono text-[13px] text-on-surface"
            />
          </label>
        ) : null}
        <label className="flex items-center gap-2 text-[13px] text-on-surface pt-1">
          <input
            type="checkbox"
            checked={prefs.conflicts}
            onChange={(event) => update({ conflicts: event.target.checked })}
          />
          Conflict alerts
        </label>
        <button
          type="button"
          className="rounded-lg border border-outline-soft px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-on-muted hover:text-primary hover:border-primary/40 transition-colors disabled:opacity-50"
          onClick={() =>
            showNotice({
              title: 'Synergo notifications are on',
              body: `You will hear about room ${roomId} here.`,
              tag: `synergo:${roomId}:test`,
            })
          }
        >
          Send a test notification
        </button>
      </fieldset>
    </div>
  );
}

function statusText(hook: WebhookInfo): string {
  if (hook.status === 'paused') {
    return `Paused after ${hook.consecutive_failures} failed deliveries (${hook.last_error}); retried in a few minutes`;
  }
  if (hook.status === 'failing') {
    return `Last delivery failed (${hook.last_error})`;
  }
  return 'Delivering';
}

function Webhooks({ roomId, e2e }: { roomId: string; e2e: boolean }) {
  const token = ownerToken(roomId);
  const origin = relayHttpOrigin();
  const [hooks, setHooks] = useState<WebhookInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState('');
  const [kind, setKind] = useState<WebhookKind>('slack');
  const [kindTouched, setKindTouched] = useState(false);
  const [events, setEvents] = useState<WebhookEvent[]>(['block', 'conflict_alert']);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      setHooks(await listWebhooks(origin, roomId, token));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reach the relay');
    }
  }, [origin, roomId, token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!token) {
    return (
      <div className="lane-shell rounded-2xl p-5">
        <p className="section-label m-0">Slack, Discord and webhooks</p>
        <p className="text-[13px] text-on-muted mt-2 mb-0">
          Only the room’s owner (the dashboard or CLI that created it) can post this room’s
          blocks and conflicts to Slack, Discord or a webhook.
        </p>
      </div>
    );
  }

  const problem = url ? webhookUrlProblem(url) : null;
  const full = hooks != null && hooks.length >= 3;

  const submit = async () => {
    const check = webhookUrlProblem(url);
    if (check) {
      setError(check);
      return;
    }
    if (events.length === 0) {
      setError('Pick at least one event');
      return;
    }
    setBusy(true);
    try {
      await addWebhook(origin, roomId, token, { url: url.trim(), kind, events });
      setUrl('');
      setKindTouched(false);
      setError(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add the webhook');
    } finally {
      setBusy(false);
    }
  };

  const remove = async (hook: WebhookInfo) => {
    if (!window.confirm(`Stop posting to ${hook.url}?`)) return;
    setBusy(true);
    try {
      await deleteWebhook(origin, roomId, token, hook.id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove the webhook');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="lane-shell rounded-2xl p-5 space-y-4">
      <div>
        <p className="section-label m-0">Slack, Discord and webhooks</p>
        <p className="text-[13px] text-on-muted mt-2 mb-0">
          The relay posts this room’s events to up to three HTTPS webhooks (at most 30
          notifications a minute). The URL is a secret: it is stored with the room and shown
          masked.
          {e2e
            ? ' This room is end-to-end encrypted, so messages only say what happened (“a blocked edit in room …”), never paths, names or reasons.'
            : ''}
        </p>
      </div>

      {hooks == null && !error ? (
        <p className="text-[12px] text-on-muted m-0">Loading…</p>
      ) : null}
      {hooks != null && hooks.length > 0 ? (
        <ul className="m-0 p-0 list-none divide-y divide-outline-soft/50">
          {hooks.map((hook) => (
            <li key={hook.id} className="py-2.5 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-[13px] text-on-surface m-0">
                  <span className="font-semibold">{KIND_LABELS[hook.kind] ?? hook.kind}</span>{' '}
                  <code className="font-mono text-[12px] text-on-muted break-all">{hook.url}</code>
                </p>
                <p className="text-[12px] text-on-muted mt-0.5 mb-0">
                  {hook.events.map((e) => EVENT_LABELS[e] ?? e).join(' · ')} —{' '}
                  <span className={hook.status === 'ok' ? '' : 'text-amber'}>{statusText(hook)}</span>
                </p>
              </div>
              <button
                type="button"
                className="shrink-0 rounded-lg border border-outline-soft px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-on-muted hover:text-error hover:border-error/40 transition-colors disabled:opacity-50"
                disabled={busy}
                onClick={() => void remove(hook)}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {hooks != null && hooks.length === 0 ? (
        <p className="text-[12px] text-on-muted m-0">No webhooks yet.</p>
      ) : null}

      {!full ? (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="block text-[12px] text-on-muted">
            Webhook URL
            <input
              type="url"
              value={url}
              autoComplete="off"
              spellCheck={false}
              placeholder="https://hooks.slack.com/services/…"
              onChange={(event) => {
                setUrl(event.target.value);
                if (!kindTouched) setKind(guessKind(event.target.value));
              }}
              className="mt-1 block w-full rounded-lg border border-outline-soft bg-transparent px-2.5 py-1.5 font-mono text-[13px] text-on-surface"
            />
          </label>
          {problem ? <p className="text-[12px] text-amber m-0">{problem}</p> : null}
          <div className="flex flex-wrap items-center gap-4">
            <label className="text-[12px] text-on-muted">
              Format{' '}
              <select
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value as WebhookKind);
                  setKindTouched(true);
                }}
                className="ml-1 rounded-lg border border-outline-soft bg-transparent px-2 py-1 text-[13px] text-on-surface"
              >
                {WEBHOOK_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {KIND_LABELS[k]}
                  </option>
                ))}
              </select>
            </label>
            {WEBHOOK_EVENTS.map((e) => (
              <label key={e} className="flex items-center gap-1.5 text-[13px] text-on-surface">
                <input
                  type="checkbox"
                  checked={events.includes(e)}
                  onChange={(change) =>
                    setEvents((prev) =>
                      change.target.checked
                        ? WEBHOOK_EVENTS.filter((x) => x === e || prev.includes(x))
                        : prev.filter((x) => x !== e),
                    )
                  }
                />
                {EVENT_LABELS[e]}
              </label>
            ))}
          </div>
          <button
            type="submit"
            className="deploy-btn rounded-xl px-4 py-2 text-[11px] font-bold disabled:opacity-50"
            disabled={busy || !url || problem != null || events.length === 0}
          >
            {busy ? 'Saving…' : 'Add webhook'}
          </button>
        </form>
      ) : (
        <p className="text-[12px] text-on-muted m-0">
          This room has the maximum of three webhooks; remove one to add another.
        </p>
      )}
      {error ? (
        <p className="text-[12px] text-error m-0" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export default function NotificationsSettings({ roomId, e2e = false }: NotificationsSettingsProps) {
  return (
    <div className="p-6 lg:p-8 max-w-2xl animate-fade-up space-y-4">
      <div>
        <h1 className="text-xl font-bold tracking-tight mb-1">Notifications</h1>
        <p className="text-[13px] text-on-muted m-0">
          Hear about blocked edits and conflicts without watching the board.
        </p>
      </div>
      <BrowserNotifications roomId={roomId} />
      <Webhooks roomId={roomId} e2e={e2e} />
    </div>
  );
}
