import WebSocket from "ws";

export const PROTOCOL_VERSION = 1;

export function roomUrl(relay: string, room: string): string {
  return `${relay.replace(/\/+$/, "")}/${encodeURIComponent(room)}`;
}

/** Spectator connection to a relay room with exponential-backoff reconnect. */
export class RelayFeed {
  private ws: WebSocket | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private backoff = 1000;
  private closed = false;
  connected = false;

  constructor(
    readonly url: string,
    private onMessage: (frame: string) => void,
    private onState: (connected: boolean) => void = () => {}
  ) {}

  start(): void {
    if (this.closed) return;
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.on("open", () => {
      this.backoff = 1000;
      this.connected = true;
      ws.send(JSON.stringify({ type: "join", name: null, protocol_version: PROTOCOL_VERSION }));
      this.onState(true);
    });
    ws.on("message", (data) => this.onMessage(data.toString()));
    ws.on("error", () => {
      /* "close" follows and schedules the reconnect */
    });
    ws.on("close", () => {
      const was = this.connected;
      this.connected = false;
      if (was) this.onState(false);
      if (this.closed) return;
      this.timer = setTimeout(() => this.start(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 30_000);
    });
  }

  dispose(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.connected = false;
  }
}
