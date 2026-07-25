"""Supervises locally spawned `watcher/watcher.py` subprocesses.

State is in-memory only: if the companion restarts, spawned watchers are
terminated with it (see WatcherManager.shutdown).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Literal, Optional

log = logging.getLogger("companion.process")

# Repo root = parent of companion/. The watcher is launched from here so that
# `python watcher/watcher.py` resolves its sibling `prompt_capture` import.
REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHER_SCRIPT = REPO_ROOT / "watcher" / "watcher.py"

# Bounded output history per process — enough to debug a failed start without
# letting a chatty watcher grow unbounded in memory.
LOG_LINES = 50

# SIGTERM grace period before escalating to SIGKILL.
TERMINATE_TIMEOUT_SECONDS = 3.0

WatcherStatus = Literal["running", "stopped", "errored"]

# The watcher CLI only accepts these --agent values. Tools we can detect but
# that the relay has no lane styling for are reported as "unknown".
WATCHER_CLI_AGENTS = {"cursor", "claude-code", "codex", "unknown"}


class WatcherAlreadyRunning(Exception):
    """Raised when a (path, agent) pair is already supervised and alive."""


@dataclass
class WatcherRecord:
    watcher_id: str
    path: str
    agent: str
    room_id: str
    name: str
    server_url: str
    process: subprocess.Popen
    started_at: float
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    # Set when the stop came from us, so a non-zero exit reads as "stopped".
    stop_requested: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def status(self) -> WatcherStatus:
        code = self.process.poll()
        if code is None:
            return "running"
        if self.stop_requested or code == 0:
            return "stopped"
        return "errored"

    @property
    def exit_code(self) -> Optional[int]:
        return self.process.poll()

    def append_log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)

    def recent_logs(self) -> list[str]:
        with self._lock:
            return list(self.logs)

    def key(self) -> tuple[str, str]:
        return (self.path, self.agent)


def _normalize_path(raw: str) -> str:
    return str(Path(os.path.expanduser(raw)).resolve())


def _build_command(
    path: str, room_id: str, name: str, agent: str, server_url: str
) -> list[str]:
    cli_agent = agent if agent in WATCHER_CLI_AGENTS else "unknown"
    return [
        sys.executable,
        str(WATCHER_SCRIPT),
        "--room",
        room_id,
        "--name",
        name,
        "--path",
        path,
        "--agent",
        cli_agent,
        "--server",
        server_url,
    ]


class WatcherManager:
    """Tracks watcher subprocesses keyed by generated watcher_id."""

    def __init__(self) -> None:
        self._watchers: dict[str, WatcherRecord] = {}
        self._lock = threading.Lock()

    # --- lifecycle -----------------------------------------------------------

    def start(
        self,
        *,
        path: str,
        room_id: str,
        name: str,
        agent: str,
        server_url: str,
    ) -> WatcherRecord:
        """Spawn a watcher for one (path, agent) pair.

        Raises ValueError if the path is not a directory and
        WatcherAlreadyRunning if an identical watcher is still alive.
        """
        resolved = _normalize_path(path)
        if not Path(resolved).is_dir():
            raise ValueError(f"Not a directory: {resolved}")

        with self._lock:
            self._reap_locked()
            for record in self._watchers.values():
                if record.key() == (resolved, agent) and record.status == "running":
                    raise WatcherAlreadyRunning(
                        f"A {agent} watcher is already running for {resolved}"
                    )

            command = _build_command(resolved, room_id, name, agent, server_url)
            log.info("Spawning watcher: %s", " ".join(command))
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            record = WatcherRecord(
                watcher_id=str(uuid.uuid4()),
                path=resolved,
                agent=agent,
                room_id=room_id.strip().upper(),
                name=name,
                server_url=server_url,
                process=process,
                started_at=time.time(),
            )
            self._watchers[record.watcher_id] = record

        self._start_reader(record)
        return record

    def _start_reader(self, record: WatcherRecord) -> None:
        """Drain merged stdout/stderr into the record's ring buffer."""

        def pump() -> None:
            stream = record.process.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    record.append_log(line.rstrip("\n"))
            except Exception as exc:  # stream closed mid-read on kill
                log.debug("Log pump ended for %s: %s", record.watcher_id, exc)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        thread = threading.Thread(
            target=pump, name=f"watcher-log-{record.watcher_id[:8]}", daemon=True
        )
        thread.start()

    def stop(self, watcher_id: str) -> Optional[WatcherRecord]:
        """SIGTERM, wait briefly, then SIGKILL. Returns None if unknown id."""
        with self._lock:
            record = self._watchers.get(watcher_id)
        if record is None:
            return None
        self._terminate(record)
        return record

    def _terminate(self, record: WatcherRecord) -> None:
        record.stop_requested = True
        if record.process.poll() is not None:
            return
        try:
            record.process.terminate()
            record.process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            log.info("Watcher %s terminated", record.watcher_id)
        except subprocess.TimeoutExpired:
            log.warning("Watcher %s ignored SIGTERM; killing", record.watcher_id)
            record.process.kill()
            try:
                record.process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                log.error("Watcher %s survived SIGKILL", record.watcher_id)
        except Exception as exc:
            log.warning("Failed to stop watcher %s: %s", record.watcher_id, exc)

    def shutdown(self) -> None:
        """Terminate every supervised watcher. Safe to call more than once."""
        with self._lock:
            records = list(self._watchers.values())
        for record in records:
            self._terminate(record)

    # --- inspection ----------------------------------------------------------

    def get(self, watcher_id: str) -> Optional[WatcherRecord]:
        with self._lock:
            return self._watchers.get(watcher_id)

    def list(self) -> list[WatcherRecord]:
        with self._lock:
            return sorted(self._watchers.values(), key=lambda r: r.started_at)

    def remove(self, watcher_id: str) -> None:
        with self._lock:
            self._watchers.pop(watcher_id, None)

    def _reap_locked(self) -> None:
        """Forget cleanly exited watchers so their (path, agent) frees up.

        Caller must hold self._lock. Crashed watchers are kept so the UI can
        surface `errored` plus their final log lines.
        """
        dead = [
            wid
            for wid, record in self._watchers.items()
            if record.status == "stopped" and record.stop_requested
        ]
        for wid in dead:
            del self._watchers[wid]
