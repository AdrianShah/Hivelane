import * as vscode from "vscode";
import { ActivityTracker, initials, tooltip } from "./activity";
import {
  DaemonAuthError,
  DaemonClient,
  DaemonStatus,
  EventAck,
  RepoStatus,
  dashboardBase,
  describeAck,
  feedPlan,
  pathsOverlap,
  relativeTo,
  repoForPath,
  resolvePort,
  roomLink,
  statusText,
} from "./daemonClient";
import { EditGate, KeyedDebounce } from "./editGate";
import { FeedState, RelayFeed, roomUrl } from "./relayFeed";

// TODO(owner): keep in sync with package.json "repository" before publishing.
const DOCS_URL = "https://github.com/AdrianShah/Hivelane#readme";
const POLL_MS = 5000;

let status: DaemonStatus | undefined;
// Set when the daemon answers but refuses our token (a different HIVELANE_HOME).
let authProblem: string | undefined;
let client: DaemonClient;
const feeds = new Map<string, { feed: RelayFeed; tracker: ActivityTracker; repo: RepoStatus }>();
// Why a repo in this workspace has no live feed (keyed by repo root).
const feedNotes = new Map<string, string>();
const gate = new EditGate(60_000);
const debounce = new KeyedDebounce(400);
const pendingBaselines = new Map<string, Promise<void>>();
const pendingTimers = new Set<ReturnType<typeof setTimeout>>();

function cfg(resource?: vscode.Uri) {
  return vscode.workspace.getConfiguration("hivelane", resource ?? vscode.workspace.workspaceFolders?.[0]?.uri);
}

function port(): number {
  return resolvePort(cfg().inspect<number>("port"));
}

function tokenPath(): string | undefined {
  const configured = cfg().get<string>("tokenPath", "").trim();
  return configured || undefined;
}

/** One per editor window: windows must not share a session. */
function sessionId(): string {
  return `vscode-${vscode.env.sessionId || vscode.env.machineId}`;
}

function activeRepo(): RepoStatus | undefined {
  const uri = vscode.window.activeTextEditor?.document.uri ?? vscode.workspace.workspaceFolders?.[0]?.uri;
  if (status && uri) {
    const repo = repoForPath(status, uri.fsPath);
    if (repo) return repo;
  }
  for (const f of vscode.workspace.workspaceFolders ?? []) {
    const repo = repoForPath(status, f.uri.fsPath);
    if (repo) return repo;
  }
  return undefined;
}

function dashboardUrl(repo?: RepoStatus): string {
  const base = dashboardBase(cfg().get<string>("dashboardUrl", ""), repo?.relay);
  return repo ? roomLink(base, repo.room, repo.relay) : base;
}

function describeFeed(state: FeedState): string | undefined {
  switch (state.kind) {
    case "stopped":
      return `peer badges stopped: ${state.reason}`;
    case "pending":
      return "waiting for a room owner to approve this editor";
    case "retrying":
      return `relay feed reconnecting in ${Math.round(state.inMs / 1000)} s${state.reason ? ` (${state.reason})` : ""}`;
    default:
      return undefined;
  }
}

class PeerDecorations implements vscode.FileDecorationProvider, vscode.Disposable {
  private emitter = new vscode.EventEmitter<vscode.Uri[] | undefined>();
  readonly onDidChangeFileDecorations = this.emitter.event;

  refresh(uris?: vscode.Uri[]) {
    this.emitter.fire(uris);
  }

  provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
    if (uri.scheme !== "file") return undefined;
    for (const { tracker, repo } of feeds.values()) {
      const rel = relativeTo(repo.root, uri.fsPath);
      if (rel === uri.fsPath.replace(/\\/g, "/") || rel === uri.fsPath) continue;
      const t = tracker.get(rel, repo.repo_id || undefined);
      if (t) {
        return new vscode.FileDecoration(initials(t.userId), tooltip(t), new vscode.ThemeColor("charts.orange"));
      }
    }
    return undefined;
  }

  dispose() {
    this.emitter.dispose();
  }
}

