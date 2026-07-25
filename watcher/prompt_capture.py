"""Best-effort Cursor prompt capture.

Reads the latest user message from Cursor's local SQLite (`state.vscdb`).
This is undocumented and version-sensitive — any failure returns None so the
watcher still works on diffs alone.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

# Ignore prompts older than this relative to flush time.
DEFAULT_MAX_AGE_SECONDS = 15 * 60


def _cursor_user_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cursor" / "User"
    # macOS / Linux fallbacks for teammates not on Windows.
    home = Path.home()
    mac = home / "Library" / "Application Support" / "Cursor" / "User"
    if mac.exists():
        return mac
    return home / ".config" / "Cursor" / "User"


def _normalize_folder(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").lower()


def _folder_from_uri(uri: str) -> Optional[str]:
    if not uri:
        return None
    parsed = urlparse(uri)
    raw = unquote(parsed.path or "")
    # file:///c%3A/Hack%20Day -> /c:/Hack Day on Windows urls
    if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]
    return raw.replace("\\", "/").lower()


def resolve_workspace_id(project_root: Path) -> Optional[str]:
    """Match the watched folder to a Cursor workspaceStorage hash."""
    try:
        target = project_root.resolve()
    except OSError:
        return None

    storage = _cursor_user_dir() / "workspaceStorage"
    if not storage.is_dir():
        return None

    target_norm = _normalize_folder(target)
    for entry in storage.iterdir():
        meta = entry / "workspace.json"
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            folder = _folder_from_uri(data.get("folder", ""))
            if not folder:
                continue
            if folder == target_norm or Path(folder).resolve() == target:
                return entry.name
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return None


def _parse_created_at(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Cursor sometimes stores ms epochs.
        return float(value) / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _open_readonly(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=1.0)
        con.execute("PRAGMA query_only=ON")
        return con
    except sqlite3.Error as exc:
        log.debug("Could not open %s: %s", db_path, exc)
        return None


def _composer_ids_for_workspace(global_db: sqlite3.Connection, workspace_id: str, limit: int = 8) -> list[str]:
    try:
        rows = global_db.execute(
            """
            SELECT composerId FROM composerHeaders
            WHERE workspaceId = ? AND isArchived = 0
            ORDER BY lastUpdatedAt DESC
            LIMIT ?
            """,
            (workspace_id, limit),
        ).fetchall()
        if rows:
            return [r[0] for r in rows]
    except sqlite3.Error:
        pass

    # Fallback: ItemTable composer.composerHeaders filtered by workspaceIdentifier.
    try:
        row = global_db.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerHeaders'"
        ).fetchone()
        if not row:
            return []
        data = json.loads(row[0])
        composers = data.get("allComposers") or []
        matched: list[tuple[int, str]] = []
        for item in composers:
            ident = (item.get("workspaceIdentifier") or {}).get("id")
            if ident == workspace_id and not item.get("isArchived"):
                matched.append((int(item.get("lastUpdatedAt") or 0), item.get("composerId", "")))
        matched.sort(reverse=True)
        return [cid for _, cid in matched if cid][:limit]
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return []


def _latest_user_text(
    global_db: sqlite3.Connection,
    composer_id: str,
    now: float,
    max_age_seconds: float,
) -> Optional[tuple[float, str]]:
    try:
        rows = global_db.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ? ORDER BY rowid DESC LIMIT 40",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return None

    best: Optional[tuple[float, str]] = None
    for (raw,) in rows:
        try:
            bubble = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if bubble.get("type") != 1:
            continue
        text = bubble.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        created = _parse_created_at(bubble.get("createdAt"))
        if created is None or now - created > max_age_seconds:
            continue
        if best is None or created > best[0]:
            best = (created, text.strip())
    return best


def capture_latest_prompt(
    project_root: Path,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> Optional[str]:
    """Return the most recent Cursor user prompt for this project, or None."""
    try:
        workspace_id = resolve_workspace_id(project_root)
        if not workspace_id:
            log.debug("No Cursor workspace matched for %s", project_root)
            return None

        global_db_path = _cursor_user_dir() / "globalStorage" / "state.vscdb"
        con = _open_readonly(global_db_path)
        if con is None:
            return None

        try:
            now = datetime.now(timezone.utc).timestamp()
            best: Optional[tuple[float, str]] = None
            for composer_id in _composer_ids_for_workspace(con, workspace_id):
                hit = _latest_user_text(con, composer_id, now, max_age_seconds)
                if hit and (best is None or hit[0] > best[0]):
                    best = hit
            if best:
                # Keep payloads small for the relay / Gemini.
                text = best[1]
                if len(text) > 500:
                    text = text[:500].rstrip() + "…"
                return text
            return None
        finally:
            con.close()
    except Exception as exc:
        log.debug("Prompt capture failed: %s", exc)
        return None
