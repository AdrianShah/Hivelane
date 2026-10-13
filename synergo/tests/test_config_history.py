"""history_hours in .synergo.toml: the team's choice (project file only),
0..24, sent on join only when the file sets it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synergo import config as cfg
from synergo.daemon import RoomConnection
from synergo.protocol import RoomSettings


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfg.HOME_ENV, str(tmp_path / "home"))


def test_history_hours_is_read_and_sent_with_the_windows(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\nhistory_hours = 4\n')
    config = cfg.load_config(tmp_path)
    assert config.history_hours == 4.0
    assert config.room_settings()["history_hours"] == 4.0
    assert RoomSettings.model_validate(config.room_settings()).history_hours == 4.0


def test_absent_history_hours_is_not_sent(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\n')
    config = cfg.load_config(tmp_path)
    assert config.history_hours is None
    assert "history_hours" not in config.room_settings()


@pytest.mark.parametrize("value", ["25", "-1", '"4"', "true", "inf"])
def test_out_of_range_history_hours_is_an_error(tmp_path: Path, value: str) -> None:
    write(tmp_path / cfg.PROJECT_FILE, f'room = "TEAM42"\nhistory_hours = {value}\n')
    with pytest.raises(cfg.ConfigFileError, match="history_hours"):
        cfg.load_config(tmp_path)


def test_zero_turns_history_off_explicitly(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\nhistory_hours = 0\n')
    assert cfg.load_config(tmp_path).room_settings()["history_hours"] == 0.0


def test_the_personal_file_cannot_set_history_hours(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\nhistory_hours = 2\n')
    write(cfg.personal_config_path(), "history_hours = 24\n")
    assert cfg.load_config(tmp_path).history_hours == 2.0


def test_writer_round_trips_history_hours(tmp_path: Path) -> None:
    cfg.write_project_config(tmp_path, room="TEAM42", history_hours=4)
    text = (tmp_path / cfg.PROJECT_FILE).read_text(encoding="utf-8")
    assert "history_hours = 4" in text
    assert cfg.load_config(tmp_path).history_hours == 4.0
    cfg.write_project_config(tmp_path, history_hours=0.5)
    assert cfg.load_config(tmp_path).history_hours == 0.5


def _join(settings: RoomSettings) -> dict:
    connection = RoomConnection("TEAM42", "ada", settings=settings)
    return json.loads(connection._join_payload())


def test_join_carries_history_hours_only_when_the_file_sets_it(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\n')
    without = RoomSettings.model_validate(cfg.load_config(tmp_path).room_settings())
    assert "history_hours" not in _join(without)["settings"]

    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\nhistory_hours = 0\n')
    off = RoomSettings.model_validate(cfg.load_config(tmp_path).room_settings())
    assert _join(off)["settings"]["history_hours"] == 0
