"""The local daemon's per-install secret.

Anything on the machine can open a loopback socket, and before this every
process (and, through DNS rebinding, every web page) could drive the daemon:
register a repo into a room of its choosing, read the room's peers, shut it
down. Now every HTTP endpoint except a bare `GET /health` wants the header
`X-Synergo-Token` carrying the contents of `SYNERGO_HOME/token`: 32 random
bytes, url-safe base64, created owner-only (0600) the first time the daemon or
`synergo up` runs. Whoever can read that file is, by definition, the user the
daemon belongs to; nobody else is.

The hook hot path reads the same file with plain `open()` (see
synergo.hook_fast.read_token) so it pays no import for it.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Optional

from synergo.config import synergo_home

TOKEN_FILENAME = "token"
TOKEN_HEADER = "X-Synergo-Token"
TOKEN_BYTES = 32
# Shorter than this is not a token we wrote (a truncated or hand-edited file):
# replaced rather than trusted. token_urlsafe(32) is 43 characters.
MIN_TOKEN_CHARS = 32
MAX_TOKEN_CHARS = 512
# url-safe base64 only: the same rule as synergo.hook_fast (asserted in the
# tests), so a token the CLI accepts is one the hook sends, and nothing in the
# file can smuggle a header into the hook's hand-built request.
TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
# A racing creator writes its token right after O_EXCL succeeds; a reader that
# lands in between sees an empty file for a moment.
RACE_WAIT_SECONDS = 1.0
# Windows opens os.open() files in text mode (\n written as \r\n) by default.
_BINARY = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def token_path() -> Path:
    return synergo_home() / TOKEN_FILENAME


def _valid(text: str) -> bool:
    return MIN_TOKEN_CHARS <= len(text) <= MAX_TOKEN_CHARS and TOKEN_CHARS.issuperset(text)


def read_token(path: Optional[Path] = None) -> Optional[str]:
    """The token on disk, or None when it is missing, unreadable or malformed."""
    path = path or token_path()
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if _valid(text) else None


def restrict_permissions(path: Path) -> None:
    """Owner read/write only where the OS has POSIX modes. On Windows the file
    inherits its directory's ACL: under the user profile (the default
    SYNERGO_HOME) that is only the user and administrators. A SYNERGO_HOME
    outside the profile (C:\\synergo, a shared drive) gets that directory's
    ACL instead, which may be readable by other users; chmod would only toggle
    the read-only flag, so nothing here can fix that."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_private(path: Path, text: str) -> None:
    """Replace path's contents with text, owner-only (0600) from the first
    byte: the temp file is created with that mode, so there is never a moment
    where another user could open it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An unpredictable name, created exclusively and never through a symlink:
    # a pre-planted file or link at the temp path cannot receive the secret.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        restrict_permissions(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    restrict_permissions(path)


def _is_empty(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def ensure_token(path: Optional[Path] = None) -> str:
    """The install's token, creating it (0600) if there is none yet.

    Creation is O_EXCL, so of two processes racing to create it (a hook-started
    daemon and `synergo up`, say) exactly one writes and the other reads what
    it wrote: both end up with the same secret. A malformed file is replaced.
    """
    path = path or token_path()
    existing = read_token(path)
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
    except FileExistsError:
        # Someone else created it (or a malformed one is there). Only an
        # empty file can be a racing creator mid-write, so only that is worth
        # waiting for; anything else is replaced at once.
        deadline = time.monotonic() + RACE_WAIT_SECONDS
        while _is_empty(path) and time.monotonic() < deadline:
            time.sleep(0.02)
        existing = read_token(path)
        if existing is not None:
            return existing
        write_private(path, token)
        # Two processes replacing the same malformed file both get here; the
        # last writer wins, so return what is actually on disk.
        return read_token(path) or token
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    restrict_permissions(path)
    return token


def auth_headers(token: Optional[str] = None) -> dict[str, str]:
    """{X-Synergo-Token: ...} from the token file, or {} when there is none
    (a request without it is refused, which callers report)."""
    token = token if token is not None else read_token()
    return {TOKEN_HEADER: token} if token else {}
