"""`synergo join/daemon --root PATH`: validated up front, handed to the Daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from synergo import cli


@pytest.fixture
def captured_daemon(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the Daemon with a recorder whose run() returns at once."""
    calls: list[dict[str, Any]] = []

    class FakeDaemon:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        async def run(self) -> int:
            return 0

    monkeypatch.setattr("synergo.daemon.Daemon", FakeDaemon)
    return calls


@pytest.mark.parametrize("verb", ["join", "daemon"])
def test_root_is_passed_to_the_daemon(
    verb: str, tmp_path: Path, captured_daemon: list[dict[str, Any]]
) -> None:
    assert cli.main([verb, "TEAM42", "--name", "Ada", "--root", str(tmp_path)]) == 0
    assert captured_daemon[-1]["root"] == str(tmp_path)
    assert captured_daemon[-1]["room"] == "TEAM42"


def test_root_defaults_to_none(captured_daemon: list[dict[str, Any]]) -> None:
    assert cli.main(["join", "TEAM42", "--name", "Ada"]) == 0
    assert captured_daemon[-1]["root"] is None


def test_missing_root_is_a_config_error(
    tmp_path: Path, captured_daemon: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope"
    assert cli.main(["join", "TEAM42", "--name", "Ada", "--root", str(missing)]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert captured_daemon == []


def test_file_root_is_a_config_error(tmp_path: Path, captured_daemon: list[dict[str, Any]]) -> None:
    afile = tmp_path / "a.txt"
    afile.write_text("", encoding="utf-8")
    with pytest.raises(cli.ConfigError, match="not a directory"):
        cli.resolve_root(str(afile))
    assert cli.main(["daemon", "TEAM42", "--name", "Ada", "--root", str(afile)]) == 2
    assert captured_daemon == []


def test_empty_root_is_a_config_error() -> None:
    with pytest.raises(cli.ConfigError):
        cli.resolve_root("  ")


def test_hook_port_flag_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.parse_hook_args([]) is None
    assert cli.parse_hook_args(["--port", "4601"]) == 4601
    assert cli.parse_hook_args(["--port=4602"]) == 4602
    with pytest.raises(ValueError):
        cli.parse_hook_args(["--verbose"])
