import * as fs from "fs";
import * as vscode from "vscode";
import { ActivityTracker, initials, tooltip } from "./activity";
import {
  DaemonClient,
  DaemonStatus,
  EventAck,
  RepoStatus,
  describeAck,
  pathsOverlap,
  relativeTo,
  repoForPath,
  statusText,
} from "./daemonClient";
import { EditGate, KeyedDebounce } from "./editGate";
import { RelayFeed, roomUrl } from "./relayFeed";

const DOCS_URL = "https://github.com/synergo/synergo#readme";
const POLL_MS = 5000;

let status: DaemonStatus | undefined;
let client: DaemonClient;
const feeds = new Map<string, { feed: RelayFeed; tracker: ActivityTracker; repo: RepoStatus }>();
const gate = new EditGate(60_000);
const debounce = new KeyedDebounce(400);

function cfg() {
  return vscode.workspace.getConfiguration("synergo");
}

function port(): number {
  const inspected = cfg().inspect<number>("port");
  const explicit = inspected?.workspaceValue ?? inspected?.globalValue ?? inspected?.workspaceFolderValue;
  if (explicit) return explicit;
  const env = Number(process.env.SYNERGO_PORT);
  return Number.isFinite(env) && env > 0 ? env : cfg().get<number>("port", 4600);
}

function sessionId(): string {
  return `vscode-${vscode.env.machineId}`;
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
  const base = cfg().get<string>("dashboardUrl", "http://localhost:5173").replace(/\/+$/, "");
  return repo ? `${base}/r/${encodeURIComponent(repo.room)}` : base;
}

class PeerDecorations implements vscode.FileDecorationProvider {
  private emitter = new vscode.EventEmitter<vscode.Uri[] | undefined>();
  readonly onDidChangeFileDecorations = this.emitter.event;

  refresh(uris?: vscode.Uri[]) {
    this.emitter.fire(uris);
  }

  provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
    if (uri.scheme !== "file") return undefined;
    for (const { tracker, repo } of feeds.values()) {
      const rel = relativeTo(repo.root, uri.fsPath);
      if (rel === uri.fsPath.replace(/\\/g, "/")) continue;
      const t = tracker.get(rel);
      if (t) {
        return new vscode.FileDecoration(initials(t.userId), tooltip(t), new vscode.ThemeColor("charts.orange"));
      }
    }
    return undefined;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  client = new DaemonClient(port());
  const decorations = new PeerDecorations();
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  bar.command = "synergo.menu";
  bar.text = "Synergo: offline";
  bar.show();
  context.subscriptions.push(bar, vscode.window.registerFileDecorationProvider(decorations));

  const uriFor = (repo: RepoStatus, rel: string) => vscode.Uri.file(`${repo.root.replace(/[\\/]+$/, "")}/${rel}`);

