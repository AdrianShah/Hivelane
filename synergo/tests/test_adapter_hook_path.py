"""`synergo hook <agent>` for every registered adapter runs through the
stdlib-only fast path, and multi-file events combine to the strictest ack."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from synergo import adapters, hook_fast
from synergo.tests.test_adapter_conformance import ACKS, load_fixture
from synergo.tests.test_hook_fast import HEAVY, SLOW_STDLIB, stub_daemon, unused_port  # noqa: F401

PROBE = r"""
import sys
before = set(sys.modules)
from synergo import cli
code = cli.main(["hook", sys.argv[1]])
loaded = sorted(set(sys.modules) - before)
sys.stderr.write("\nLOADED=" + repr(loaded) + "\nCODE=" + repr(code) + "\n")
"""


@pytest.mark.parametrize("agent", adapters.ids())
@pytest.mark.parametrize("scenario", ["allow", "block", "no-daemon"])
def test_hook_never_imports_the_daemon_stack(
    agent: str, scenario: str, stub_daemon: tuple[int, dict[str, Any]], tmp_path: Path  # noqa: F811
) -> None:
    port, state = stub_daemon
    (tmp_path / "room.py").write_text("def room():\n    pass\n", encoding="utf-8")
    payload, _ = load_fixture(agent, "pre_edit", tmp_path)
    if scenario == "block":
        state["ack"] = ACKS["block"]
    if scenario == "no-daemon":
        port = unused_port()

    env = dict(os.environ, SYNERGO_PORT=str(port), SYNERGO_HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-c", PROBE, agent],
        input=json.dumps(payload).encode(),
        env=env,
        capture_output=True,
        timeout=30,
    )
    stderr = result.stderr.decode(errors="replace")
    assert result.returncode == 0, stderr
    assert "CODE=0" in stderr
    loaded_line = next(line for line in stderr.splitlines() if line.startswith("LOADED="))
    roots = {name.split(".")[0] for name in ast.literal_eval(loaded_line.removeprefix("LOADED="))}
    assert not roots & HEAVY, f"hook imported {sorted(roots & HEAVY)}"
    assert not roots & SLOW_STDLIB, f"hook imported slow stdlib {sorted(roots & SLOW_STDLIB)}"
    other = {m for a in adapters.ids() if a != agent for m in [adapters.REGISTRY[a]]}
    assert not any(name in other for name in ast.literal_eval(loaded_line.removeprefix("LOADED=")))

    stdout = result.stdout.decode()
    adapter = adapters.load(agent)
    if scenario == "allow":
        # "" for most agents; Cursor reads empty output as a block, so it
        # answers an explicit allow instead.
        assert stdout == adapter.passthrough(payload)
    elif scenario == "block":
        data = json.loads(stdout)
        hso = data.get("hookSpecificOutput") or {}
        assert (
            hso.get("permissionDecision") == "deny"
            or data.get("decision") == "deny"
            or data.get("permission") == "deny"
        )
    else:
        (event,) = adapter.normalize(payload)
        expected = adapter.render_notice(event, hook_fast.OFFLINE_NOT_RUNNING).stdout
        assert json.loads(stdout) == json.loads(expected)


def _run(agent: str, payload: Any, port: int, monkeypatch: pytest.MonkeyPatch, home: Path) -> tuple[int, str]:
    monkeypatch.setenv("SYNERGO_HOME", str(home))
    raw = json.dumps(payload).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = hook_fast.run_agent(agent, port)
    return code, out.getvalue()


def test_codex_multi_file_patch_posts_each_file_and_strictest_wins(
    stub_daemon: tuple[int, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    port, state = stub_daemon
    (tmp_path / "room.py").write_text("def room():\n    pass\n", encoding="utf-8")
    payload, expect = load_fixture("codex", "pre_edit_multi", tmp_path)

    acks = iter([ACKS["allow"], ACKS["warn"], ACKS["block"], ACKS["allow"]])
    state["ack"] = ACKS["allow"]

    original_post = hook_fast.post_event
    seen: list[str] = []

    def post(event: dict[str, Any], p: int, timeout: float = 2.0) -> Any:
        seen.append(event["file_path"])
        state["ack"] = next(acks)
        return original_post(event, p, timeout)

    monkeypatch.setattr(hook_fast, "post_event", post)
    code, stdout = _run("codex", payload, port, monkeypatch, tmp_path / "home")

    assert code == 0
    assert len(seen) == len(expect) == len(state["events"])
    data = json.loads(stdout)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert os.path.normpath(str(tmp_path / "pkg" / "room.py")) in [os.path.normpath(p) for p in seen]


def test_unknown_agent_fails_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    code, stdout = _run("nope", {}, unused_port(), monkeypatch, tmp_path)
    assert (code, stdout) == (0, "")


@pytest.mark.parametrize("agent", adapters.ids())
def test_cli_routes_every_adapter_to_the_fast_path(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from synergo import cli

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(hook_fast, "run_agent", lambda a, p: calls.append((a, p)) or 0)
    monkeypatch.setattr(cli, "build_parser", lambda: pytest.fail("parser built on the hook path"))
    assert cli.main(["hook", agent, "--port", "4999"]) == 0
    assert calls == [(agent, 4999)]
    assert cli.main(["hook", agent, "--bogus"]) == 0  # bad flags fail open
