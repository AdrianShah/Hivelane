"""`synergo run`: wraps a hookless agent's process with the observe-only watcher."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from synergo import cli, watch


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def fake_poster(port: int, say: Any) -> Any:
        def post(event: dict[str, Any]) -> dict[str, Any]:
            events.append(event)
            return {"decision": "allow", "others": []}

        return post

    monkeypatch.setattr(watch, "daemon_poster", fake_poster)
    return events


def test_message_arg() -> None:
    assert watch.message_arg(["-m", "fix it"]) == "fix it"
    assert watch.message_arg(["--yes", "--message", "x"]) == "x"
    assert watch.message_arg(["--message=y"]) == "y"
    assert watch.message_arg(["-m"]) is None
    assert watch.message_arg(["foo.py"]) is None


def test_run_posts_post_edit_and_forwards_exit_code(tmp_path: Path, posted: list[dict[str, Any]]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "room.py").write_text("old", encoding="utf-8")
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path('room.py').write_text('new')\nsys.exit(3)\n", encoding="utf-8"
    )
    code = cli.main(
        ["run", "--agent", "aider", "--root", str(repo), "--interval", "0.05", "--",
         sys.executable, str(script), "-m", "rename room"]
    )
    assert code == 3
    assert posted[0]["kind"] == "intent" and posted[0]["prompt"] == "rename room"
    edits = [e for e in posted if e["kind"] == "post_edit"]
    assert len(edits) == 1
    assert Path(edits[0]["file_path"]).name == "room.py"
    assert edits[0]["old_content"] == "old" and edits[0]["new_content"] == "new"
    assert edits[0]["agent"] in ("aider", "unknown")  # "aider" once it is an AgentKind


def test_run_generic_agent_without_message(tmp_path: Path, posted: list[dict[str, Any]]) -> None:
    code = cli.main(["run", "--root", str(tmp_path), "--interval", "0.05", sys.executable, "-c", "pass"])
    assert code == 0
    assert posted == []


def test_run_missing_command(tmp_path: Path, posted: list[dict[str, Any]]) -> None:
    assert cli.main(["run", "--root", str(tmp_path), "--", "definitely-not-a-real-tool-xyz"]) == 127
    assert cli.main(["run", "--root", str(tmp_path)]) == 2