export function activate(context: vscode.ExtensionContext): void {
  client = new DaemonClient(port(), undefined, undefined, tokenPath());
  const decorations = new PeerDecorations();
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  bar.command = "hivelane.menu";
  bar.text = "Hivelane: offline";
  bar.show();
  context.subscriptions.push(bar, decorations, vscode.window.registerFileDecorationProvider(decorations));

  const uriFor = (repo: RepoStatus, rel: string) => vscode.Uri.file(`${repo.root.replace(/[\\/]+$/, "")}/${rel}`);

  const render = () => {
    const repo = activeRepo();
    bar.text = `$(organization) ${statusText(status, repo, !!authProblem)}`;
    const note = repo ? feedNotes.get(repo.root) : undefined;
    const base = authProblem
      ? `Hivelane: ${authProblem}`
      : repo?.fatal
        ? `Hivelane: ${repo.fatal}`
        : status
          ? `Hivelane daemon v${status.version} on port ${status.port}`
          : "Hivelane daemon not reachable, click to start it";
    bar.tooltip = note ? `${base}\n${note}` : base;
  };

  const syncFeeds = () => {
    const allowApproval = cfg().get<boolean>("joinApprovalRooms", false);
    const wanted = new Map<string, RepoStatus>();
    const roots = new Set<string>();
    for (const repo of status?.repos ?? []) {
      const inWorkspace = (vscode.workspace.workspaceFolders ?? []).some((f) => pathsOverlap(repo.root, f.uri.fsPath));
      if (!inWorkspace) continue;
      roots.add(repo.root);
      const plan = feedPlan(repo, allowApproval);
      if (!plan.open) {
        if (plan.reason && plan.reason !== "no room") feedNotes.set(repo.root, plan.reason);
        else feedNotes.delete(repo.root);
        continue;
      }
      wanted.set(roomUrl(repo.relay, repo.room) + "|" + repo.root + "|" + allowApproval, repo);
    }
    for (const root of [...feedNotes.keys()]) if (!roots.has(root)) feedNotes.delete(root);
    for (const [key, entry] of feeds) {
      if (!wanted.has(key)) {
        entry.feed.dispose();
        const gone = entry.tracker.clear();
        if (gone.length) decorations.refresh(gone.map((rel) => uriFor(entry.repo, rel)));
        feeds.delete(key);
      }
    }
    for (const [key, repo] of wanted) {
      const existing = feeds.get(key);
      if (existing) {
        existing.repo = repo;
        existing.tracker.selfUserId = repo.name ?? undefined;
        continue;
      }
      const tracker = new ActivityTracker(repo.name ?? undefined, 60);
      const entry = { feed: undefined as unknown as RelayFeed, tracker, repo };
      entry.feed = new RelayFeed(
        roomUrl(repo.relay, repo.room),
        (frame) => {
          const changed = tracker.ingest(frame);
          if (changed.length) decorations.refresh(changed.map((rel) => uriFor(entry.repo, rel)));
        },
        (state) => {
          const note = describeFeed(state);
          if (note) feedNotes.set(entry.repo.root, note);
          else feedNotes.delete(entry.repo.root);
          render();
        },
        { allowApprovalWait: allowApproval }
      );
      feeds.set(key, entry);
      entry.feed.start();
    }
  };

  // One request at a time; a poll requested meanwhile runs once afterwards.
  let inFlight: Promise<void> | undefined;
  let again = false;
  const pollOnce = async () => {
    client.port = port();
    client.setTokenPath(tokenPath());
    try {
      status = await client.status();
      authProblem = undefined;
    } catch (e) {
      status = undefined;
      authProblem = e instanceof DaemonAuthError ? e.message : undefined;
    }
    syncFeeds();
    render();
    for (const { tracker, repo: r } of feeds.values()) {
      const gone = tracker.prune();
      if (gone.length) decorations.refresh(gone.map((rel) => uriFor(r, rel)));
    }
  };
  const poll = (): Promise<void> => {
    if (inFlight) {
      again = true;
      return inFlight;
    }
    inFlight = (async () => {
      try {
        do {
          again = false;
          await pollOnce();
        } while (again);
      } finally {
        inFlight = undefined;
      }
    })();
    return inFlight;
  };
  void poll();
  const timer = setInterval(() => void poll(), POLL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  const showAck = async (ack: EventAck, repo: RepoStatus, rel: string) => {
    if (ack.decision === "allow") return false;
    const msg = `Hivelane ${ack.decision === "block" ? "conflict" : "heads-up"} on ${rel}: ${describeAck(ack)}`;
    const pick = await vscode.window.showWarningMessage(msg, "Open dashboard");
    if (pick === "Open dashboard") void vscode.env.openExternal(vscode.Uri.parse(dashboardUrl(repo)));
    return true;
  };

  const preEdit = async (doc: vscode.TextDocument, explicit: boolean) => {
    const repo = repoForPath(status, doc.uri.fsPath);
    const rel = repo ? relativeTo(repo.root, doc.uri.fsPath) : doc.uri.fsPath;
    if (!repo) {
      if (explicit) void vscode.window.showInformationMessage("Hivelane: this file is not in a repo the daemon is watching (is `hivelane up` running?).");
      return;
    }
    try {
      const ack = await client.postEvent({
        kind: "pre_edit",
        agent: "human",
        session_id: sessionId(),
        cwd: repo.root,
        file_path: doc.uri.fsPath,
        tool: "vscode",
      });
      const warned = await showAck(ack, repo, rel);
      if (!warned && explicit) void vscode.window.showInformationMessage(`Hivelane: ${rel} is clear, nobody else is on it.`);
    } catch (e) {
      if (explicit) void vscode.window.showErrorMessage(`Hivelane: daemon unreachable (${(e as Error).message}).`);
    }
  };

  const readBaseline = async (uri: vscode.Uri): Promise<string> => {
    try {
      const bytes = await vscode.workspace.fs.readFile(uri);
      return Buffer.from(bytes).toString("utf8").replace(/^﻿/, "");
    } catch {
      return "";
    }
  };

  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((e) => {
      const doc = e.document;
      if (doc.uri.scheme !== "file" || !e.contentChanges.length) return;
      // A clean document changing is VS Code reloading it from disk (an agent
      // wrote it, git checkout/pull): not a human edit.
      if (!doc.isDirty) return;
      const key = doc.uri.toString();
      if (!gate.hasBaseline(key) && cfg(doc.uri).get<boolean>("shareHumanEdits", true)) {
        // The document was clean until this change, so disk still holds the pre-edit text.
        gate.rememberBaseline(key, "");
        pendingBaselines.set(
          key,
          readBaseline(doc.uri).then((text) => {
            gate.replaceBaseline(key, text);
            pendingBaselines.delete(key);
          })
        );
      }
      if (!cfg(doc.uri).get<boolean>("warnOnEdit", true) || !repoForPath(status, doc.uri.fsPath)) return;
      debounce.run(key, () => {
        if (gate.shouldCheck(key)) void preEdit(doc, false);
      });
    }),
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      const key = doc.uri.toString();
      await pendingBaselines.get(key);
      const old = gate.takeBaseline(key);
      if (old === undefined || !cfg(doc.uri).get<boolean>("shareHumanEdits", true)) return;
      const repo = repoForPath(status, doc.uri.fsPath);
      const text = doc.getText();
      if (!repo || old === text) return;
      try {
        await client.postEvent({
          kind: "post_edit",
          agent: "human",
          session_id: sessionId(),
          cwd: repo.root,
          file_path: doc.uri.fsPath,
          old_content: old,
          new_content: text,
          tool: "vscode",
        });
      } catch {
        /* daemon offline: nothing to share with */
      }
    }),
    vscode.workspace.onDidCloseTextDocument((doc) => gate.forget(doc.uri.toString())),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("hivelane")) void poll();
    }),
    // Re-render from the cached status; the interval keeps it fresh.
    vscode.window.onDidChangeActiveTextEditor(() => render()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => void poll()),
    { dispose: () => debounce.dispose() }
  );

  const whosHere = async () => {
    const repo = activeRepo();
    if (!repo) {
      void vscode.window.showInformationMessage(status ? "Hivelane: no room for this workspace." : "Hivelane: daemon offline.");
      return;
    }
    const entry = [...feeds.values()].find((f) => f.repo.root === repo.root);
    const names = new Set<string>(repo.peers ?? []);
    for (const id of entry?.tracker.online.keys() ?? []) names.add(id);
    const items: vscode.QuickPickItem[] = [
      { label: `$(account) ${repo.name ?? "you"} (you)`, description: repo.agent ?? "" },
      ...[...names]
        .filter((n) => n !== repo.name)
        .map((n) => ({
          label: `$(person) ${n}`,
          description: entry?.tracker.online.get(n) ?? "",
          detail: entry?.tracker.intentOf(n),
        })),
    ];
    const pick = await vscode.window.showQuickPick(items, { title: `Hivelane room ${repo.room}, ${items.length} online`, placeHolder: "Pick to open the dashboard" });
    if (pick) void vscode.env.openExternal(vscode.Uri.parse(dashboardUrl(repo)));
  };

  let upTerminal: vscode.Terminal | undefined;
  context.subscriptions.push(vscode.window.onDidCloseTerminal((t) => t === upTerminal && (upTerminal = undefined)));
  const up = () => {
    if (!upTerminal) {
      upTerminal = vscode.window.createTerminal({ name: "Hivelane", cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath });
      upTerminal.sendText("hivelane up");
    }
    upTerminal.show();
    const t = setTimeout(() => {
      pendingTimers.delete(t);
      void poll();
    }, 3000);
    pendingTimers.add(t);
  };

  const reconnectFeeds = () => {
    for (const { feed } of feeds.values()) feed.restart();
    void poll();
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("hivelane.openDashboard", () => vscode.env.openExternal(vscode.Uri.parse(dashboardUrl(activeRepo())))),
    vscode.commands.registerCommand("hivelane.whosHere", whosHere),
    vscode.commands.registerCommand("hivelane.up", up),
    vscode.commands.registerCommand("hivelane.reconnect", reconnectFeeds),
    vscode.commands.registerCommand("hivelane.checkFile", (uri?: vscode.Uri) => {
      const doc = uri ? vscode.workspace.textDocuments.find((d) => d.uri.toString() === uri.toString()) : vscode.window.activeTextEditor?.document;
      if (doc) return preEdit(doc, true);
      if (uri) return vscode.workspace.openTextDocument(uri).then((d) => preEdit(d, true));
      void vscode.window.showInformationMessage("Hivelane: open a file first.");
    }),
    vscode.commands.registerCommand("hivelane.menu", async () => {
      const repo = activeRepo();
      const items: (vscode.QuickPickItem & { run: () => unknown })[] = [];
      if (repo) items.push({ label: "$(globe) Open dashboard", description: dashboardUrl(repo), run: () => vscode.commands.executeCommand("hivelane.openDashboard") });
      if (repo) items.push({ label: "$(person) Show peers", description: `${(repo.peers?.length ?? 0) + 1} online`, run: whosHere });
      if (vscode.window.activeTextEditor) items.push({ label: "$(shield) Check this file", run: () => vscode.commands.executeCommand("hivelane.checkFile") });
      const note = repo ? feedNotes.get(repo.root) : undefined;
      if (note && feeds.size) items.push({ label: "$(refresh) Reconnect peer badges", description: note, run: reconnectFeeds });
      if (!status) items.push({ label: "$(terminal) Run `hivelane up`", description: "start the daemon", run: up });
      items.push({ label: "$(book) Open docs", run: () => vscode.env.openExternal(vscode.Uri.parse(DOCS_URL)) });
      const pick = await vscode.window.showQuickPick(items, { title: bar.text.replace(/^\$\([^)]*\)\s*/, "") });
      await pick?.run();
    })
  );
}

export function deactivate(): void {
  for (const t of pendingTimers) clearTimeout(t);
  pendingTimers.clear();
  for (const { feed } of feeds.values()) feed.dispose();
  feeds.clear();
  feedNotes.clear();
}
