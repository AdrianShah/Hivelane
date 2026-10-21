import { test } from "node:test";
import assert from "node:assert/strict";
import { AddressInfo } from "net";
import WebSocket, { WebSocketServer } from "ws";
import { ActivityTracker, normalizeRel } from "../activity";
import {
  DEFAULT_DASHBOARD,
  DEFAULT_DASHBOARD_LOCAL,
  dashboardBase,
  feedPlan,
  isValidToken,
  relayIsLocal,
  resolvePort,
  roomLink,
  statusText,
} from "../daemonClient";
import { EditGate } from "../editGate";
import { FeedState, RelayFeed, normalizeRoomCode, roomUrl } from "../relayFeed";

type Behaviour = (sock: WebSocket, attempt: number) => void;

async function relay(behaviour: Behaviour) {
  const wss = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  await new Promise((r) => wss.once("listening", r));
  const port = (wss.address() as AddressInfo).port;
  const times: number[] = [];
  wss.on("connection", (sock) => {
    times.push(Date.now());
    const attempt = times.length;
    sock.once("message", () => behaviour(sock, attempt));
  });
  return {
    url: roomUrl(`ws://127.0.0.1:${port}`, "ROOM1"),
    times,
    close: () => new Promise((r) => wss.close(r)),
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function until(pred: () => boolean, ms = 3000) {
  const deadline = Date.now() + ms;
  while (!pred() && Date.now() < deadline) await sleep(10);
}

test("accept-then-close does not reset backoff, and retries are capped", async () => {
  // The relay accepts every socket and closes it without a join (the storm case).
  const r = await relay((sock) => sock.close(1000));
  const states: FeedState[] = [];
  const feed = new RelayFeed(r.url, () => {}, (s) => states.push(s), { initialBackoffMs: 20, maxBackoffMs: 1000, maxAttempts: 4, heartbeatMs: 0 });
  feed.start();
  await until(() => feed.stopped);
  feed.dispose();
  await r.close();
  assert.equal(r.times.length, 4);
  const delays = states.filter((s): s is Extract<FeedState, { kind: "retrying" }> => s.kind === "retrying").map((s) => s.inMs);
  assert.deepEqual(delays, [20, 40, 80]);
  assert.match((feed.state as any).reason, /gave up after 4/);
});

test("room_rejected (room not found, declined) stops for good", async () => {
  const r = await relay((sock) => {
    sock.send(JSON.stringify({ type: "room_rejected", reason: "Room not found." }));
    sock.close(4404);
  });
  const feed = new RelayFeed(r.url, () => assert.fail("rejections are not traffic"), () => {}, { initialBackoffMs: 10, heartbeatMs: 0 });
  feed.start();
  await until(() => feed.stopped);
  await sleep(100);
  feed.dispose();
  await r.close();
  assert.equal(r.times.length, 1);
  assert.deepEqual(feed.state, { kind: "stopped", reason: "Room not found." });
});

test("close codes 4404/4403/4426 stop even without a frame", async () => {
  for (const code of [4404, 4403, 4426]) {
    const r = await relay((sock) => sock.close(code));
    const feed = new RelayFeed(r.url, () => {}, () => {}, { initialBackoffMs: 10, heartbeatMs: 0 });
    feed.start();
    await until(() => feed.stopped);
    await sleep(60);
    feed.dispose();
    await r.close();
    assert.equal(r.times.length, 1, `code ${code}`);
  }
});

test("rate_limited retry_after is honoured", async () => {
  const r = await relay((sock, attempt) => {
    if (attempt === 1) {
      sock.send(JSON.stringify({ type: "rate_limited", reason: "slow down", retry_after: 1 }));
      sock.close(4429);
    } else {
      sock.send(JSON.stringify({ type: "user_joined", user_id: "sam" }));
    }
  });
  const states: FeedState[] = [];
  const feed = new RelayFeed(r.url, () => {}, (s) => states.push(s), { initialBackoffMs: 10, heartbeatMs: 0 });
  feed.start();
  await until(() => feed.state.kind === "live", 4000);
  feed.dispose();
  await r.close();
  assert.equal(r.times.length, 2);
  assert.ok(r.times[1] - r.times[0] >= 950, `waited ${r.times[1] - r.times[0]} ms`);
  assert.ok(states.some((s) => s.kind === "retrying" && s.inMs === 1000 && s.reason === "rate limited"));
});

test("approval-required room: stop by default instead of waiting as a spectator", async () => {
  const r = await relay((sock) => sock.send(JSON.stringify({ type: "join_pending", request_id: "r1", reason: "needs approval" })));
  const feed = new RelayFeed(r.url, () => {}, () => {}, { initialBackoffMs: 10, heartbeatMs: 0 });
  feed.start();
  await until(() => feed.stopped);
  await sleep(60);
  feed.dispose();
  await r.close();
  assert.equal(r.times.length, 1);
  assert.match((feed.state as any).reason, /approval/);
});

test("approval opt-in waits, goes live on admission, and never retries a decline", async () => {
  const r = await relay((sock, attempt) => {
    sock.send(JSON.stringify({ type: "join_pending", request_id: "r1", reason: "needs approval" }));
    setTimeout(() => {
      if (attempt === 1) {
        sock.send(JSON.stringify({ type: "join_resolved", request_id: "r1", outcome: "approved" }));
        sock.send(JSON.stringify({ type: "user_joined", user_id: "sam" }));
      }
    }, 30);
  });
  const frames: string[] = [];
  const states: string[] = [];
  const feed = new RelayFeed(r.url, (f) => frames.push(f), (s) => states.push(s.kind), { allowApprovalWait: true, heartbeatMs: 0 });
  feed.start();
  await until(() => feed.state.kind === "live");
  feed.dispose();
  await r.close();
  assert.ok(states.includes("pending"));
  assert.equal(frames.length, 1);
  assert.match(frames[0], /user_joined/);

  const r2 = await relay((sock) => {
    sock.send(JSON.stringify({ type: "join_pending", request_id: "r2", reason: "needs approval" }));
    sock.send(JSON.stringify({ type: "room_rejected", reason: "declined" }));
    sock.close(4403);
  });
  const feed2 = new RelayFeed(r2.url, () => {}, () => {}, { allowApprovalWait: true, initialBackoffMs: 10, heartbeatMs: 0 });
  feed2.start();
  await until(() => feed2.stopped);
  await sleep(60);
  feed2.dispose();
  await r2.close();
  assert.equal(r2.times.length, 1);
});

test("backoff resets only after a successful join", async () => {
  const r = await relay((sock, attempt) => {
    if (attempt === 3) {
      sock.send(JSON.stringify({ type: "user_joined", user_id: "sam" }));
      setTimeout(() => sock.close(1001), 20);
    } else sock.close(1000);
  });
  const delays: number[] = [];
  const feed = new RelayFeed(r.url, () => {}, (s) => s.kind === "retrying" && delays.push(s.inMs), { initialBackoffMs: 20, maxAttempts: 3, heartbeatMs: 0 });
  feed.start();
  await until(() => r.times.length >= 5 || feed.stopped);
  feed.dispose();
  await r.close();
  assert.deepEqual(delays.slice(0, 3), [20, 40, 20]);
});

test("restart() resets a stopped feed", async () => {
  let reject = true;
  const r = await relay((sock) => {
    if (reject) {
      sock.send(JSON.stringify({ type: "room_rejected", reason: "nope" }));
      sock.close(4404);
    } else sock.send(JSON.stringify({ type: "user_joined", user_id: "sam" }));
  });
  const feed = new RelayFeed(r.url, () => {}, () => {}, { heartbeatMs: 0 });
  feed.start();
  await until(() => feed.stopped);
  reject = false;
  feed.restart();
  await until(() => feed.state.kind === "live");
  feed.dispose();
  await r.close();
  assert.equal(feed.state.kind, "live");
});

test("a malformed relay URL stops the feed instead of throwing", () => {
  const feed = new RelayFeed("not a url", () => {}, () => {}, { heartbeatMs: 0 });
  assert.doesNotThrow(() => feed.start());
  assert.equal(feed.state.kind, "stopped");
  feed.dispose();
});

test("heartbeat terminates a half-open socket", async () => {
  const wss = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  await new Promise((r) => wss.once("listening", r));
  const port = (wss.address() as AddressInfo).port;
  let connections = 0;
  wss.on("connection", (sock) => {
    connections++;
    sock.once("message", () => {
      sock.send(JSON.stringify({ type: "user_joined", user_id: "sam" }));
      // Stop reading: pings go unanswered, like a socket that went half-open.
      (sock as any)._socket.pause();
    });
  });
  const states: string[] = [];
  const feed = new RelayFeed(`ws://127.0.0.1:${port}/ROOM1`, () => {}, (s) => states.push(s.kind), { heartbeatMs: 40, initialBackoffMs: 10 });
  feed.start();
  await until(() => states.includes("retrying"), 2000);
  feed.dispose();
  for (const c of wss.clients) c.terminate();
  await new Promise((r) => wss.close(r));
  assert.ok(states.includes("live"));
  assert.ok(states.includes("retrying"), states.join(","));
  assert.ok(connections >= 1);
});

test("room codes normalize like the dashboard", () => {
  assert.equal(normalizeRoomCode(" k7m2-pq2x "), "K7M2PQ2X");
  assert.equal(normalizeRoomCode("k7m2 pq2x"), "K7M2PQ2X");
  assert.equal(roomUrl("wss://relay.example/ws/", "ab-cd"), "wss://relay.example/ws/ABCD");
});

test("dashboard URL follows the CLI and carries ?relay=", () => {
  assert.equal(dashboardBase("", "wss://relay.example/ws", {}), DEFAULT_DASHBOARD);
  assert.equal(DEFAULT_DASHBOARD, "https://adrianshah.github.io/Hivelane");
  assert.equal(dashboardBase("", "ws://localhost:8000/ws", {}), DEFAULT_DASHBOARD_LOCAL);
  assert.equal(dashboardBase("", "wss://relay.example/ws", { HIVELANE_DASHBOARD: "https://dash.example/" }), "https://dash.example");
  assert.equal(dashboardBase("https://mine.example/", "wss://relay.example/ws", { HIVELANE_DASHBOARD: "https://dash.example" }), "https://mine.example");
  assert.ok(relayIsLocal("ws://127.0.0.1:8000/ws"));
  assert.ok(!relayIsLocal("wss://relay.example/ws"));
  assert.equal(
    roomLink(DEFAULT_DASHBOARD, "k7m2-pq2x", "wss://relay.example/ws"),
    "https://adrianshah.github.io/Hivelane/r/K7M2PQ2X?relay=wss%3A%2F%2Frelay.example%2Fws"
  );
  // The relay serving its own dashboard: no parameter needed.
  assert.equal(roomLink("https://relay.example", "ROOM", "wss://relay.example/ws"), "https://relay.example/r/ROOM");
  assert.equal(roomLink("https://x.example", "ROOM"), "https://x.example/r/ROOM");
});

test("feed plan skips fatal, e2e and approval rooms", () => {
  const base = { root: "/r", room: "R", relay: "wss://x/ws" };
  assert.deepEqual(feedPlan(base, false), { open: true });
  assert.equal(feedPlan({ ...base, fatal: "room expired" }, false).open, false);
  assert.match(feedPlan({ ...base, e2e: true }, false).reason!, /encrypted/);
  assert.equal(feedPlan({ ...base, require_approval: true }, false).open, false);
  assert.equal(feedPlan({ ...base, require_approval: true }, true).open, true);
  assert.equal(feedPlan({ ...base, relay: "" }, false).open, false);
});

test("port precedence and validation", () => {
  assert.equal(resolvePort({ workspaceFolderValue: 1, workspaceValue: 2, globalValue: 3 }, { HIVELANE_PORT: "4" }), 1);
  assert.equal(resolvePort({ workspaceValue: 2, globalValue: 3 }, {}), 2);
  assert.equal(resolvePort({ globalValue: 3.5 }, { HIVELANE_PORT: "4601" }), 4601);
  assert.equal(resolvePort(undefined, { HIVELANE_PORT: "nope" }), 4600);
  assert.equal(resolvePort({ globalValue: 70000 }, {}), 4600);
});

test("token validation matches the daemon", () => {
  assert.ok(isValidToken("a".repeat(32)));
  assert.ok(isValidToken("ab=c.d/e+f".padEnd(40, "x")));
  assert.ok(!isValidToken("a".repeat(31)));
  assert.ok(!isValidToken("a b".padEnd(40, "x")));
});

test("auth problem shows token mismatch, not offline", () => {
  assert.equal(statusText(undefined, undefined, true), "Hivelane: token mismatch");
  assert.equal(statusText(undefined, undefined, false), "Hivelane: offline");
});

test("tracker keys touches by repo_id", () => {
  const t = new ActivityTracker("me", 60, true);
  t.ingest({ type: "activity_update", user_id: "sam", repo_id: "B", timestamp: 100, changes: [{ file_path: "src/index.ts" }] });
  assert.equal(t.get("src/index.ts", "A", 101), undefined);
  assert.equal(t.get("src/index.ts", "B", 101)?.userId, "sam");
  t.ingest({ type: "activity_update", user_id: "ana", repo_id: "A", timestamp: 102, changes: [{ file_path: "src/index.ts" }] });
  assert.equal(t.get("src/index.ts", "A", 103)?.userId, "ana");
  assert.equal(t.get("src/index.ts", "B", 103)?.userId, "sam");
  // Peers that send no repo_id still count everywhere.
  t.ingest({ type: "activity_update", user_id: "old", timestamp: 104, changes: [{ file_path: "a.ts" }] });
  assert.equal(t.get("a.ts", "A", 105)?.userId, "old");
  assert.deepEqual(t.paths().sort(), ["a.ts", "src/index.ts"]);
  // e2e frames carry no usable file_path.
  assert.deepEqual(t.ingest({ type: "activity_update", user_id: "x", e2e: "blob", changes: [{ file_path: "", e2e: "blob" }] }), []);
});

test("path case folding is per platform", () => {
  assert.equal(normalizeRel("src\\Foo.ts", false), "src/Foo.ts");
  assert.equal(normalizeRel("src\\Foo.ts", true), "src/foo.ts");
  const t = new ActivityTracker("me", 60, false);
  t.ingest({ type: "activity_update", user_id: "sam", timestamp: 1, changes: [{ file_path: "Foo.ts" }] });
  assert.equal(t.get("foo.ts", undefined, 2), undefined);
  assert.ok(t.get("Foo.ts", undefined, 2));
});

test("edit gate prunes old checks and replaces pending baselines", () => {
  const g = new EditGate(1000);
  for (let i = 0; i < 300; i++) g.shouldCheck(`k${i}`, 0);
  g.shouldCheck("late", 5000);
  assert.equal((g as any).lastCheck.size, 1);
  g.rememberBaseline("a", "");
  g.replaceBaseline("a", "disk text");
  assert.equal(g.takeBaseline("a"), "disk text");
  g.replaceBaseline("a", "ignored once taken");
  assert.equal(g.takeBaseline("a"), undefined);
});
