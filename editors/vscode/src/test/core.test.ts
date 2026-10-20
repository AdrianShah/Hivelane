import { test } from "node:test";
import assert from "node:assert/strict";
import * as http from "http";
import { AddressInfo } from "net";
import { WebSocketServer } from "ws";
import { ActivityTracker, initials, tooltip } from "../activity";
import { DaemonClient, describeAck, pathsOverlap, relativeTo, repoForPath, statusText, DaemonStatus } from "../daemonClient";
import { EditGate } from "../editGate";
import { RelayFeed, roomUrl } from "../relayFeed";

const STATUS: DaemonStatus = {
  version: "0.9.0",
  pid: 1,
  port: 0,
  mode: "multi",
  repos: [
    { root: "C:\\work\\app", room: "ROOM1", relay: "ws://x", name: "me", connected: true, peers: ["sam", "ana"] },
    { root: "C:\\work\\app\\sub", room: "ROOM2", relay: "ws://x", name: "me", connected: false, peers: [] },
  ],
};

function mockDaemon(handler: (req: http.IncomingMessage, body: string) => [number, unknown]) {
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const [code, payload] = handler(req, body);
      res.writeHead(code, { "content-type": "application/json" });
      res.end(JSON.stringify(payload));
    });
  });
  return new Promise<{ server: http.Server; port: number }>((r) => server.listen(0, "127.0.0.1", () => r({ server, port: (server.address() as AddressInfo).port })));
}

test("status() and postEvent() talk to the daemon", async () => {
  const seen: any[] = [];
  const { server, port } = await mockDaemon((req, body) => {
    assert.equal(req.headers.origin, undefined);
    if (req.method === "GET" && req.url === "/status") return [200, STATUS];
    if (req.method === "POST" && req.url === "/events") {
      seen.push(JSON.parse(body));
      return [200, { decision: "warn", reason: "recent edit", file_path: "a.ts", others: [{ user_id: "sam", agent: "claude-code", seconds_ago: 12, prompt: "refactor auth" }] }];
    }
    return [404, {}];
  });
  try {
    const c = new DaemonClient(port);
    const s = await c.status();
    assert.equal(s.repos[0].room, "ROOM1");
    const ack = await c.postEvent({ kind: "pre_edit", agent: "unknown", session_id: "vscode-x", cwd: "C:\\work\\app", file_path: "C:\\work\\app\\a.ts" });
    assert.equal(ack.decision, "warn");
    assert.equal(seen[0].kind, "pre_edit");
    assert.equal(seen[0].session_id, "vscode-x");
    assert.match(describeAck(ack), /sam \(claude-code\) 12 s ago: "refactor auth"\. recent edit/);
  } finally {
    server.close();
  }
});

test("daemon errors and offline are rejected", async () => {
  const { server, port } = await mockDaemon(() => [422, { detail: "bad" }]);
  await assert.rejects(new DaemonClient(port).postEvent({ kind: "intent", agent: "unknown", session_id: "s", cwd: "/" }), /422/);
  server.close();
  await assert.rejects(new DaemonClient(port, "127.0.0.1", 500).status());
});

test("repo selection, paths and status text", () => {
  assert.equal(repoForPath(STATUS, "C:/work/app/sub/x.ts")?.room, "ROOM2");
  // Case-insensitive file systems (Windows, macOS) also match a differently cased path.
  const folds = process.platform === "win32" || process.platform === "darwin";
  assert.equal(repoForPath(STATUS, "c:/work/app/sub/x.ts")?.room, folds ? "ROOM2" : undefined);
  assert.equal(repoForPath(STATUS, "C:\\work\\app\\x.ts")?.room, "ROOM1");
  assert.equal(repoForPath(STATUS, "C:\\work\\apple\\x.ts"), undefined);
  assert.equal(relativeTo("C:\\work\\app", "C:\\work\\app\\src\\a.ts"), "src/a.ts");
  assert.ok(pathsOverlap("C:\\work\\app", "C:/work"));
  assert.ok(!pathsOverlap("C:\\work\\app", "C:\\work\\apple"));
  assert.equal(statusText(undefined, undefined), "Synergo: offline");
  assert.equal(statusText(STATUS, STATUS.repos[0]), "Synergo: ROOM1 · 3 online");
  assert.equal(statusText(STATUS, STATUS.repos[1]), "Synergo: ROOM2 · connecting");
});

