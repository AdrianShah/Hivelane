import * as fs from "fs";
import * as http from "http";
import * as os from "os";
import * as path from "path";

/** Header the daemon checks on every route but /health (synergo/auth.py). */
export const TOKEN_HEADER = "x-synergo-token";

/** Where the daemon keeps its per-install token: $SYNERGO_HOME/token, else ~/.synergo/token. */
export function defaultTokenPath(env: NodeJS.ProcessEnv = process.env, home: string = os.homedir()): string {
  const base = env.SYNERGO_HOME && env.SYNERGO_HOME.trim() ? env.SYNERGO_HOME : path.join(home, ".synergo");
  return path.join(base, "token");
}

/** The token in `file`, or undefined when it is missing or not a token. */
export function readToken(file: string): string | undefined {
  try {
    const text = fs.readFileSync(file, "ascii").trim();
    return isValidToken(text) ? text : undefined;
  } catch {
    return undefined;
  }
}

/** Mirrors synergo/auth.py _valid: printable ASCII, no spaces, at least 32 characters. */
export function isValidToken(text: string): boolean {
  return /^[\x21-\x7e]{32,4096}$/.test(text);
}

export class DaemonAuthError extends Error {
  constructor(tokenFile: string) {
    super(`the Synergo daemon refused this editor's token (${tokenFile}); is it running under another SYNERGO_HOME? Set synergo.tokenPath.`);
    this.name = "DaemonAuthError";
  }
}

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
  e2e?: boolean;
  require_approval?: boolean;
  owner?: boolean;
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
  private token: string | undefined;
  tokenFile: string;

  /** `tokenPath` defaults to defaultTokenPath(); the token is read now and re-read once on a 401. */
  constructor(
    public port: number,
    private host = "127.0.0.1",
    private timeoutMs = 3000,
    tokenPath?: string
  ) {
    this.tokenFile = tokenPath || defaultTokenPath();
    this.token = readToken(this.tokenFile);
  }

  /** Point at another token file (the setting changed) and read it. */
  setTokenPath(tokenPath: string | undefined): void {
    const next = tokenPath || defaultTokenPath();
    if (next !== this.tokenFile) {
      this.tokenFile = next;
      this.token = readToken(next);
    }
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    try {
      return await this.once<T>(method, path, body);
    } catch (e) {
      if (!(e instanceof DaemonAuthError)) throw e;
      // The daemon may have regenerated its token since we read it.
      const fresh = readToken(this.tokenFile);
      if (!fresh || fresh === this.token) throw e;
      this.token = fresh;
      return this.once<T>(method, path, body);
    }
  }

  private once<T>(method: string, path: string, body?: unknown): Promise<T> {
    return new Promise((resolve, reject) => {
      const data = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
      const headers: Record<string, string | number> = data
        ? { "content-type": "application/json", "content-length": data.length }
        : {};
      if (this.token) headers[TOKEN_HEADER] = this.token;
      const req = http.request(
        {
          host: this.host,
          port: this.port,
          method,
          path,
          headers,
          timeout: this.timeoutMs,
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => {
            const text = Buffer.concat(chunks).toString("utf8");
            if (res.statusCode === 401) {
              reject(new DaemonAuthError(this.tokenFile));
              return;
            }
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

const foldCase = process.platform === "win32" || process.platform === "darwin";
const norm = (p: string) => {
  const s = p.replace(/\\/g, "/").replace(/\/+$/, "");
  return foldCase ? s.toLowerCase() : s;
};

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
  return norm(p).startsWith(norm(r) + "/") ? p.slice(r.length + 1) : p;
}

export function statusText(status: DaemonStatus | undefined, repo: RepoStatus | undefined, authProblem?: boolean): string {
  if (authProblem) return "Synergo: token mismatch";
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

export const DEFAULT_DASHBOARD = "https://adrianshah.github.io/Synergo";
export const DEFAULT_DASHBOARD_LOCAL = "http://localhost:5173";

function httpUrl(wsOrHttp: string): URL {
  return new URL(wsOrHttp.trim().replace(/^ws(s?):/i, "http$1:"));
}

/** Same test as synergo/cli.py relay_is_local. */
export function relayIsLocal(relay: string): boolean {
  let host = "";
  try {
    host = httpUrl(relay).hostname.toLowerCase().replace(/^\[|\]$/g, "");
  } catch {
    return false;
  }
  return ["localhost", "127.0.0.1", "::1", "0.0.0.0"].includes(host) || host.endsWith(".localhost");
}

/**
 * Dashboard base the way the CLI picks it: the synergo.dashboardUrl setting,
 * else $SYNERGO_DASHBOARD, else localhost:5173 for a local relay and the
 * hosted dashboard for any other.
 */
export function dashboardBase(setting: string | undefined, relay: string | undefined, env: NodeJS.ProcessEnv = process.env): string {
  const chosen = (setting ?? "").trim() || (env.SYNERGO_DASHBOARD ?? "").trim();
  if (chosen) return chosen.replace(/\/+$/, "");
  return relay && relayIsLocal(relay) ? DEFAULT_DASHBOARD_LOCAL : DEFAULT_DASHBOARD;
}

/**
 * Room link: <base>/r/ROOM, plus ?relay=<ws base> (as the dashboard's own
 * room links carry) unless the dashboard is served by that relay itself.
 */
export function roomLink(base: string, room: string, relay?: string): string {
  const code = room.trim().replace(/[\s-]+/g, "").toUpperCase();
  const link = `${base.replace(/\/+$/, "")}/r/${encodeURIComponent(code)}`;
  if (!relay) return link;
  try {
    if (httpUrl(relay).origin === new URL(base).origin) return link;
  } catch {
    return link;
  }
  return `${link}?relay=${encodeURIComponent(relay.trim().replace(/\/+$/, ""))}`;
}

/** Whether to open a spectator feed for a repo binding, and if not, why. */
export function feedPlan(repo: RepoStatus, allowApprovalWait: boolean): { open: boolean; reason?: string } {
  if (!repo.relay || !repo.room) return { open: false, reason: "no room" };
  if (repo.fatal) return { open: false, reason: repo.fatal };
  if (repo.e2e) return { open: false, reason: "peer badges are unavailable in end-to-end encrypted rooms" };
  if (repo.require_approval && !allowApprovalWait)
    return { open: false, reason: "this room requires an owner's approval; peer badges are off (enable synergo.joinApprovalRooms to request access)" };
  return { open: true };
}

export interface InspectedNumber {
  workspaceFolderValue?: unknown;
  workspaceValue?: unknown;
  globalValue?: unknown;
}

function validPort(v: unknown): number | undefined {
  const n = typeof v === "string" && v.trim() ? Number(v) : v;
  return typeof n === "number" && Number.isInteger(n) && n > 0 && n < 65536 ? n : undefined;
}

/** Folder setting, then workspace, then user, then $SYNERGO_PORT, then 4600. */
export function resolvePort(inspected: InspectedNumber | undefined, env: NodeJS.ProcessEnv = process.env): number {
  return (
    validPort(inspected?.workspaceFolderValue) ??
    validPort(inspected?.workspaceValue) ??
    validPort(inspected?.globalValue) ??
    validPort(env.SYNERGO_PORT) ??
    4600
  );
}
