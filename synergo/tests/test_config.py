"""`.synergo.toml` + `~/.synergo/config.toml`: precedence, validation, and a
writer that edits the committed file without wrecking what people put there."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from synergo import config as cfg


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv(cfg.HOME_ENV, str(home))
    monkeypatch.delenv(cfg.RELAY_ENV, raising=False)
    monkeypatch.delenv(cfg.NAME_ENV, raising=False)
    return home


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def personal(text: str) -> Path:
    return write(cfg.personal_config_path(), text)


# --- locations -------------------------------------------------------------------


def test_synergo_home_honours_env(isolated: Path) -> None:
    assert cfg.synergo_home() == isolated
    assert cfg.personal_config_path() == isolated / "config.toml"


def test_synergo_home_defaults_to_dot_synergo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cfg.HOME_ENV)
    assert cfg.synergo_home() == Path.home() / ".synergo"


def test_find_project_config_walks_up(tmp_path: Path) -> None:
    project = write(tmp_path / "repo" / cfg.PROJECT_FILE, 'room = "TEAM42"\n')
    nested = tmp_path / "repo" / "src" / "pkg"
    nested.mkdir(parents=True)
    assert cfg.find_project_config(nested) == project
    assert cfg.find_project_config(tmp_path) is None


# --- precedence ----------------------------------------------------------------------


def test_defaults_without_any_file(tmp_path: Path) -> None:
    config = cfg.load_config(tmp_path)
    assert config.room is None
    assert config.relay == cfg.DEFAULT_RELAY
    assert config.block_window_seconds == cfg.DEFAULT_BLOCK_WINDOW
    assert config.warn_window_seconds == cfg.DEFAULT_WARN_WINDOW
    assert config.share_diffs is True
    assert config.root is None
    assert config.sources == ()


def test_project_file_values_and_root(tmp_path: Path) -> None:
    project = write(
        tmp_path / cfg.PROJECT_FILE,
        'room = "TEAM42"\nrelay = "wss://relay.example.com/ws"\n'
        "block_window_seconds = 10\nwarn_window_seconds = 30.5\n"
        'ignore = ["*.lock"]\nshare_diffs = false\n',
    )
    config = cfg.load_config(tmp_path)
    assert config.room == "TEAM42"
    assert config.relay == "wss://relay.example.com/ws"
    assert config.room_settings() == {"block_window_seconds": 10.0, "warn_window_seconds": 30.5}
    assert config.ignore == ("*.lock",)
    assert config.share_diffs is False
    assert config.root == tmp_path.resolve()
    assert config.sources == (project.resolve(),)


def test_personal_overrides_project_for_personal_keys(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\nname = "team-default"\nignore = ["a"]\n')
    personal('name = "ada"\nrelay = "ws://private:9000/ws"\nignore = ["b"]\nshare_diffs = false\n')
    config = cfg.load_config(tmp_path)
    assert config.name == "ada"
    assert config.relay == "ws://private:9000/ws"
    assert config.ignore == ("a", "b")
    assert config.share_diffs is False
    assert len(config.sources) == 2


def test_personal_file_cannot_override_room_or_windows(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\nblock_window_seconds = 15\n')
    personal('room = "MINE99"\nblock_window_seconds = 0\nwarn_window_seconds = 1\n')
    config = cfg.load_config(tmp_path)
    assert config.room == "TEAM42"
    assert config.block_window_seconds == 15.0
    assert config.warn_window_seconds == cfg.DEFAULT_WARN_WINDOW


def test_personal_room_alone_is_ignored(tmp_path: Path) -> None:
    personal('room = "MINE99"\n')
    assert cfg.load_config(tmp_path).room is None


def test_env_beats_personal_and_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'relay = "ws://project/ws"\nname = "p"\n')
    personal('relay = "ws://personal/ws"\nname = "q"\n')
    monkeypatch.setenv(cfg.RELAY_ENV, "wss://env.example/ws")
    monkeypatch.setenv(cfg.NAME_ENV, "envname")
    config = cfg.load_config(tmp_path)
    assert config.relay == "wss://env.example/ws"
    assert config.name == "envname"


def test_blank_env_vars_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfg.RELAY_ENV, "  ")
    monkeypatch.setenv(cfg.NAME_ENV, "")
    config = cfg.load_config(tmp_path)
    assert config.relay == cfg.DEFAULT_RELAY
    assert config.name is None


def test_overrides_beat_everything_and_none_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'room = "TEAM42"\n')
    monkeypatch.setenv(cfg.RELAY_ENV, "wss://env.example/ws")
    config = cfg.load_config(tmp_path, relay="ws://flag/ws", room="FLAG01", name=None)
    assert config.relay == "ws://flag/ws"
    assert config.room == "FLAG01"
    assert config.name is None


def test_root_override_sets_root(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    config = cfg.load_config(tmp_path, root=other)
    assert config.root == other


def test_with_overrides_skips_none(tmp_path: Path) -> None:
    base = cfg.load_config(tmp_path)
    changed = cfg.with_overrides(base, name="ada", room=None)
    assert changed.name == "ada"
    assert changed.room is None


# --- validation ----------------------------------------------------------------------


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, "room = \n")
    with pytest.raises(cfg.ConfigFileError, match="not valid TOML"):
        cfg.load_config(tmp_path)


def test_invalid_personal_toml_is_reported(tmp_path: Path) -> None:
    personal("name = [unclosed\n")
    with pytest.raises(cfg.ConfigFileError, match="config.toml"):
        cfg.load_config(tmp_path)


@pytest.mark.parametrize(
    "text, message",
    [
        ("room = 42\n", "room must be a non-empty string"),
        ('room = "  "\n', "room must be a non-empty string"),
        ('room = "has/slash"\n', "room must be letters"),
        ('relay = "http://example.com"\n', "ws:// or wss://"),
        ('block_window_seconds = "10"\n', "non-negative number"),
        ("block_window_seconds = -1\n", "non-negative number"),
        ("warn_window_seconds = true\n", "non-negative number"),
        ("warn_window_seconds = nan\n", "non-negative number"),
        ('ignore = "*.lock"\n', "list of glob strings"),
        ("ignore = [1, 2]\n", "list of glob strings"),
        ('share_diffs = "yes"\n', "true or false"),
        ("block_window_seconds = 90\nwarn_window_seconds = 30\n", "cannot exceed"),
    ],
)
def test_invalid_values_are_reported(tmp_path: Path, text: str, message: str) -> None:
    write(tmp_path / cfg.PROJECT_FILE, text)
    with pytest.raises(cfg.ConfigFileError, match=message):
        cfg.load_config(tmp_path)


def test_invalid_personal_window_is_still_reported(tmp_path: Path) -> None:
    """Ignored or not, a nonsense value should not pass silently."""
    personal("block_window_seconds = -5\n")
    with pytest.raises(cfg.ConfigFileError):
        cfg.load_config(tmp_path)


def test_invalid_relay_env_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfg.RELAY_ENV, "relay.example.com")
    with pytest.raises(cfg.ConfigFileError, match=cfg.RELAY_ENV):
        cfg.load_config(tmp_path)


def test_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    (tmp_path / cfg.PROJECT_FILE).write_bytes(b'\xef\xbb\xbfroom = "TEAM42"\n')
    assert cfg.load_config(tmp_path).room == "TEAM42"


# --- writer ----------------------------------------------------------------------------


def parsed(root: Path) -> dict:
    return tomllib.loads((root / cfg.PROJECT_FILE).read_text(encoding="utf-8"))


def test_writer_creates_a_commented_file(tmp_path: Path) -> None:
    path = cfg.write_project_config(tmp_path, room="TEAM42", relay="wss://r.example/ws")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert parsed(tmp_path) == {"room": "TEAM42", "relay": "wss://r.example/ws"}
    assert cfg.load_config(tmp_path).room == "TEAM42"


def test_writer_round_trip_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    original = (
        "# Team settings\n"
        'room = "OLD123"  # the team room\n'
        "\n"
        "# keep diffs private for now\n"
        "share_diffs = false\n"
        'future_key = "kept"\n'
        "ignore = [\n"
        '  "*.lock",  # generated\n'
        '  "dist/**",\n'
        "]\n"
    )
    write(tmp_path / cfg.PROJECT_FILE, original)

    cfg.write_project_config(
        tmp_path, room="NEW456", block_window_seconds=12.5, ignore=["*.min.js"], relay=None
    )

    text = (tmp_path / cfg.PROJECT_FILE).read_text(encoding="utf-8")
    assert "# Team settings" in text
    assert "# keep diffs private for now" in text
    assert 'room = "NEW456"  # the team room' in text
    assert 'future_key = "kept"' in text
    assert "dist/**" not in text  # the whole multi-line array was replaced
    data = parsed(tmp_path)
    assert data == {
        "room": "NEW456",
        "share_diffs": False,
        "future_key": "kept",
        "ignore": ["*.min.js"],
        "block_window_seconds": 12.5,
    }
    # Writing the same values again changes nothing.
    cfg.write_project_config(tmp_path, room="NEW456", block_window_seconds=12.5, ignore=["*.min.js"])
    assert (tmp_path / cfg.PROJECT_FILE).read_text(encoding="utf-8") == text


def test_writer_never_appends_into_a_table(tmp_path: Path) -> None:
    write(
        tmp_path / cfg.PROJECT_FILE,
        'share_diffs = true\n\n# tooling, not ours\n[tool.other]\nroom = "not-top-level"\n',
    )
    cfg.write_project_config(tmp_path, room="TEAM42", relay="ws://r/ws")
    data = parsed(tmp_path)
    assert data["room"] == "TEAM42"
    assert data["relay"] == "ws://r/ws"
    assert data["tool"]["other"] == {"room": "not-top-level"}


def test_writer_with_only_a_table_inserts_above_its_comment(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, "# about the table\n[extra]\nx = 1\n")
    cfg.write_project_config(tmp_path, room="TEAM42")
    data = parsed(tmp_path)
    assert data == {"room": "TEAM42", "extra": {"x": 1}}
    lines = (tmp_path / cfg.PROJECT_FILE).read_text(encoding="utf-8").splitlines()
    assert lines.index("# about the table") == lines.index("[extra]") - 1


def test_writer_escapes_strings(tmp_path: Path) -> None:
    cfg.write_project_config(tmp_path, ignore=['we"ird\\path', "tab\there"])
    assert parsed(tmp_path)["ignore"] == ['we"ird\\path', "tab\there"]


def test_writer_keeps_crlf_files_crlf(tmp_path: Path) -> None:
    (tmp_path / cfg.PROJECT_FILE).write_bytes(b'# hi\r\nroom = "OLD123"\r\n')
    cfg.write_project_config(tmp_path, room="NEW456", relay="ws://r/ws")
    raw = (tmp_path / cfg.PROJECT_FILE).read_bytes()
    assert b'room = "NEW456"\r\n' in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_writer_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        cfg.write_project_config(tmp_path, name="ada")
    assert not (tmp_path / cfg.PROJECT_FILE).exists()


def test_writer_refuses_to_break_a_file(tmp_path: Path) -> None:
    broken = 'room = "TEAM42"\nthis is not toml\n'
    write(tmp_path / cfg.PROJECT_FILE, broken)
    with pytest.raises(cfg.ConfigFileError, match="refusing to write"):
        cfg.write_project_config(tmp_path, relay="ws://r/ws")
    assert (tmp_path / cfg.PROJECT_FILE).read_text(encoding="utf-8") == broken


def test_personal_writer_round_trip(isolated: Path, tmp_path: Path) -> None:
    path = cfg.write_personal_config(name="ada")
    assert path == cfg.personal_config_path()
    assert path.read_text(encoding="utf-8").startswith("#")
    assert cfg.load_config(tmp_path).name == "ada"

    path.write_text('# mine\nname = "ada"  # me\nshare_diffs = false\n', encoding="utf-8")
    cfg.write_personal_config(name="grace", relay="wss://mine.example/ws")
    assert path.read_text(encoding="utf-8") == (
        '# mine\nname = "grace"  # me\nshare_diffs = false\nrelay = "wss://mine.example/ws"\n'
    )


def test_personal_writer_refuses_team_keys(isolated: Path) -> None:
    with pytest.raises(ValueError, match="unsupported personal keys"):
        cfg.write_personal_config(room="TEAM42")
    assert not cfg.personal_config_path().exists()


def test_remove_project_keys_preserves_everything_else(tmp_path: Path) -> None:
    original = (
        "# Team settings\n"
        'room = "TEAM42"  # shared room\n'
        'relay = "wss://r.example/ws"\n'
        "ignore = [\n"
        '  "*.lock",\n'
        "]\n"
        "\n"
        "[tool.other]\n"
        'room = "nested"\n'
    )
    write(tmp_path / cfg.PROJECT_FILE, original)

    assert cfg.remove_project_keys(tmp_path, "room", "share_diffs") == ["room"]

    text = (tmp_path / cfg.PROJECT_FILE).read_text(encoding="utf-8")
    assert text == original.replace('room = "TEAM42"  # shared room\n', "")
    assert "room" not in parsed(tmp_path)
    assert parsed(tmp_path)["tool"]["other"]["room"] == "nested"
    assert cfg.remove_project_keys(tmp_path, "room") == []


def test_remove_project_keys_handles_multiline_values(tmp_path: Path) -> None:
    write(tmp_path / cfg.PROJECT_FILE, 'ignore = [\n  "a",\n  "b",\n]\nroom = "TEAM42"\n')
    assert cfg.remove_project_keys(tmp_path, "ignore") == ["ignore"]
    assert parsed(tmp_path) == {"room": "TEAM42"}


def test_remove_project_keys_without_a_file(tmp_path: Path) -> None:
    assert cfg.remove_project_keys(tmp_path, "room") == []
    assert not (tmp_path / cfg.PROJECT_FILE).exists()
