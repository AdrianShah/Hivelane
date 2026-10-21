import * as http from "http";

export interface RepoStatus {
  root: string;
  room: string;
  relay: string;
  name?: string | null;
  agent?: string;
  connected?: boolean;
  peers?: string[];
  queued_events?: number;
  repo_id?: string;
  fatal?: string | null;
  [key: string]: unknown;
}

export interface DaemonStatus {
  version: string;
  pid: number;
  port: number;
  mode?: string;
  repos: RepoStatus[];
}

export type EventKind = "intent" | "pre_edit" | "post_edit";

export interface NormalizedEvent {
  kind: EventKind;
  agent: string;
  session_id: string;
  cwd: string;
  prompt?: string;
  file_path?: string;
  old_content?: string;
  new_content?: string;
  tool?: string;
}

export interface PeerTouch {
  user_id: string;
  agent?: string;
  seconds_ago?: number;
  prompt?: string | null;
}

export interface EventAck {
  decision: "allow" | "warn" | "block";
  reason: string;
  file_path?: string | null;
  others: PeerTouch[];
}

export class DaemonClient {
  constructor(public port: number, private host = "127.0.0.1", private timeoutMs = 3000) {}

  private request<T>(method: string, path: string, body?: unknown): Promise<T> {
    return new Promise((resolve, reject) => {
      const data = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
      const req = http.request(
        {
          host: this.host,
          port: this.port,
          method,
          path,
          headers: data ? { "content-type": "application/json", "content-length": data.length } : {},
          timeout: this.timeoutMs,
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => {
            const text = Buffer.concat(chunks).toString("utf8");
            if ((res.statusCode ?? 500) >= 400) {
              reject(new Error(`daemon ${method} ${path} -> ${res.statusCode}: ${text.slice(0, 200)}`));
              return;
            }
            try {
              resolve(JSON.parse(text) as T);
            } catch (e) {
              reject(e);
            }
          });
        }
      );
      req.on("timeout", () => req.destroy(new Error("daemon request timed out")));
      req.on("error", reject);
      if (data) req.write(data);
      req.end();
    });
  }

  status(): Promise<DaemonStatus> {
    return this.request<DaemonStatus>("GET", "/status");
  }

  async postEvent(event: NormalizedEvent): Promise<EventAck> {
    const ack = await this.request<Partial<EventAck>>("POST", "/events", event);
    return { decision: ack.decision ?? "allow", reason: ack.reason ?? "", file_path: ack.file_path, others: ack.others ?? [] };
  }
}

const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();

/** Choose the repo binding that contains `fsPath` (longest root wins). */
export function repoForPath(status: DaemonStatus | undefined, fsPath: string): RepoStatus | undefined {
  if (!status) return undefined;
  const target = norm(fsPath);
  let best: RepoStatus | undefined;
  for (const repo of status.repos ?? []) {
    const root = norm(repo.root);
    if (target === root || target.startsWith(root + "/")) {
      if (!best || root.length > norm(best.root).length) best = repo;
    }
  }
  return best;
}

/** True if either path contains the other (repo inside workspace folder or vice versa). */
export function pathsOverlap(a: string, b: string): boolean {
  const x = norm(a);
  const y = norm(b);
  return x === y || x.startsWith(y + "/") || y.startsWith(x + "/");
}

export function relativeTo(root: string, fsPath: string): string {
  const r = root.replace(/\\/g, "/").replace(/\/+$/, "");
  const p = fsPath.replace(/\\/g, "/");
  return p.toLowerCase().startsWith(r.toLowerCase() + "/") ? p.slice(r.length + 1) : p;
}

export function statusText(status: DaemonStatus | undefined, repo: RepoStatus | undefined): string {
  if (!status) return "Synergo: offline";
  if (!repo) return "Synergo: no room";
  if (!repo.connected) return `Synergo: ${repo.room} · connecting`;
  const online = (repo.peers?.length ?? 0) + 1;
  return `Synergo: ${repo.room} · ${online} online`;
}

export function describeAck(ack: EventAck): string {
  if (!ack.others.length) return ack.reason || ack.decision;
  const who = ack.others
    .map((o) => {
      const ago = o.seconds_ago !== undefined ? ` ${Math.round(o.seconds_ago)} s ago` : "";
      const intent = o.prompt ? `: "${truncate(o.prompt, 80)}"` : "";
      const agent = o.agent && o.agent !== "unknown" ? ` (${o.agent})` : "";
      return `${o.user_id}${agent}${ago}${intent}`;
    })
    .join("; ");
  return ack.reason ? `${who}. ${ack.reason}` : who;
}

export function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
