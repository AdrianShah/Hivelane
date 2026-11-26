"""The Hivelane section `hivelane up` keeps in agent instruction files, and
`up --room <share link>`. No network and no real daemon: see cli_fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivelane import agent_docs, cli, links
from hivelane.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

# --- agent_docs, the module ------------------------------------------------------


def test_creates_agents_md_when_no_instruction_file_exists(tmp_path: Path) -> None:
    changes = agent_docs.apply(tmp_path)
    assert [(c.path.name, c.action) for c in changes] == [("AGENTS.md", "created")]
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(agent_docs.BEGIN) and text.rstrip().endswith(agent_docs.END)
    assert "hivelane status" in text and "hivelane up -y" in text
    assert links.INSTALL_COMMAND in text
    assert "join_room" in text and "task" in text


def test_appends_to_existing_files_and_keeps_their_content(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Project\n\nUse tabs.\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "copilot-instructions.md").write_text("Be brief.", encoding="utf-8")

    changes = agent_docs.apply(tmp_path)

    assert sorted((c.path.name, c.action) for c in changes) == [
        ("CLAUDE.md", "added"),
        ("copilot-instructions.md", "added"),
    ]
    assert not (tmp_path / "AGENTS.md").exists()  # only created when none exist
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith("# Project\n\nUse tabs.\n\n" + agent_docs.BEGIN)
    assert (tmp_path / ".github" / "copilot-instructions.md").read_text(encoding="utf-8").startswith("Be brief.\n\n")


def test_rerun_is_idempotent_and_updates_the_block_in_place(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "# Top\n\n<!-- hivelane:begin old note -->\nstale text\n<!-- hivelane:end -->\n\n## After\nkeep me\n",
        encoding="utf-8",
    )
    assert [c.action for c in agent_docs.apply(tmp_path)] == ["updated"]
    once = path.read_text(encoding="utf-8")
    assert "stale text" not in once
    assert once.startswith("# Top\n\n" + agent_docs.BEGIN)
    assert once.endswith(agent_docs.END + "\n\n## After\nkeep me\n")
    assert once.count("hivelane:begin") == 1

    assert [c.action for c in agent_docs.apply(tmp_path)] == ["unchanged"]
    assert path.read_text(encoding="utf-8") == once
    assert agent_docs.pending(tmp_path) == []


def test_keeps_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# Rules\r\n\r\nNo force pushes.\r\n")
    agent_docs.apply(tmp_path)
    raw = path.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert [c.action for c in agent_docs.apply(tmp_path)] == ["unchanged"]


def test_unterminated_block_is_not_eaten(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("<!-- hivelane:begin -->\nmy own notes after a broken marker\n", encoding="utf-8")
    agent_docs.apply(tmp_path)
    assert "my own notes after a broken marker" in path.read_text(encoding="utf-8")


def test_claude_and_cursor_get_their_own_files_when_set_up(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    changes = {c.path.relative_to(tmp_path).as_posix(): c.action for c in agent_docs.apply(tmp_path)}
    assert changes == {"AGENTS.md": "created", "CLAUDE.md": "created", ".cursor/rules/hivelane.mdc": "created"}
    rule = (tmp_path / ".cursor" / "rules" / "hivelane.mdc").read_text(encoding="utf-8")
    assert rule.startswith("---\n") and "alwaysApply: true" in rule and agent_docs.BEGIN in rule
    assert [c.action for c in agent_docs.apply(tmp_path)] == ["unchanged"] * 3


# --- hivelane up -------------------------------------------------------------------


@pytest.mark.usefixtures("machine")
def test_up_adds_the_section_and_rerun_leaves_it(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--yes"]) == 0
    out = capsys.readouterr().out
    agents = machine.repo / "AGENTS.md"
    assert agents.is_file() and agent_docs.BEGIN in agents.read_text(encoding="utf-8")
    # Claude Code's hooks went into .claude/, so it gets a CLAUDE.md too.
    assert agent_docs.BEGIN in (machine.repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "agent instructions: created AGENTS.md" in out
    before = agents.read_bytes()

    assert cli.main(["up", "--yes"]) == 0
    out = capsys.readouterr().out
    assert agents.read_bytes() == before
    assert "Hivelane section already in" in out


@pytest.mark.usefixtures("machine")
def test_no_agent_docs_flag_skips_it(machine: Machine) -> None:
    assert cli.main(["up", "--yes", "--no-agent-docs"]) == 0
    assert not (machine.repo / "AGENTS.md").exists()
    assert not (machine.repo / "CLAUDE.md").exists()


@pytest.mark.usefixtures("machine")
def test_declining_leaves_files_alone(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    real = cli.confirm

    def answer(question: str, *, assume_yes: bool, default: bool = True) -> bool:
        if "Hivelane section" in question:
            return False
        return real(question, assume_yes=assume_yes, default=default)

    monkeypatch.setattr(cli, "confirm", answer)
    assert cli.main(["up"]) == 0
    assert not (machine.repo / "AGENTS.md").exists()


@pytest.mark.usefixtures("machine")
def test_up_prints_the_agent_prompt_to_share(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--yes", "--no-agent-docs"]) == 0
    out = capsys.readouterr().out
    prompt = links.agent_join_prompt("ws://localhost:8000/ws", "K7Q2XM")
    assert prompt in out
    assert prompt.startswith("Join Hivelane room K7Q2XM in this repo: install hivelane if needed")
    assert '`hivelane up --room "https://adrianshah.github.io/Hivelane/j/K7Q2XM?relay=' in prompt
    assert prompt.endswith('" -y`.')


# --- up --room <link> ----------------------------------------------------------


@pytest.mark.usefixtures("machine")
def test_up_room_accepts_a_share_link_with_its_relay(machine: Machine) -> None:
    link = "https://adrianshah.github.io/Hivelane/j/ABCD2345?relay=wss%3A%2F%2Frelay.example.com%2Fws"
    assert cli.main(["up", "--room", link, "--yes", "--no-agent-docs"]) == 0
    config = machine.service.called("connect_repo")[-1][3]
    assert (config.room, config.relay) == ("ABCD2345", "wss://relay.example.com/ws")
    assert machine.relay.requests == []  # joined, nothing created


@pytest.mark.usefixtures("machine")
def test_explicit_relay_beats_the_links(machine: Machine) -> None:
    link = "https://x.example/j/ABCD2345?relay=wss%3A%2F%2Frelay.example.com%2Fws"
    assert cli.main(["up", "--room", link, "--relay", "wss://mine.example/ws", "--yes", "--no-agent-docs"]) == 0
    assert machine.service.called("connect_repo")[-1][3].relay == "wss://mine.example/ws"


def test_room_argument_parses_codes_and_links() -> None:
    assert cli.room_argument(None) == (None, None, None)
    assert cli.room_argument(" abcd2345 ") == ("ABCD2345", None, None)
    assert cli.room_argument("https://d.example/r/ABCD2345") == ("ABCD2345", None, None)
    assert cli.room_argument("https://d.example/j/ABCD2345#key=s3cret") == ("ABCD2345", None, "s3cret")
    with pytest.raises(cli.ConfigError):
        cli.room_argument("https://d.example/nothing-here")
