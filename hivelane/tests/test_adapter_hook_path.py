"""`hivelane hook <agent>` for every registered adapter runs through the
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

from hivelane import adapters, hook_fast
from hivelane.tests.test_adapter_conformance import ACKS, load_fixture
from hivelane.tests.test_hook_fast import HEAVY, SLOW_STDLIB, stub_daemon, unused_port  # noqa: F401

PROBE = r"""
import sys
before = set(sys.modules)
from hivelane import cli
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
    home = tmp_path / "home"
    home.mkdir()
    # Reading the token file is on the hot path too: it must stay import-free.
    (home / "token").write_text("t" * 43, encoding="ascii")

    env = dict(os.environ, HIVELANE_PORT=str(port), HIVELANE_HOME=str(home))
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
    # The scenario really exercised the path it claims to, token included.
    if scenario != "no-daemon":
        assert state["tokens"] and set(state["tokens"]) == {"t" * 43}
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
    monkeypatch.setenv("HIVELANE_HOME", str(home))
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
    # Posting stops at the first block (finding 01 #33): the fourth file is
    # never checked, so it records no claim for an edit that will not happen.
    assert len(expect) == 4
    assert len(seen) == 3 == len(state["events"])
    data = json.loads(stdout)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert os.path.normpath(str(tmp_path / "pkg" / "room.py")) in [os.path.normpath(p) for p in seen]


def test_unknown_agent_fails_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    code, stdout = _run("nope", {}, unused_port(), monkeypatch, tmp_path)
    assert (code, stdout) == (0, "")


@pytest.mark.parametrize("agent", adapters.ids())
def test_cli_routes_every_adapter_to_the_fast_path(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivelane import cli

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(hook_fast, "run_agent", lambda a, p: calls.append((a, p)) or 0)
    monkeypatch.setattr(cli, "build_parser", lambda: pytest.fail("parser built on the hook path"))
    assert cli.main(["hook", agent, "--port", "4999"]) == 0
    assert calls == [(agent, 4999)]
    assert cli.main(["hook", agent, "--bogus"]) == 0  # bad flags fail open


# --- finding 01 regressions ------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["hook", "claude"],  # typo
        ["hook", "gemini"],  # an id from another hivelane version
        ["hook", "some-future-agent", "--port", "4600"],
        ["hook"],  # no id at all
    ],
)
def test_cli_unknown_hook_id_fails_open(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """finding 01 #1 / T1: argparse would exit 2 ("invalid choice"), which
    every supported agent reads as "block this edit"."""
    from hivelane import cli

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(cli, "build_parser", lambda: pytest.fail("argparse would exit 2 here"))
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "proceeding without coordination" in captured.err


def test_cli_hook_admin_commands_still_reach_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    from hivelane import cli

    with pytest.raises(SystemExit) as caught:
        cli.main(["hook", "install", "--help"])
    assert caught.value.code == 0


def _codex_patch_payload(tmp_path: Path, names: list[str]) -> dict[str, Any]:
    body = "".join(f"*** Update File: {n}\n@@\n-a\n+b\n" for n in names)
    for n in names:
        (tmp_path / n).write_text("a\n", encoding="utf-8")
    return {
        "session_id": "c1", "cwd": str(tmp_path), "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Begin Patch\n{body}*** End Patch\n"},
    }


