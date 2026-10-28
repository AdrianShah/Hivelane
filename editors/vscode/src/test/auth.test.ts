import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "fs";
import * as http from "http";
import * as os from "os";
import * as path from "path";
import { AddressInfo } from "net";
import { DaemonClient, DaemonUnverifiedError, TOKEN_HEADER, daemonProof, defaultTokenPath, readToken } from "../daemonClient";

const GOOD = "g".repeat(43);

function tmpToken(value: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "hivelane-token-"));
  const file = path.join(dir, "token");
  fs.writeFileSync(file, value, "ascii");
  return file;
}

/** A daemon that only answers requests carrying `accepted()`'s token. */
function guardedDaemon(accepted: () => string) {
  const seen: (string | undefined)[] = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://x");
    if (url.pathname === "/health") {
      // The daemon's proof: only a holder of accepted()'s token can give it.
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, proof: daemonProof(accepted(), url.searchParams.get("nonce") ?? "") }));
      return;
    }
    const token = req.headers[TOKEN_HEADER] as string | undefined;
    seen.push(token);
    const ok = token === accepted();
    res.writeHead(ok ? 200 : 401, { "content-type": "application/json" });
    res.end(JSON.stringify(ok ? { version: "1", pid: 1, port: 0, repos: [] } : { detail: "missing or wrong X-Hivelane-Token" }));
  });
  return new Promise<{ server: http.Server; port: number; seen: (string | undefined)[] }>((r) =>
    server.listen(0, "127.0.0.1", () => r({ server, port: (server.address() as AddressInfo).port, seen }))
  );
}

test("token path honours HIVELANE_HOME, and only real tokens are read", () => {
  assert.equal(defaultTokenPath({ HIVELANE_HOME: "/x/home" }, "/u"), path.join("/x/home", "token"));
  assert.equal(defaultTokenPath({}, "/u"), path.join("/u", ".hivelane", "token"));
  assert.equal(readToken(tmpToken(`${GOOD}\n`)), GOOD);
  assert.equal(readToken(tmpToken("short")), undefined);
  assert.equal(readToken(tmpToken("not a token\r\nX-Evil: 1")), undefined);
  assert.equal(readToken(path.join(os.tmpdir(), "no-such-dir-hivelane", "token")), undefined);
});

test("the token is sent, and re-read once when the daemon refuses it", async () => {
  const file = tmpToken(GOOD);
  let current = GOOD;
  const { server, port, seen } = await guardedDaemon(() => current);
  try {
    const client = new DaemonClient(port, "127.0.0.1", 2000, file);
    assert.equal((await client.status()).version, "1");
    assert.deepEqual(seen, [GOOD]);

    // The daemon's token was regenerated on disk: one retry picks it up, and
    // the stale token is never sent (the proof fails first).
    current = "n".repeat(43);
    fs.writeFileSync(file, current, "ascii");
    assert.equal((await client.status()).version, "1");
    assert.deepEqual(seen.slice(1), [current]);

    // A daemon that cannot prove our token (another HIVELANE_HOME, or another
    // program on the port): a clear error, no retry storm, and nothing sent.
    current = "z".repeat(43);
    await assert.rejects(client.status(), (e: unknown) => e instanceof DaemonUnverifiedError && /tokenPath/.test((e as Error).message));
    assert.equal(seen.length, 2);
  } finally {
    server.close();
  }
});

test("a squatter on the daemon port gets neither the token nor the event", async () => {
  const file = tmpToken(GOOD);
  const requests: string[] = [];
  // Another program on the port: answers everything, can prove nothing.
  const server = http.createServer((req, res) => {
    requests.push(`${req.method} ${req.url} ${req.headers[TOKEN_HEADER] ?? ""}`);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, decision: "allow", reason: "", others: [] }));
  });
  const port = await new Promise<number>((r) => server.listen(0, "127.0.0.1", () => r((server.address() as AddressInfo).port)));
  try {
    const client = new DaemonClient(port, "127.0.0.1", 2000, file);
    await assert.rejects(
      client.postEvent({ kind: "post_edit", agent: "unknown", session_id: "s", cwd: "/", file_path: "/a", new_content: "secret" }),
      (e: unknown) => e instanceof DaemonUnverifiedError
    );
    assert.ok(requests.length > 0 && requests.every((r) => r.startsWith("GET /health?nonce=") && !r.includes(GOOD)));
  } finally {
    server.close();
  }
});
