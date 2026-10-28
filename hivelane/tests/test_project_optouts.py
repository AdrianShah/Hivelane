"""A repo the daemon adopts on its own cannot turn off transport security or
redaction through its committed .hivelane.toml (hivelane.daemon.discover_repo)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivelane import config
from hivelane.daemon import discover_repo


def _repo(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / config.PROJECT_FILE).write_text(
        'room = "ABCD1234"\nname = "ada"\n' + body, encoding="utf-8"
    )
    return repo


def test_a_plain_project_file_is_adopted(tmp_path: Path) -> None:
    found = discover_repo(str(_repo(tmp_path, 'relay = "wss://relay.example/ws"\n')))
    assert found is not None and found[0].room == "ABCD1234"


@pytest.mark.parametrize(
    "body",
    [
        'relay = "ws://203.0.113.5:9/ws"\ninsecure = true\n',
        'relay = "wss://relay.example/ws"\nredact = false\nshare_diffs = true\n',
    ],
)
def test_project_optouts_are_not_adopted_silently(
    tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert discover_repo(str(_repo(tmp_path, body))) is None
    assert "run `hivelane up` there" in capsys.readouterr().err


def test_the_personal_config_can_confirm_an_optout(tmp_path: Path) -> None:
    repo = _repo(tmp_path, 'relay = "ws://203.0.113.5:9/ws"\ninsecure = true\n')
    personal = config.personal_config_path()
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text("insecure = true\n", encoding="utf-8")
    found = discover_repo(str(repo))
    assert found is not None and found[0].insecure is True
