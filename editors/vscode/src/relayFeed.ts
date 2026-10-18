import WebSocket from "ws";

export const PROTOCOL_VERSION = 1;

/** Close codes the relay uses (server/main.py). */
export const WS_CLOSE_JOIN_REFUSED = 4403;
export const WS_CLOSE_ROOM_NOT_FOUND = 4404;
export const WS_CLOSE_VERSION_TOO_OLD = 4426;
export const WS_CLOSE_RATE_LIMITED = 4429;

/** Room codes as the dashboard normalizes them: uppercase, spaces and dashes stripped. */
export function normalizeRoomCode(raw: string): string {
  return raw.trim().replace(/[\s-]+/g, "").toUpperCase();
}

export function roomUrl(relay: string, room: string): string {
  return `${relay.replace(/\/+$/, "")}/${encodeURIComponent(normalizeRoomCode(room))}`;
}

export type FeedState =
  | { kind: "connecting" }
  | { kind: "live" }
  | { kind: "pending" } // waiting for an owner's approval (only when allowed)
  | { kind: "retrying"; inMs: number; reason?: string }
  | { kind: "stopped"; reason: string };

export interface FeedOptions {
  /** Wait at the door of an approval-required room (an owner must admit us). Default false: stop instead. */
  allowApprovalWait?: boolean;
  /** Consecutive failed attempts (no successful join) before giving up. */
  maxAttempts?: number;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  /** Ping interval; a missed pong terminates the socket. 0 disables. */
  heartbeatMs?: number;
  /** Fallback wait after a 4429 close that carried no rate_limited frame. */
  rateLimitFallbackMs?: number;
}

/** Frames that are part of the handshake, not post-join traffic. */
const HANDSHAKE_FRAMES = new Set(["rate_limited", "room_rejected", "version_rejected", "name_rejected", "join_pending", "join_resolved"]);

/**
 * Spectator connection to a relay room.
 *
 * The relay accepts the socket before it decides (room not found, approval,
 * rate limit), so "open" proves nothing: the backoff resets only once the
 * first post-join frame arrives. Rejections stop the feed for good, a
 * rate_limited frame's retry_after is honoured, and retries are capped.
 */
export class RelayFeed {
  private ws: WebSocket | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private heartbeat: ReturnType<typeof setInterval> | undefined;
  private backoff: number;
  private attempts = 0;
  private closed = false;
  private joined = false;
  private nextDelay: number | undefined;
  private stopReason: string | undefined;
  private readonly opts: Required<FeedOptions>;
  connected = false;
  state: FeedState = { kind: "connecting" };

  constructor(
    readonly url: string,
    private onMessage: (frame: string) => void,
    private onState: (state: FeedState) => void = () => {},
    options: FeedOptions = {}
  ) {
    this.opts = {
      allowApprovalWait: false,
      maxAttempts: 8,
      initialBackoffMs: 1000,
      maxBackoffMs: 60_000,
      heartbeatMs: 30_000,
      rateLimitFallbackMs: 60_000,
      ...options,
    };
    this.backoff = this.opts.initialBackoffMs;
  }

  get stopped(): boolean {
    return this.state.kind === "stopped";
  }

  private setState(state: FeedState): void {
    this.state = state;
    this.onState(state);
  }

  /** Stop for good (until restart()); the socket is closed. */
  private stop(reason: string): void {
    this.stopReason = reason;
    this.clearTimers();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
  }

