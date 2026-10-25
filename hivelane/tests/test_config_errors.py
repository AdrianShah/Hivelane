"""Bad local config is reported, never guessed around, settings.json is the
user's file and hivelane writes it back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hivelane.cli import ConfigError, load_settings, resolve_port


def test_non_object_settings_refuses_rather_than_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_settings(path)

    assert json.loads(path.read_text(encoding="utf-8")) == ["not", "an", "object"]


def test_invalid_json_settings_refuses(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"hooks": {,}}', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_settings(path)


def test_missing_and_empty_settings_are_fine(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "absent.json") == {}
    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert load_settings(empty) == {}


def test_bad_port_env_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVELANE_PORT", "4600oops")
    with pytest.raises(ConfigError):
        resolve_port()


def test_out_of_range_port_is_reported() -> None:
    with pytest.raises(ConfigError):
        resolve_port(99999)
