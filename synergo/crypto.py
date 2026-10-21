"""End-to-end encryption for rooms whose `.synergo.toml` says `e2e = true`.

The relay matches overlaps on equality alone, so it never needs to read a
path, a symbol, a diff or a prompt. In an e2e room the daemon sends it:

* keyed hashes in place of names: every file path, symbol and branch in a
  cleartext field becomes `h:` + the first 32 hex digits of
  HMAC-SHA256(mac_key, value). The same value under the same key always
  hashes the same, so two teammates' edits to `server/room.py` still collide
  on the relay; without the key the relay cannot test a guess.
* empty strings in place of content (diffs, prompts, plan summaries), and
* the real values in the message's opaque `e2e` blob: AES-256-GCM under the
  room key, which the relay copies onto its broadcasts untouched so the
  teammates' daemons and dashboards (who hold the key) can read them.

Key schedule (all of it reproducible in browser WebCrypto; see
protocol/e2e-test-vectors.json, checked by both the Python and the dashboard
tests):

    enc_key = HKDF-SHA256(ikm=passphrase (UTF-8),
                          salt="synergo-e2e-v1:" + ROOM_CODE (uppercase),
                          info="enc", length=32)
    mac_key = the same with info="mac"
    blob    = base64url_nopad(nonce(12 random bytes) || ciphertext || tag(16))
              with AES-256-GCM associated data = the client message type the
              blob was made for ("activity", "intent", "plan", "edit_check")

The passphrase comes from SYNERGO_ROOM_KEY or the personal config's
`room_keys` table, never from the committed project file.

HKDF and HMAC are stdlib; AES-GCM needs the `cryptography` package, the
optional extra `synergo[e2e]`, imported only when a key is actually used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Optional

SALT_PREFIX = "synergo-e2e-v1:"
HASH_PREFIX = "h:"
HASH_HEX_CHARS = 32
NONCE_BYTES = 12
KEY_BYTES = 32
# The client message types a blob can be made for: its associated data.
ENVELOPE_TYPES = ("activity", "intent", "plan", "edit_check")
INSTALL_HINT = "pip install 'synergo[e2e]'  (or: pipx inject synergo cryptography)"


class E2EUnavailable(Exception):
    """e2e is configured but the `cryptography` package is not installed."""


class DecryptError(Exception):
    """A blob that this key cannot open (wrong key, wrong type, tampered)."""


def _aesgcm(key: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise E2EUnavailable(
            "this room is end-to-end encrypted, which needs the `cryptography` package: "
            + INSTALL_HINT
        ) from None
    return AESGCM(key)


def available() -> bool:
    """True when AES-GCM can be used (the e2e extra is installed)."""
    try:
        _aesgcm(bytes(KEY_BYTES))
    except E2EUnavailable:
        return False
    return True


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    """RFC 5869 HKDF with SHA-256 (extract then expand)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def normalize_room(room: str) -> str:
    return (room or "").strip().upper()


def generate_passphrase() -> str:
    """A fresh room passphrase: 192 random bits, url-safe (pastes cleanly into
    a terminal, an env var and a `#k=` URL fragment)."""
    return secrets.token_urlsafe(24)


def is_hashed(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == len(HASH_PREFIX) + HASH_HEX_CHARS
        and value.startswith(HASH_PREFIX)
    )


class RoomKey:
    """The two keys derived from a room's passphrase, and what they do."""

    __slots__ = ("room", "enc_key", "mac_key", "_aead")

    def __init__(self, room: str, enc_key: bytes, mac_key: bytes) -> None:
        self.room = normalize_room(room)
        self.enc_key = enc_key
        self.mac_key = mac_key
        self._aead: Any = None

    def __repr__(self) -> str:  # never print key material
        return f"RoomKey(room={self.room!r})"

    @classmethod
    def derive(cls, passphrase: str, room: str) -> "RoomKey":
        if not passphrase:
            raise ValueError("an empty passphrase is not a room key")
        ikm = passphrase.encode("utf-8")
        salt = (SALT_PREFIX + normalize_room(room)).encode("utf-8")
        return cls(
            room,
            hkdf_sha256(ikm, salt, b"enc"),
            hkdf_sha256(ikm, salt, b"mac"),
        )

    # --- names ------------------------------------------------------------------

    def hash(self, value: str) -> str:
        """`h:` + 32 hex of HMAC-SHA256(mac_key, value). "" stays "" (an empty
        branch means "unknown" to the relay, and must keep meaning that)."""
        if not value:
            return value
        digest = hmac.new(self.mac_key, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return HASH_PREFIX + digest[:HASH_HEX_CHARS]

    # --- blobs ------------------------------------------------------------------

    def _cipher(self) -> Any:
        if self._aead is None:
            self._aead = _aesgcm(self.enc_key)
        return self._aead

    def encrypt(self, message_type: str, plaintext: bytes, nonce: Optional[bytes] = None) -> str:
        """base64url(nonce || ciphertext || tag), message_type as associated
        data. `nonce` is for test vectors only: reusing one under a key
        breaks GCM, so real callers always let it be random."""
        if nonce is None:
            nonce = os.urandom(NONCE_BYTES)
        if len(nonce) != NONCE_BYTES:
            raise ValueError("AES-GCM nonces here are 96 bits")
        sealed = self._cipher().encrypt(nonce, plaintext, message_type.encode("utf-8"))
        return b64url_encode(nonce + sealed)

    def decrypt(self, message_type: str, blob: str) -> bytes:
        try:
            raw = b64url_decode(blob)
        except (ValueError, TypeError) as exc:
            raise DecryptError(f"not a base64url blob ({exc})") from None
        if len(raw) < NONCE_BYTES + 16:
            raise DecryptError("blob too short")
        try:
            from cryptography.exceptions import InvalidTag
        except ImportError:  # pragma: no cover - _cipher() raises first
            InvalidTag = ValueError  # noqa: N806
        try:
            return self._cipher().decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], message_type.encode("utf-8"))
        except InvalidTag:
            raise DecryptError("wrong room key (or a tampered message)") from None

    def seal(self, message_type: str, envelope: dict[str, Any]) -> str:
        """encrypt() of the envelope's compact JSON."""
        data = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self.encrypt(message_type, data)

    def open(self, blob: str, types: tuple[str, ...] = ENVELOPE_TYPES) -> tuple[str, dict[str, Any]]:
        """(message type, envelope) of a blob made for any of `types`. The relay
        copies blobs onto derived records (a PeerTouch may carry a teammate's
        activity, plan or edit_check blob), so the caller cannot always know
        which type it was made for; GCM authenticates the type either way."""
        last: Optional[DecryptError] = None
        for message_type in types:
            try:
                data = self.decrypt(message_type, blob)
            except DecryptError as exc:
                last = exc
                continue
            try:
                envelope = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise DecryptError(f"envelope is not JSON ({exc})") from None
            if not isinstance(envelope, dict):
                raise DecryptError("envelope is not a JSON object")
            return message_type, envelope
        raise last or DecryptError("no envelope type matched")


def derive(passphrase: str, room: str) -> RoomKey:
    return RoomKey.derive(passphrase, room)