  start(): void {
    if (this.closed) return;
    this.joined = false;
    this.nextDelay = undefined;
    this.stopReason = undefined;
    this.attempts++;
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this.ws = undefined;
      this.setState({ kind: "stopped", reason: `invalid relay URL ${this.url}: ${(e as Error).message}` });
      return;
    }
    this.ws = ws;
    this.setState({ kind: "connecting" });
    let alive = true;
    ws.on("open", () => {
      ws.send(JSON.stringify({ type: "join", name: null, agent: "unknown", protocol_version: PROTOCOL_VERSION }));
      if (this.opts.heartbeatMs > 0) {
        this.heartbeat = setInterval(() => {
          if (!alive) {
            ws.terminate();
            return;
          }
          alive = false;
          try {
            ws.ping();
          } catch {
            /* close follows */
          }
        }, this.opts.heartbeatMs);
      }
    });
    ws.on("pong", () => (alive = true));
    ws.on("message", (data) => {
      if (this.ws !== ws) return;
      alive = true;
      const text = data.toString();
      let type: string | undefined;
      let msg: any;
      try {
        msg = JSON.parse(text);
        type = typeof msg?.type === "string" ? msg.type : undefined;
      } catch {
        /* not JSON: treat as traffic */
      }
      if (type && HANDSHAKE_FRAMES.has(type)) {
        this.handleHandshake(type, msg);
        return;
      }
      if (!this.joined) {
        this.joined = true;
        this.attempts = 0;
        this.backoff = this.opts.initialBackoffMs;
        this.connected = true;
        this.setState({ kind: "live" });
      }
      this.onMessage(text);
    });
    ws.on("error", () => {
      /* "close" follows */
    });
    ws.on("close", (code: number) => {
      if (this.ws !== ws) return; // superseded by restart()
      this.ws = undefined;
      this.clearHeartbeat();
      this.connected = false;
      if (this.closed) return;
      if (this.stopReason) {
        this.setState({ kind: "stopped", reason: this.stopReason });
        return;
      }
      if (code === WS_CLOSE_ROOM_NOT_FOUND) return this.setState({ kind: "stopped", reason: "room not found on the relay" });
      if (code === WS_CLOSE_JOIN_REFUSED) return this.setState({ kind: "stopped", reason: "the room refused this editor" });
      if (code === WS_CLOSE_VERSION_TOO_OLD) return this.setState({ kind: "stopped", reason: "the relay rejected this protocol version" });
      if (code === WS_CLOSE_RATE_LIMITED && this.nextDelay === undefined) this.nextDelay = this.opts.rateLimitFallbackMs;
      if (this.attempts >= this.opts.maxAttempts) {
        this.setState({ kind: "stopped", reason: `gave up after ${this.attempts} attempts` });
        return;
      }
      const delay = this.nextDelay ?? this.backoff;
      this.backoff = Math.min(this.backoff * 2, this.opts.maxBackoffMs);
      this.setState({ kind: "retrying", inMs: delay, reason: this.nextDelay !== undefined ? "rate limited" : undefined });
      this.timer = setTimeout(() => this.start(), delay);
    });
  }

  private handleHandshake(type: string, msg: any): void {
    switch (type) {
      case "rate_limited": {
        const secs = Number(msg?.retry_after);
        this.nextDelay = Number.isFinite(secs) && secs > 0 ? Math.ceil(secs) * 1000 : this.opts.rateLimitFallbackMs;
        return;
      }
      case "room_rejected":
        // Room missing, approval declined or timed out, or queue full: never auto-retry.
        return this.stop(String(msg?.reason || "the room rejected this editor"));
      case "version_rejected":
        return this.stop(String(msg?.reason || "the relay rejected this protocol version"));
      case "join_pending":
        if (!this.opts.allowApprovalWait) return this.stop("this room requires an owner's approval");
        this.setState({ kind: "pending" });
        return;
      default:
        return; // join_resolved / name_rejected: the next frame decides
    }
  }

  /** Reset the attempt budget and connect again (e.g. a user-initiated reconnect). */
  restart(): void {
    if (this.closed) return;
    this.clearTimers();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = undefined;
    this.attempts = 0;
    this.backoff = this.opts.initialBackoffMs;
    this.start();
  }

  private clearHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = undefined;
  }

  private clearTimers(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    this.clearHeartbeat();
  }

  dispose(): void {
    this.closed = true;
    this.clearTimers();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.connected = false;
  }
}