  const syncFeeds = () => {
    const wanted = new Map<string, RepoStatus>();
    for (const repo of status?.repos ?? []) {
      if (!repo.relay || !repo.room) continue;
      const inWorkspace = (vscode.workspace.workspaceFolders ?? []).some((f) => pathsOverlap(repo.root, f.uri.fsPath));
      if (inWorkspace) wanted.set(roomUrl(repo.relay, repo.room) + "|" + repo.root, repo);
    }
    for (const [key, entry] of feeds) {
      if (!wanted.has(key)) {
        entry.feed.dispose();
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
      const feed = new RelayFeed(roomUrl(repo.relay, repo.room), (frame) => {
        const changed = tracker.ingest(frame);
        if (changed.length) decorations.refresh(changed.map((rel) => uriFor(repo, rel)));
      });
      feeds.set(key, { feed, tracker, repo });
      feed.start();
    }
  };

  const poll = async () => {
    client.port = port();
    try {
      status = await client.status();
    } catch {
      status = undefined;
    }
    const repo = activeRepo();
    bar.text = `$(organization) ${statusText(status, repo)}`;
    bar.tooltip = repo?.fatal ? `Synergo: ${repo.fatal}` : status ? `Synergo daemon v${status.version} on port ${status.port}` : "Synergo daemon not reachable — click to start it";
    syncFeeds();
    for (const { tracker, repo: r } of feeds.values()) {
      const gone = tracker.prune();
      if (gone.length) decorations.refresh(gone.map((rel) => uriFor(r, rel)));
    }
  };
  void poll();
  const timer = setInterval(poll, POLL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  const showAck = async (ack: EventAck, repo: RepoStatus, rel: string) => {
    if (ack.decision === "allow") return false;
    const msg = `Synergo ${ack.decision === "block" ? "conflict" : "heads-up"} on ${rel}: ${describeAck(ack)}`;
    const pick = await vscode.window.showWarningMessage(msg, "Open dashboard");
    if (pick === "Open dashboard") void vscode.env.openExternal(vscode.Uri.parse(dashboardUrl(repo)));
    return true;
  };

  const preEdit = async (doc: vscode.TextDocument, explicit: boolean) => {
    const repo = repoForPath(status, doc.uri.fsPath);
    const rel = repo ? relativeTo(repo.root, doc.uri.fsPath) : doc.uri.fsPath;
    if (!repo) {
      if (explicit) void vscode.window.showInformationMessage("Synergo: this file is not in a repo the daemon is watching (is `synergo up` running?).");
      return;
    }
    try {
      const ack = await client.postEvent({
        kind: "pre_edit",
        agent: "unknown",
        session_id: sessionId(),
        cwd: repo.root,
        file_path: doc.uri.fsPath,
        tool: "vscode",
      });
      const warned = await showAck(ack, repo, rel);
      if (!warned && explicit) void vscode.window.showInformationMessage(`Synergo: ${rel} is clear — nobody else is on it.`);
    } catch (e) {
      if (explicit) void vscode.window.showErrorMessage(`Synergo: daemon unreachable (${(e as Error).message}).`);
    }
  };

  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((e) => {
      const doc = e.document;
      if (doc.uri.scheme !== "file" || !e.contentChanges.length) return;
      const key = doc.uri.toString();
      if (!gate.hasBaseline(key) && cfg().get<boolean>("shareHumanEdits", true)) {
        try {
          gate.rememberBaseline(key, fs.readFileSync(doc.uri.fsPath, "utf8"));
        } catch {
          gate.rememberBaseline(key, "");
        }
      }
      if (!cfg().get<boolean>("warnOnEdit", true) || !repoForPath(status, doc.uri.fsPath)) return;
      debounce.run(key, () => {
        if (gate.shouldCheck(key)) void preEdit(doc, false);
      });
    }),
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      const key = doc.uri.toString();
      const old = gate.takeBaseline(key);
      if (old === undefined || !cfg().get<boolean>("shareHumanEdits", true)) return;
      const repo = repoForPath(status, doc.uri.fsPath);
      const text = doc.getText();
      if (!repo || old === text) return;
      try {
        await client.postEvent({
          kind: "post_edit",
          agent: "unknown",
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
      if (e.affectsConfiguration("synergo")) void poll();
    }),
    vscode.window.onDidChangeActiveTextEditor(() => void poll()),
    { dispose: () => debounce.dispose() }
  );

  const whosHere = async () => {
    const repo = activeRepo();
    if (!repo) {
      void vscode.window.showInformationMessage(status ? "Synergo: no room for this workspace." : "Synergo: daemon offline.");
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
    const pick = await vscode.window.showQuickPick(items, { title: `Synergo room ${repo.room} — ${items.length} online`, placeHolder: "Pick to open the dashboard" });
    if (pick) void vscode.env.openExternal(vscode.Uri.parse(dashboardUrl(repo)));
  };

  const up = () => {
    const term = vscode.window.createTerminal({ name: "Synergo", cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath });
    term.show();
    term.sendText("synergo up");
    setTimeout(() => void poll(), 3000);
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("synergo.openDashboard", () => vscode.env.openExternal(vscode.Uri.parse(dashboardUrl(activeRepo())))),
    vscode.commands.registerCommand("synergo.whosHere", whosHere),
    vscode.commands.registerCommand("synergo.up", up),
    vscode.commands.registerCommand("synergo.checkFile", (uri?: vscode.Uri) => {
      const doc = uri ? vscode.workspace.textDocuments.find((d) => d.uri.toString() === uri.toString()) : vscode.window.activeTextEditor?.document;
      if (doc) return preEdit(doc, true);
      if (uri) return vscode.workspace.openTextDocument(uri).then((d) => preEdit(d, true));
      void vscode.window.showInformationMessage("Synergo: open a file first.");
    }),
    vscode.commands.registerCommand("synergo.menu", async () => {
      const repo = activeRepo();
      const items: (vscode.QuickPickItem & { run: () => unknown })[] = [];
      if (repo) items.push({ label: "$(globe) Open dashboard", description: dashboardUrl(repo), run: () => vscode.commands.executeCommand("synergo.openDashboard") });
      if (repo) items.push({ label: "$(person) Show peers", description: `${(repo.peers?.length ?? 0) + 1} online`, run: whosHere });
      if (vscode.window.activeTextEditor) items.push({ label: "$(shield) Check this file", run: () => vscode.commands.executeCommand("synergo.checkFile") });
      items.push({ label: "$(terminal) Run `synergo up`", description: status ? "daemon already running" : "start the daemon", run: up });
      items.push({ label: "$(book) Open docs", run: () => vscode.env.openExternal(vscode.Uri.parse(DOCS_URL)) });
      const pick = await vscode.window.showQuickPick(items, { title: bar.text.replace(/^\$\([^)]*\)\s*/, "") });
      await pick?.run();
    })
  );
}

export function deactivate(): void {
  for (const { feed } of feeds.values()) feed.dispose();
  feeds.clear();
}
