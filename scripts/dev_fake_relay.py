"""A fake Hivelane relay for working on the dashboard without agents.

Serves one room over the real wire format (frontend/src/types.ts): a
spectator joins, gets a catch-up (settings, policy, members, intents, plans
and ~45 minutes of back-dated history so the heat map and timeline have
something to show), then a live stream of activity, intents, warn/block
decisions, conflict alerts and feedback tallies every couple of seconds.

    .venv/Scripts/python scripts/dev_fake_relay.py --port 8765
    # then, in frontend/:
    VITE_WS_URL=ws://localhost:8765/ws npm run dev
    # open http://localhost:5173/r/DEMO2345

Development only: no auth, no persistence, one scripted team.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

ROOM = "DEMO2345"

TEAM: list[tuple[str, str]] = [
    ("ada", "claude-code"),
    ("sam", "cursor"),
    ("lin", "codex"),
    ("raj", "gemini-cli"),
    ("mo", "aider"),
]

FILES: dict[str, list[str]] = {
    "src/auth/login.ts": ["login", "validateCredentials"],
    "src/auth/session.ts": ["createSession", "refreshToken"],
    "src/auth/oauth/callback.ts": ["handleCallback"],
    "src/api/client.ts": ["request", "retryWithBackoff"],
    "src/components/Navbar.tsx": ["Navbar", "<imports>"],
    "src/components/Settings.tsx": ["SettingsForm"],
    "src/utils/format.ts": ["formatBytes", "formatDuration"],
    "server/room.py": ["Room.join", "Room.find_overlaps"],
    "server/main.py": ["websocket_endpoint"],
    "server/store.py": ["RedisStore.save"],
    "tests/test_room.py": ["test_join_twice"],
    "docs/README.md": [],
}

INTENTS = {
    "ada": ["Add OAuth login with GitHub", "Fix the token refresh race", "Write tests for room joins"],
    "sam": ["Refactor the API client retries", "Make the navbar responsive", "Tidy session cookies"],
    "lin": ["Speed up overlap detection in the relay", "Persist rooms in Redis"],
    "raj": ["Polish the settings page", "Document the CLI flags"],
    "mo": ["Format durations consistently"],
}

CAPTIONS = [
    "Extracted the retry policy into a helper and added jitter.",
    "Guarded the refresh path against concurrent requests.",
    "Split the callback handler and validated the state parameter.",
    "Replaced the linear scan with an index by file path.",
    "Moved the settings form to controlled inputs.",
    "Added a test for joining the same room twice.",
]

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def diff_for(path: str, symbol: str, seed: int) -> str:
    rnd = random.Random(seed)
    start = rnd.randint(8, 120)
    name = symbol.split(".")[-1] if symbol and not symbol.startswith("<") else "value"
    if path.endswith(".py"):
        body = [
            f" def {name}(self, *args):",
            f"-    return self._{name}_legacy(*args)",
            f"+    result = self._{name}(*args)",
            "+    self._metrics.observe(result)",
            "+    return result",
            "",
        ]
    elif path.endswith(".md"):
        body = [" ## Usage", "-Run `hivelane up`.", "+Run `hivelane up --room <code>` in each repo.", "+"]
    else:
        body = [
            f" export function {name}(input: Input) {{",
            f"-  return legacy{name.capitalize()}(input);",
            f"+  const result = {name}Impl(input, {{ retries: {rnd.randint(2, 5)} }});",
            "+  log.debug('done', result.id);",
            "+  return result;",
            " }",
        ]
    count_old = sum(1 for l in body if not l.startswith("+"))
    count_new = sum(1 for l in body if not l.startswith("-"))
    header = [f"--- a/{path}", f"+++ b/{path}", f"@@ -{start},{count_old} +{start},{count_new} @@"]
    return "\n".join(header + body)


def activity(user: str, agent: str, ts: float, paths: list[str], prompt: str | None) -> dict[str, Any]:
    changes = []
    for i, path in enumerate(paths):
        symbols = FILES.get(path, [])[:2]
        changes.append(
            {
                "file_path": path,
                "diff": diff_for(path, symbols[0] if symbols else "", int(ts) + i),
                "symbols": symbols,
                "line_ranges": [[10, 18]],
            }
        )
    return {
        "type": "activity_update",
        "user_id": user,
        "timestamp": ts,
        "prompt": prompt,
        "changes": changes,
        "caption": random.choice(CAPTIONS),
        "agent": agent,
        "branch": "main" if user != "lin" else "relay-index",
    }


def decision(user: str, agent: str, ts: float, path: str, kind: str, others: list[str], scope: str) -> dict[str, Any]:
    reason = (
        f"{', '.join(others)} edited {path} {random.randint(4, 40)}s ago"
        if kind == "block"
        else f"{', '.join(others)} touched {path} recently ({scope})"
    )
    return {
        "type": "edit_decision",
        "user_id": user,
        "file_path": path,
        "decision": kind,
        "others": others,
        "reason": reason,
        "timestamp": ts,
        "agent": agent,
        "scope": scope,
        "decision_id": uuid.uuid4().hex[:12],
        "suggestion": "src/auth/session.ts is free right now" if kind == "block" else "",
    }


def conflict(users: list[str], ts: float, path: str) -> dict[str, Any]:
    return {
        "type": "conflict_alert",
        "users": users,
        "file_path": path,
        "verdict": random.choice(["overlapping", "related"]),
        "explanation": f"Both changed {FILES.get(path, ['this file'])[0] if FILES.get(path) else 'this file'} in {path}",
        "timestamp": ts,
    }


def intent(user: str, agent: str, ts: float, prompt: str) -> dict[str, Any]:
    return {"type": "intent_update", "user_id": user, "prompt": prompt, "timestamp": ts, "agent": agent}


def plan(user: str, agent: str, ts: float, summary: str, files: list[str]) -> dict[str, Any]:
    return {
        "type": "plan_update",
        "user_id": user,
        "agent": agent,
        "summary": summary,
        "files": files,
        "symbols": [],
        "branch": "main",
        "timestamp": ts,
        "expires_at": ts + 3600,
    }


def history(now: float) -> list[dict[str, Any]]:
    """~45 minutes of back-dated events, oldest first."""
    events: list[dict[str, Any]] = []
    agents = dict(TEAM)
    t = now - 45 * 60
    paths = list(FILES)
    rnd = random.Random(7)
    for user, agent in TEAM:
        events.append(intent(user, agent, t, INTENTS[user][0]))
    while t < now - 60:
        t += rnd.uniform(25, 70)
        user = rnd.choice([u for u, _ in TEAM])
        agent = agents[user]
        roll = rnd.random()
        if roll < 0.62:
            touched = rnd.sample(paths, rnd.choice([1, 1, 2]))
            events.append(activity(user, agent, t, touched, rnd.choice(INTENTS[user])))
        elif roll < 0.8:
            # A block followed by a move to another file: "prevented".
            path = rnd.choice(paths[:6])
            other = rnd.choice([u for u, _ in TEAM if u != user])
            events.append(decision(user, agent, t, path, "block", [other], rnd.choice(["symbol", "lines", "file"])))
            alt = rnd.choice([p for p in paths if p != path])
            events.append(activity(user, agent, t + rnd.uniform(15, 80), [alt], None))
            t += 80
        elif roll < 0.9:
            # A warning the agent edited through: "edited anyway".
            path = rnd.choice(paths)
            other = rnd.choice([u for u, _ in TEAM if u != user])
            events.append(decision(user, agent, t, path, "warn", [other], rnd.choice(["lines", "file", "intent"])))
            events.append(activity(user, agent, t + 5, [path], None))
            t += 10
        else:
            a, b = rnd.sample([u for u, _ in TEAM], 2)
            events.append(conflict([a, b], t, rnd.choice(paths[:8])))
    events.sort(key=lambda e: e["timestamp"])
    return events


async def live(socket: WebSocket) -> None:
    agents = dict(TEAM)
    paths = list(FILES)
    for tick in itertools.count():
        await asyncio.sleep(random.uniform(1.2, 2.4))
        now = time.time()
        user = random.choice(["ada", "sam", "lin", "raj"])
        agent = agents[user]
        roll = random.random()
        events: list[dict[str, Any]] = []
        if roll < 0.55:
            events.append(activity(user, agent, now, random.sample(paths, 1), random.choice(INTENTS[user])))
        elif roll < 0.68:
            events.append(intent(user, agent, now, random.choice(INTENTS[user])))
        elif roll < 0.82:
            other = random.choice([u for u in ("ada", "sam", "lin", "raj") if u != user])
            events.append(
                decision(user, agent, now, random.choice(paths[:5]), random.choice(["warn", "block"]), [other], random.choice(["symbol", "lines", "file", "plan"]))
            )
        elif roll < 0.9:
            a, b = random.sample(["ada", "sam", "lin", "raj"], 2)
            events.append(conflict([a, b], now, random.choice(paths[:6])))
        else:
            events.append(
                {"type": "decision_feedback_update", "decision_id": "seed", "useful_count": tick, "noise_count": tick // 4}
            )
        await socket.send_text(json.dumps({"type": "hivelane_feed", "events": events}))


@app.post("/rooms")
async def create_room() -> dict[str, str]:
    return {"room_id": ROOM, "owner_token": "dev-owner-token"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/{room_id}")
async def room_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    try:
        await socket.receive_text()  # the spectator's join
        now = time.time()

        async def send(frame: dict[str, Any]) -> None:
            await socket.send_text(json.dumps(frame))

        await send(
            {
                "type": "room_settings",
                "block_window_seconds": 20,
                "warn_window_seconds": 60,
                "set_by": None,
                "timestamp": now,
            }
        )
        await send({"type": "room_policy_update", "require_approval": False})
        for user, agent in TEAM:
            await send({"type": "user_joined", "user_id": user, "agent": agent})
        await send(plan("ada", "claude-code", now - 600, "OAuth login", ["src/auth/login.ts", "src/auth/oauth/callback.ts", "src/auth/oauth/state.ts", "tests/test_oauth.py"]))
        await send(plan("lin", "codex", now - 900, "Index overlaps by path", ["server/room.py", "server/store.py", "server/index.py"]))
        await send({"type": "hivelane_feed", "events": history(now)})
        # mo's daemon drops off: an offline, greyed-out lane.
        await send({"type": "user_left", "user_id": "mo"})

        listener = asyncio.create_task(drain(socket))
        streamer = asyncio.create_task(live(socket))
        done, pending = await asyncio.wait({listener, streamer}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass


async def drain(socket: WebSocket) -> None:
    """Votes from the dashboard: answer with a tally so the buttons update."""
    tallies: dict[str, list[int]] = {}
    while True:
        raw = await socket.receive_text()
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if frame.get("type") == "decision_feedback":
            tally = tallies.setdefault(frame["decision_id"], [0, 0])
            tally[0 if frame.get("useful") else 1] += 1
            await socket.send_text(
                json.dumps(
                    {
                        "type": "decision_feedback_update",
                        "decision_id": frame["decision_id"],
                        "useful_count": tally[0],
                        "noise_count": tally[1],
                    }
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