def test_codex_block_survives_a_later_unreachable_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finding 01 #2 / T8: a.py blocks, then the daemon stops answering on
    b.py, the block must still be rendered, never the offline notice."""
    payload = _codex_patch_payload(tmp_path, ["a.py", "b.py"])
    answers: list[Any] = [ACKS["warn"], hook_fast.DaemonUnreachable(hook_fast.OFFLINE_NOT_RESPONDING)]

    def post(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(hook_fast, "post_event", post)
    code, stdout = _run("codex", payload, unused_port(), monkeypatch, tmp_path / "home")
    assert code == 0
    data = json.loads(stdout)
    assert "Hivelane warning" in data["hookSpecificOutput"]["additionalContext"]
    assert "offline" not in stdout

    answers[:] = [ACKS["allow"], ACKS["block"]]
    payload = _codex_patch_payload(tmp_path, ["a.py", "b.py", "c.py"])
    answers.append(hook_fast.DaemonUnreachable(hook_fast.OFFLINE_NOT_RESPONDING))
    code, stdout = _run("codex", payload, unused_port(), monkeypatch, tmp_path / "home")
    assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_unreachable_before_any_ack_is_the_offline_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _codex_patch_payload(tmp_path, ["a.py", "b.py"])

    def post(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
        raise hook_fast.DaemonUnreachable(hook_fast.OFFLINE_NOT_RESPONDING)

    monkeypatch.setattr(hook_fast, "post_event", post)
    code, stdout = _run("codex", payload, unused_port(), monkeypatch, tmp_path / "home")
    assert code == 0
    assert json.loads(stdout) == {"systemMessage": hook_fast.OFFLINE_NOT_RESPONDING}


def test_multi_file_posts_share_one_time_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """finding 01 #2: each post gets only what is left of HOOK_TOTAL_BUDGET,
    posting stops once it is spent, and the strictest ack so far decides."""
    names = [f"f{i}.py" for i in range(8)]
    payload = _codex_patch_payload(tmp_path, names)
    clock = [1000.0]
    monkeypatch.setattr(hook_fast.time, "monotonic", lambda: clock[0])
    timeouts: list[float] = []

    def post(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
        timeouts.append(timeout)
        clock[0] += 1.4  # a slow relay check per file
        return ACKS["warn"] if len(timeouts) == 1 else ACKS["allow"]

    monkeypatch.setattr(hook_fast, "post_event", post)
    code, stdout = _run("codex", payload, unused_port(), monkeypatch, tmp_path / "home")
    assert code == 0
    assert len(timeouts) < len(names)
    assert all(t <= hook_fast.HOOK_POST_TIMEOUT for t in timeouts)
    assert sum(1.4 for _ in timeouts) <= hook_fast.HOOK_TOTAL_BUDGET + 1.4
    assert timeouts[-1] < hook_fast.HOOK_POST_TIMEOUT  # the last post got only the remainder
    assert "Hivelane warning" in stdout
    # Well under every agent's PreToolUse timeout (5 s), with start-up room.
    assert hook_fast.HOOK_TOTAL_BUDGET + 1.0 < 5


def test_cursor_passthrough_never_approves(
    stub_daemon: tuple[int, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """finding 01 #3: allow ack, warn, offline and internal errors all answer
    Cursor's no-decision form, never `permission: allow`."""
    port, state = stub_daemon
    (tmp_path / "room.py").write_text("def room():\n    pass\n", encoding="utf-8")
    payload, _ = load_fixture("cursor", "pre_edit", tmp_path)
    for ack in (ACKS["allow"], ACKS["warn"], {"decision": "nonsense"}):
        state["ack"] = ack
        code, stdout = _run("cursor", payload, port, monkeypatch, tmp_path / "home")
        assert code == 0 and json.loads(stdout)["permission"] == "ask", ack
    code, stdout = _run("cursor", payload, unused_port(), monkeypatch, tmp_path / "home2")
    assert json.loads(stdout)["permission"] == "ask"

    def broken(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook_fast, "post_event", broken)
    code, stdout = _run("cursor", payload, port, monkeypatch, tmp_path / "home3")
    assert (code, json.loads(stdout)) == (0, {"permission": "ask"})


def test_hook_reads_stdin_bytes_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T5: the production path reads sys.stdin.buffer (bytes) and decodes
    UTF-8 itself, so non-ASCII prompts survive a Windows ANSI console."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(hook_fast, "post_event", lambda e, p, timeout=2.0: seen.append(e) or ACKS["allow"])
    payload = {"session_id": "s", "cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit", "prompt": "réécris ✓ 日本"}
    raw = b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert hook_fast.run_agent("claude-code", unused_port()) == 0
    assert seen and seen[0]["prompt"] == "réécris ✓ 日本"
