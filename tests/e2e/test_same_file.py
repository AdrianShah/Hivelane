"""Two agents in one file at once, end to end (real relay, real daemons, the
real Claude Code adapter): edits to different functions, methods, constants
or import lines go through (at most a warning), whether the agents share a
checkout or each have their own; the same function is still blocked, also
when both ask before either edit has been reported."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hivelane.adapters import claude_code

if TYPE_CHECKING:
    from .conftest import Cluster, DaemonClient

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git: path keys hang off the repo root"
)

PY = '''import json
from pathlib import Path

LIMIT = 100
WINDOW = 60


def alpha(x):
    total = 0
    for i in range(x):
        total += i
    return total


def beta(y):
    result = []
    for item in y:
        result.append(item * 2)
    return result


class Store:
    def load(self, key):
        return self.data[key]

    def save(self, key, value):
        self.data[key] = value


def gamma(z):
    if z > LIMIT:
        return LIMIT
    return z
'''

TS = '''import { readFile } from "fs";

export function alpha(x: number): number {
  let total = 0;
  for (let i = 0; i < x; i++) total += i;
  return total;
}

export function beta(y: number[]): number[] {
  return y.map((v) => v * 2);
}

export const gamma = (z: number): number => {
  return z > 100 ? 100 : z;
};
'''

CASES = [
    ("different functions, far apart", "far.py",
     [("    return total", "    return total + 1")], "Edit", [("    if z > LIMIT:", "    if z >= LIMIT:")], "pass"),
    ("adjacent functions", "adjacent.py",
     [("    return total", "    return total + 1")], "Edit", [("def beta(y):\n    result = []", "def beta(y):\n    result: list = []")], "pass"),
    ("A grows a function by 15 lines, B edits the next one", "grow.py",
     [("    return total", "    return total\n" + "".join(f"    # note {i}\n" for i in range(15)).rstrip("\n"))],
     "Edit", [("        result.append(item * 2)", "        result.append(item * 3)")], "pass"),
    ("two methods of one class", "methods.py",
     [("        return self.data[key]", "        return self.data.get(key)")], "Edit",
     [("        self.data[key] = value", "        self.data[key] = value  # saved")], "pass"),
    ("both add an import", "imports.py",
     [("import json\n", "import json\nimport os\n")], "Edit", [("from pathlib import Path\n", "from pathlib import Path\nimport sys\n")], "pass"),
    ("different module constants", "consts.py",
     [("LIMIT = 100", "LIMIT = 200")], "Edit", [("WINDOW = 60", "WINDOW = 30")], "pass"),
    ("A adds a new function at the end, B edits alpha", "append.py",
     [("    return z\n", "    return z\n\n\ndef delta(w):\n    return w\n")], "Edit",
     [("        total += i", "        total += i * 2")], "pass"),
    ("B edits two other functions in one MultiEdit", "multi.py",
     [("    return total", "    return total + 1")], "MultiEdit",
     [("        result.append(item * 2)", "        result.append(item * 3)"), ("    if z > LIMIT:", "    if z >= LIMIT:")], "pass"),
    ("B rewrites the whole file (Write)", "write.py",
     [("    return total", "    return total + 1")], "Write", [], "pass"),
    ("CONTROL: same function", "same.py",
     [("    return total", "    return total + 1")], "Edit", [("        total += i", "        total += i * 2")], "block"),
    ("TypeScript: different functions", "far.ts",
     [("  return total;", "  return total + 1;")], "Edit", [("  return y.map((v) => v * 2);", "  return y.map((v) => v * 3);")], "pass"),
    ("TypeScript: arrow-function const vs function", "arrow.ts",
     [("  return total;", "  return total + 1;")], "Edit", [("  return z > 100 ? 100 : z;", "  return z >= 100 ? 100 : z;")], "pass"),
]


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    # Two checkouts of one repository: the same origin, so the same repo_id.
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "https://example.invalid/acme/demo.git"], check=True)
    for _, file, *_ in CASES:
        (path / file).write_text(TS if file.endswith(".ts") else PY, encoding="utf-8")
    return path.resolve()


def _edit(client: DaemonClient, session: str, root: Path, file: str, tool: str, edits: list[tuple[str, str]], apply: bool) -> dict[str, Any]:
    path = root / file
    if tool == "Write":
        tool_input: dict[str, Any] = {"file_path": str(path), "content": path.read_text() + "\n# rewritten\n"}
    elif tool == "MultiEdit":
        tool_input = {"file_path": str(path), "edits": [{"old_string": o, "new_string": n} for o, n in edits]}
    else:
        ((old, new),) = edits
        tool_input = {"file_path": str(path), "old_string": old, "new_string": new}
    pre = {"hook_event_name": "PreToolUse", "session_id": session, "cwd": str(root), "tool_name": tool, "tool_input": tool_input}
    ack = client.post(claude_code.normalize_payload(pre))
    if apply and ack.get("decision") != "block":
        text = path.read_text()
        for old, new in edits:
            assert old in text, (file, old)
            text = text.replace(old, new, 1)
        path.write_text(text)
        client.post(claude_code.normalize_payload({**pre, "hook_event_name": "PostToolUse"}))
    return ack


@pytest.mark.parametrize("checkouts", ["two machines", "one checkout, two chats"])
def test_same_file_matrix(cluster: Cluster, tmp_path: Path, checkouts: str) -> None:
    room = cluster.create_room()
    a_root = _repo(tmp_path / "adrian")
    if checkouts == "two machines":
        b_root = _repo(tmp_path / "sam")
        a, b = cluster.start_daemons(room, [
            {"name": "adrian", "batch_seconds": 0.05, "root": str(a_root)},
            {"name": "sam", "agent": "cursor", "batch_seconds": 0.05, "root": str(b_root)},
        ])
        b_session = "sam-1"
    else:
        b_root = a_root
        (a,) = cluster.start_daemons(room, [{"name": "adrian", "batch_seconds": 0.05, "root": str(a_root)}])
        b, b_session = a, "chat-2"
    spectator = cluster.spectate(room)
    wrong = []
    for name, file, a_edits, b_tool, b_edits, expect in CASES:
        _edit(a, "chat-1", a_root, file, "Edit", a_edits, apply=True)
        spectator.wait_for(
            lambda e, f=file: e.get("type") == "activity_update" and any(c["file_path"] == f for c in e.get("changes", [])),
            what=f"A's edit of {file}",
        )
        ack = _edit(b, b_session, b_root, file, b_tool, b_edits, apply=False)
        blocked = ack.get("decision") == "block"
        if blocked != (expect == "block"):
            wrong.append((name, ack.get("decision"), ack.get("scope"), ack.get("reason")))
    assert not wrong, wrong


def test_two_agents_asking_at_once_about_one_function(cluster: Cluster, tmp_path: Path) -> None:
    room = cluster.create_room()
    a_root, b_root = _repo(tmp_path / "adrian"), _repo(tmp_path / "sam")
    a, b = cluster.start_daemons(room, [
        {"name": "adrian", "batch_seconds": 0.05, "root": str(a_root)},
        {"name": "sam", "agent": "cursor", "batch_seconds": 0.05, "root": str(b_root)},
    ])
    # Neither edit has been reported yet: the first passed check stands in for it.
    first = _edit(a, "chat-1", a_root, "same.py", "Edit", [("    return total", "    return total + 1")], apply=False)
    same = _edit(b, "sam-1", b_root, "same.py", "Edit", [("        total += i", "        total += i * 2")], apply=False)
    other = _edit(b, "sam-1", b_root, "same.py", "Edit", [("    if z > LIMIT:", "    if z >= LIMIT:")], apply=False)
    assert first["decision"] == "allow"
    assert (same["decision"], same["scope"]) == ("block", "symbol"), same
    assert other["decision"] != "block", other
