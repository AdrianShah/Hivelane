"""End-to-end encryption for rooms whose `.hivelane.toml` says `e2e = true`.

The relay matches overlaps on equality alone, so it never needs to read a
path, a symbol, a diff or a prompt. In an e2e room the daemon sends it:

* keyed hashes in place of names: every file path, symbol and branch in a
  cleartext field becomes `h:` + the first 32 hex digits of
  HMAC-SHA256(mac_key, value). The same value under the same key always
  hashes the same, so two teammates' edits to `server/room.py` still collide
  on the relay; without the key the relay cannot test a guess cheaply.
* empty strings in place of content (diffs, prompts, plan summaries), and
* the real values in the message's opaque `e2e` blob: AES-256-GCM under the
  room key, which the relay copies onto its broadcasts untouched so the
  teammates' daemons and dashboards (who hold the key) can read them.

Key schedule (all of it reproducible in browser WebCrypto; see
protocol/e2e-test-vectors.json, checked by both the Python and the dashboard
tests). Version 3, the only one this module reads or writes:

    ROOM    = normalize_room(code): every run of whitespace and "-" removed,
              then upper case (the relay's normalize_room_code, so every
              spelling the relay maps to one room derives one key)
    salt    = "hivelane-e2e-v3:" + ROOM           (UTF-8)
    stretch = PBKDF2-HMAC-SHA256(NFC(passphrase) UTF-8, salt, 600000 iterations, 32 bytes)
    enc_key = HKDF-SHA256(ikm=stretch, salt=salt, info="enc", length=32)
    mac_key = the same with info="mac"
    blob    = "3." + base64url_nopad(nonce(12 random bytes) || ciphertext || tag(16))
              with AES-256-GCM associated data = the client message type the
              blob was made for ("activity", "intent", "plan", "edit_check",
              "task", "pending").
              seal() pads the JSON plaintext with trailing spaces to a size
              bucket, so a blob's length says little about a prompt's.
    hash    = "h:" + first 32 hex of HMAC-SHA256(mac_key, value UTF-8)
    repo_id = hash("repo:" + repo_id)  (RoomKey.repo_token)

PBKDF2 rather than scrypt or Argon2 because the dashboard must derive the
same key with nothing but WebCrypto, which offers PBKDF2 and HKDF only;
600000 iterations is OWASP's floor for PBKDF2-HMAC-SHA256. It makes each
offline guess by a relay cost ~1.2 million SHA-256 compressions instead of
~10. It does not rescue a dictionary word: `passphrase_problem` flags those.

Earlier versions (1 and 2) used other labels and are no longer read: a
blob or hash they made does not match a v3 key. The relay keeps a room's
history for at most a day, and it refuses members whose protocol predates v3
keys from an e2e room, so a room cannot silently mix key versions.

The passphrase comes from HIVELANE_ROOM_KEY or the personal config's
`room_keys` table, never from the committed project file.

HKDF, PBKDF2 and HMAC are stdlib; AES-GCM needs the `cryptography` package,
the optional extra `hivelane[e2e]`, imported only when a key is actually used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import unicodedata
from functools import lru_cache
from typing import Any, Optional

# Part of the key derivation: changing the salt changes every key (and the
# shared vectors in protocol/e2e-test-vectors.json), so it moves only with a
# new CURRENT_VERSION and BLOB_PREFIX.
SALT_PREFIX = "hivelane-e2e-v3:"
CURRENT_VERSION = 3
PBKDF2_ITERATIONS = 600_000
BLOB_PREFIX = "3."
HASH_PREFIX = "h:"
HASH_HEX_CHARS = 32
NONCE_BYTES = 12
KEY_BYTES = 32
GENERATED_KEY_BYTES = 32
# A passphrase a human chose: below these `passphrase_problem` objects.
MIN_PASSPHRASE_CHARS = 16
MIN_PASSPHRASE_BITS = 80
# The client message types a blob can be made for: its associated data.
ENVELOPE_TYPES = ("activity", "intent", "plan", "edit_check", "task", "pending")
INSTALL_HINT = "pip install 'hivelane[e2e]'  (or: pipx inject hivelane cryptography)"


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


_B64URL = re.compile(r"[A-Za-z0-9_-]*")


def b64url_decode(text: str) -> bytes:
    """Strict base64url without padding: the inputs the dashboard's decoder
    accepts, no more (urlsafe_b64decode silently skips junk characters)."""
    text = text.strip()
    if not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# Exactly the relay's rule (server/room_manager.py: _CODE_SEPARATORS).
_ROOM_SEPARATORS = re.compile(r"[\s\-]+")


def normalize_room(room: str) -> str:
    """The canonical room code keys are derived from, identical to the
    relay's normalize_room_code: every whitespace and "-" removed, then
    upper case. `k7q2-xm9p`, ` K7Q2 XM9P ` and `K7Q2XM9P` are one relay
    room, so they must be one key (and one `room_keys` entry)."""
    return _ROOM_SEPARATORS.sub("", room or "").upper()


def normalize_passphrase(passphrase: str) -> str:
    """NFC, so a keyboard that sends a precomposed `é` and one that sends
    `e` + U+0301 give the same key."""
    return unicodedata.normalize("NFC", passphrase)


def generate_passphrase() -> str:
    """A fresh room passphrase: 256 random bits, url-safe (pastes cleanly into
    a terminal, an env var and a `#k=` URL fragment)."""
    return secrets.token_urlsafe(GENERATED_KEY_BYTES)


def passphrase_bits(passphrase: str) -> float:
    """A rough strength estimate: effective length times log2 of the pool of
    character classes used. Repeated characters count a quarter each."""
    if not passphrase:
        return 0.0
    pool = 0
    if any(c.isascii() and c.islower() for c in passphrase):
        pool += 26
    if any(c.isascii() and c.isupper() for c in passphrase):
        pool += 26
    if any(c.isascii() and c.isdigit() for c in passphrase):
        pool += 10
    if any(c.isascii() and not c.isalnum() for c in passphrase):
        pool += 33
    if any(not c.isascii() for c in passphrase):
        pool += 100
    distinct = len(set(passphrase))
    effective = distinct + 0.25 * (len(passphrase) - distinct)
    return effective * math.log2(max(pool, 2))


def passphrase_problem(passphrase: str) -> Optional[str]:
    """None when `passphrase` is strong enough to be a room key, else a
    one-line reason. The relay holds known-plaintext hashes (the `main`
    branch, public commit SHAs), so a weak passphrase falls to an offline
    guessing attack even through PBKDF2. `hivelane key set` and a
    HIVELANE_ROOM_KEY from the environment should warn (or refuse) on this."""
    if not passphrase:
        return "an empty passphrase is not a room key"
    if len(passphrase) < MIN_PASSPHRASE_CHARS:
        return (
            f"this passphrase is only {len(passphrase)} characters; a relay can guess it offline. "
            f"Use at least {MIN_PASSPHRASE_CHARS} characters, or a generated key"
        )
    if passphrase_bits(passphrase) < MIN_PASSPHRASE_BITS:
        return (
            "this passphrase looks guessable (repeated or few kinds of characters); "
            "use a generated key or a longer passphrase"
        )
    return None


def is_hashed(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == len(HASH_PREFIX) + HASH_HEX_CHARS
        and value.startswith(HASH_PREFIX)
    )


PAD_MIN = 256
PAD_LINEAR_STEP = 16 * 1024


def pad_plaintext(data: bytes) -> bytes:
    """`data` + spaces up to the next bucket: powers of two from 256 bytes to
    16 KiB, then multiples of 16 KiB (doubling a 100 KB diff would push it
    over the relay's blob limit)."""
    size = len(data)
    if size <= PAD_MIN:
        target = PAD_MIN
    elif size <= PAD_LINEAR_STEP:
        target = 1 << (size - 1).bit_length()
    else:
        target = -(-size // PAD_LINEAR_STEP) * PAD_LINEAR_STEP
    return data + b" " * (target - size)


def _utf8(text: str) -> bytes:
    """UTF-8 that never raises: a lone surrogate (a JS agent's truncated
    emoji) becomes U+FFFD instead of poisoning every later flush."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace")


class RoomKey:
    """The keys derived from a room's passphrase, and what they do."""

    __slots__ = ("room", "version", "enc_key", "mac_key", "_aead")

    def __init__(self, room: str, enc_key: bytes, mac_key: bytes) -> None:
        self.room = normalize_room(room)
        self.version = CURRENT_VERSION
        self.enc_key = enc_key
        self.mac_key = mac_key
        self._aead: Any = None

    def __repr__(self) -> str:  # never print key material
        return f"RoomKey(room={self.room!r}, v{self.version})"

    @classmethod
    def derive(cls, passphrase: str, room: str) -> "RoomKey":
        return derive(passphrase, room)

    # --- names ------------------------------------------------------------------

    def hash(self, value: str) -> str:
        """`h:` + 32 hex of HMAC-SHA256(mac_key, value). "" stays "" (an empty
        branch means "unknown" to the relay, and must keep meaning that)."""
        if not value:
            return value
        digest = hmac.new(self.mac_key, _utf8(value), hashlib.sha256).hexdigest()
        return HASH_PREFIX + digest[:HASH_HEX_CHARS]

    def repo_token(self, repo_id: str) -> str:
        """What to send as `repo_id` in an e2e room: keyed, so the relay can
        still tell clones of one repo apart from others (equality) but can no
        longer confirm a guessed origin URL or look up a public root commit.
        "" stays ""."""
        if not repo_id:
            return repo_id
        return self.hash("repo:" + repo_id)

    # --- blobs ------------------------------------------------------------------

    def _cipher(self) -> Any:
        if self._aead is None:
            self._aead = _aesgcm(self.enc_key)
        return self._aead

    def encrypt(self, message_type: str, plaintext: bytes, nonce: Optional[bytes] = None) -> str:
        """`3.` + base64url(nonce || ciphertext || tag), message_type as
        associated data. `nonce` is for test vectors only: reusing one under
        a key breaks GCM, so real callers always let it be random."""
        if nonce is None:
            nonce = os.urandom(NONCE_BYTES)
        if len(nonce) != NONCE_BYTES:
            raise ValueError("AES-GCM nonces here are 96 bits")
        sealed = self._cipher().encrypt(nonce, plaintext, message_type.encode("utf-8"))
        return BLOB_PREFIX + b64url_encode(nonce + sealed)

    def decrypt(self, message_type: str, blob: str) -> bytes:
        if not isinstance(blob, str):
            raise DecryptError("not a blob")
        blob = blob.strip()
        if not blob.startswith(BLOB_PREFIX):
            raise DecryptError(
                "made with an older room key version (every member must run a current hivelane)"
            )
        body = blob[len(BLOB_PREFIX):]
        try:
            raw = b64url_decode(body)
        except (ValueError, TypeError) as exc:
            raise DecryptError(f"not a base64url blob ({exc})") from None
        if len(raw) < NONCE_BYTES + 16:
            raise DecryptError("blob too short")
        try:
            from cryptography.exceptions import InvalidTag
        except ImportError:  # pragma: no cover - _cipher() raises first
            InvalidTag = ValueError  # noqa: N806
        cipher = self._cipher()
        try:
            return cipher.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], message_type.encode("utf-8"))
        except InvalidTag:
            raise DecryptError("wrong room key (or a tampered message)") from None

    def seal(self, message_type: str, envelope: dict[str, Any]) -> str:
        """encrypt() of the envelope's compact JSON, padded with trailing
        spaces, which every JSON parser ignores, to a size bucket."""
        text = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        return self.encrypt(message_type, pad_plaintext(_utf8(text)))

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


def _salt(prefix: str, room: str) -> bytes:
    return (prefix + room).encode("utf-8")


def stretch(passphrase: str, room: str) -> bytes:
    """The PBKDF2 step (exposed for the test vectors)."""
    pw = _utf8(normalize_passphrase(passphrase))
    return hashlib.pbkdf2_hmac(
        "sha256", pw, _salt(SALT_PREFIX, normalize_room(room)), PBKDF2_ITERATIONS, KEY_BYTES
    )


@lru_cache(maxsize=64)
def _derive_cached(passphrase: str, room: str) -> RoomKey:
    # PBKDF2 at 600k iterations costs ~0.5 s; a daemon re-derives the same
    # room's key on every (re)register, so remember it.
    if not passphrase:
        raise ValueError("an empty passphrase is not a room key")
    salt = _salt(SALT_PREFIX, normalize_room(room))
    ikm = stretch(passphrase, room)
    return RoomKey(room, hkdf_sha256(ikm, salt, b"enc"), hkdf_sha256(ikm, salt, b"mac"))


def derive(passphrase: str, room: str) -> RoomKey:
    """The room key. Slow (~0.5 s) the first time for a (passphrase, room):
    call it off the event loop where that matters."""
    return _derive_cached(passphrase, room)