test("activity tracker ingests feeds, ignores self, expires", () => {
  // Folding on explicitly so the test means the same on every platform; the
  // per-platform default is covered by "path case folding is per platform".
  const t = new ActivityTracker("me", 60, true);
  const changed = t.ingest(
    JSON.stringify({
      type: "synergo_feed",
      events: [
        { type: "intent_update", user_id: "sam", agent: "claude-code", prompt: "fix login", timestamp: 100 },
        { type: "activity_update", user_id: "sam", agent: "claude-code", timestamp: 100, changes: [{ file_path: "src/Auth.ts", diff: "" }] },
        { type: "activity_update", user_id: "me", timestamp: 100, changes: [{ file_path: "src/mine.ts", diff: "" }] },
        { type: "user_joined", user_id: "ana", agent: "cursor" },
      ],
    }),
    100
  );
  assert.deepEqual(changed, ["src/auth.ts"]);
  const touch = t.get("src\\Auth.ts", undefined, 112)!;
  assert.equal(touch.prompt, "fix login");
  assert.equal(tooltip(touch, 112), "sam (claude-code) edited 12 s ago: fix login");
  assert.equal(t.get("src/mine.ts", undefined, 100), undefined);
  assert.deepEqual([...t.online.keys()].sort(), ["ana", "me", "sam"]);
  t.ingest({ type: "user_left", user_id: "ana" });
  assert.ok(!t.online.has("ana"));
  assert.equal(t.get("src/auth.ts", undefined, 161), undefined);
  assert.deepEqual(t.prune(161), ["src/auth.ts"]);
  assert.deepEqual(t.ingest("not json"), []);
});

test("initials", () => {
  assert.equal(initials("sam"), "SA");
  assert.equal(initials("sam-lee"), "SL");
  assert.equal(initials(""), "?");
});

test("edit gate throttles per file and keeps first baseline", () => {
  const g = new EditGate(60_000);
  assert.ok(g.shouldCheck("a", 0));
  assert.ok(!g.shouldCheck("a", 30_000));
  assert.ok(g.shouldCheck("b", 30_000));
  assert.ok(g.shouldCheck("a", 60_001));
  g.rememberBaseline("a", "v1");
  g.rememberBaseline("a", "v2");
  assert.equal(g.takeBaseline("a"), "v1");
  assert.equal(g.takeBaseline("a"), undefined);
});

test("relay feed joins as spectator and receives frames", async () => {
  const wss = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  await new Promise((r) => wss.once("listening", r));
  const port = (wss.address() as AddressInfo).port;
  let joinedPath = "";
  wss.on("connection", (sock, req) => {
    joinedPath = req.url ?? "";
    sock.once("message", (m) => {
      const join = JSON.parse(m.toString());
      assert.equal(join.type, "join");
      assert.equal(join.name, null);
      sock.send(JSON.stringify({ type: "synergo_feed", events: [{ type: "user_joined", user_id: "sam" }] }));
    });
  });
  const frames: string[] = [];
  const feed = new RelayFeed(roomUrl(`ws://127.0.0.1:${port}/`, "ROOM1"), (f) => frames.push(f));
  feed.start();
  const deadline = Date.now() + 3000;
  while (!frames.length && Date.now() < deadline) await new Promise((r) => setTimeout(r, 20));
  feed.dispose();
  await new Promise((r) => wss.close(r));
  assert.equal(joinedPath, "/ROOM1");
  assert.match(frames[0], /user_joined/);
});
