import { truncate } from "./daemonClient";

export interface Touch {
  userId: string;
  agent: string;
  timestamp: number; // seconds since epoch
  prompt?: string;
  repoId?: string;
}

/** Tracks which repo-relative files peers touched recently, fed by relay feed messages. */
export class ActivityTracker {
  private files = new Map<string, Touch>();
  private intents = new Map<string, { prompt: string; agent: string }>();
  readonly online = new Map<string, string>(); // user_id -> agent

  constructor(
    public selfUserId: string | undefined = undefined,
    public windowSeconds = 60,
    /** Fold path case (Windows/macOS file systems); off on case-sensitive ones. */
    public foldCase = defaultFoldCase()
  ) {}

  /** Ingest a raw relay frame (JSON string or parsed). Returns changed (normalized) file paths. */
  ingest(frame: unknown, now = Date.now() / 1000): string[] {
    let msg: any = frame;
    if (typeof frame === "string") {
      try {
        msg = JSON.parse(frame);
      } catch {
        return [];
      }
    }
    if (!msg || typeof msg !== "object") return [];
    if (msg.type === "synergo_feed" && Array.isArray(msg.events)) {
      return msg.events.flatMap((e: unknown) => this.ingest(e, now));
    }
    const isSelf = (u: string) => !!this.selfUserId && u === this.selfUserId;
    switch (msg.type) {
      case "activity_update": {
        if (msg.user_id) this.online.set(msg.user_id, msg.agent ?? "unknown");
        if (isSelf(msg.user_id)) return [];
        const changed: string[] = [];
        const prompt = msg.prompt ?? msg.caption ?? this.intents.get(msg.user_id)?.prompt;
        for (const c of msg.changes ?? []) {
          if (!c?.file_path) continue;
          const rel = normalizeRel(c.file_path, this.foldCase);
          this.files.set(touchKey(msg.repo_id, rel), {
            userId: msg.user_id,
            agent: msg.agent ?? "unknown",
            timestamp: msg.timestamp ?? now,
            prompt: prompt ?? undefined,
            repoId: msg.repo_id,
          });
          changed.push(rel);
        }
        return changed;
      }
      case "intent_update":
        if (msg.user_id && msg.prompt) this.intents.set(msg.user_id, { prompt: msg.prompt, agent: msg.agent ?? "unknown" });
        if (msg.user_id) this.online.set(msg.user_id, msg.agent ?? "unknown");
        return [];
      case "user_joined":
        if (msg.user_id) this.online.set(msg.user_id, msg.agent ?? "unknown");
        return [];
      case "user_left":
        this.online.delete(msg.user_id);
        return [];
      default:
        return [];
    }
  }

  /** Remove expired entries; returns the paths that expired. */
  prune(now = Date.now() / 1000): string[] {
    const gone: string[] = [];
    for (const [k, t] of this.files) {
      if (now - t.timestamp > this.windowSeconds) {
        this.files.delete(k);
        gone.push(relOf(k));
      }
    }
    return gone;
  }

  /**
   * The latest touch on relPath. With repoId, only touches from that repo (or
   * from peers that sent no repo_id) count, so a multi-repo room does not
   * badge repo A's file for an edit in repo B.
   */
  get(relPath: string, repoId?: string, now = Date.now() / 1000): Touch | undefined {
    const rel = normalizeRel(relPath, this.foldCase);
    let t: Touch | undefined;
    if (repoId) {
      const own = this.files.get(touchKey(repoId, rel));
      const unscoped = this.files.get(touchKey(undefined, rel));
      t = own && unscoped ? (own.timestamp >= unscoped.timestamp ? own : unscoped) : own ?? unscoped;
    } else {
      for (const [k, v] of this.files) {
        if (relOf(k) === rel && (!t || v.timestamp > t.timestamp)) t = v;
      }
    }
    if (!t || now - t.timestamp > this.windowSeconds) return undefined;
    return t;
  }

  intentOf(userId: string): string | undefined {
    return this.intents.get(userId)?.prompt;
  }

  paths(): string[] {
    return [...new Set([...this.files.keys()].map((k) => relOf(k)))];
  }

  clear(): string[] {
    const p = this.paths();
    this.files.clear();
    this.intents.clear();
    this.online.clear();
    return p;
  }
}

export function defaultFoldCase(platform: string = process.platform): boolean {
  return platform === "win32" || platform === "darwin";
}

const SEP = "\u0000";

function touchKey(repoId: string | undefined, rel: string): string {
  return `${repoId ?? ""}${SEP}${rel}`;
}

function relOf(key: string): string {
  return key.slice(key.indexOf(SEP) + 1);
}

export function normalizeRel(p: string, foldCase = defaultFoldCase()): string {
  const s = p.replace(/\\/g, "/").replace(/^\.\//, "").replace(/^\/+/, "");
  return foldCase ? s.toLowerCase() : s;
}

export function initials(userId: string): string {
  const parts = userId.split(/[^A-Za-z0-9]+/).filter(Boolean);
  const s = parts.length >= 2 ? parts[0][0] + parts[1][0] : (parts[0] ?? "?").slice(0, 2);
  return s.toUpperCase().slice(0, 2);
}

export function tooltip(t: Touch, now = Date.now() / 1000): string {
  const ago = Math.max(0, Math.round(now - t.timestamp));
  const agent = t.agent && t.agent !== "unknown" ? ` (${t.agent})` : "";
  const intent = t.prompt ? `: ${truncate(t.prompt, 100)}` : "";
  return `${t.userId}${agent} edited ${ago} s ago${intent}`;
}
