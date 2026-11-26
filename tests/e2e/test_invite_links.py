"""Invite links against the real relay and real daemons: a teammate whose
daemon presents the share link's invite secret comes straight in and keeps
the member token the relay hands back; one with only the room code waits at
the door for an owner."""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hivelane import config as cfg
from hivelane.daemon import Daemon

from .conftest import free_port, wait_until

if TYPE_CHECKING:
    from .conftest import Cluster, DaemonClient

pytestmark = [
    pytest.mark.invite_links,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git for repo roots"),
]


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def _entry(client: DaemonClient, root: Path) -> dict[str, Any] | None:
    status = client.status() or {}
    return next((r for r in status.get("repos", []) if r["root"] == str(root)), None)


def test_the_link_admits_and_is_remembered_while_the_code_alone_waits(
    cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HIVELANE_HOME", str(home))
    created = cluster.http.post(f"{cluster.relay.http_url}/rooms").json()
    room, secret = created["room_id"], created["invite_secret"]
    sam_repo, kim_repo = _repo(tmp_path / "sam"), _repo(tmp_path / "kim")

    sam, kim = cluster.run_daemons(
        [Daemon(port=free_port(), batch_seconds=0.05), Daemon(port=free_port(), batch_seconds=0.05)]
    )
    base = {"room": room, "relay": cluster.relay.ws_url, "agent": "claude-code"}
    sam.call("/repos", {**base, "root": str(sam_repo), "name": "sam", "invite_secret": secret})
    kim.call("/repos", {**base, "root": str(kim_repo), "name": "kim"})

    def settled() -> bool:
        s, k = _entry(sam, sam_repo), _entry(kim, kim_repo)
        return bool(s and s.get("connected") and k and k.get("awaiting_approval"))

    wait_until(settled, what="sam to be let in and kim to wait for approval")
    # The relay's member token landed in the personal config, never a repo file.
    wait_until(
        lambda: cfg.load_config(sam_repo).member_token(room) is not None,
        what="sam's member token to be saved",
    )
    assert not (sam_repo / cfg.PROJECT_FILE).exists()
    assert secret not in str(_entry(sam, sam_repo))
