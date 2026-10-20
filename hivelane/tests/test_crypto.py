"""hivelane.crypto: the e2e key schedule, blobs and hashes, pinned by the test
vectors the dashboard's WebCrypto implementation is checked against too
(protocol/e2e-test-vectors.json; frontend/tests/e2e-crypto.test.ts)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from hivelane import crypto

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "protocol" / "e2e-test-vectors.json").read_text(encoding="utf-8")
)


def test_hkdf_matches_rfc5869_case_1() -> None:
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = crypto.hkdf_sha256(ikm, salt, info, 42)
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["room"])
def test_vectors(vector: dict) -> None:
    key = crypto.derive(vector["passphrase"], vector["room"], version=1)
    assert key.enc_key.hex() == vector["enc_key_hex"]
    assert key.mac_key.hex() == vector["mac_key_hex"]
    for case in vector["encryptions"]:
        nonce = bytes.fromhex(case["nonce_hex"])
        assert key.encrypt(case["type"], case["plaintext"].encode("utf-8"), nonce=nonce) == case["blob"]
        assert key.decrypt(case["type"], case["blob"]).decode("utf-8") == case["plaintext"]
        kind, envelope = key.open(case["blob"])
        assert kind == case["type"]
        assert envelope == json.loads(case["plaintext"])
    for case in vector["hashes"]:
        assert key.hash(case["value"]) == case["token"]
        assert crypto.is_hashed(case["token"])


def test_room_code_case_does_not_matter() -> None:
    a = crypto.derive("pw", "k7q2xm")
    b = crypto.derive("pw", " K7Q2XM ")
    c = crypto.derive("pw", "k7q2-xm")
    assert (a.enc_key, a.mac_key) == (b.enc_key, b.mac_key) == (c.enc_key, c.mac_key)
    assert a.enc_key != a.mac_key
    assert crypto.derive("pw", "OTHER").mac_key != a.mac_key


def test_round_trip_random_nonces_and_associated_data() -> None:
    key = crypto.derive("correct horse", "ROOM1")
    one = key.seal("activity", {"prompt": "é → ✓", "changes": []})
    two = key.seal("activity", {"prompt": "é → ✓", "changes": []})
    assert one != two  # fresh nonce every time
    assert key.open(one) == ("activity", {"prompt": "é → ✓", "changes": []})
    # The type is authenticated: an activity blob is not an intent blob.
    with pytest.raises(crypto.DecryptError):
        key.decrypt("intent", one)
    with pytest.raises(crypto.DecryptError):
        key.open(one, types=("intent", "plan"))


def test_wrong_key_and_tampering_are_refused() -> None:
    vector = VECTORS["vectors"][0]
    blob = vector["encryptions"][0]["blob"]
    wrong = crypto.derive(VECTORS["wrong_key"]["passphrase"], vector["room"], version=1)
    with pytest.raises(crypto.DecryptError, match="wrong room key"):
        wrong.open(blob)
    right = crypto.derive(vector["passphrase"], vector["room"], version=1)
    tampered = blob[:-2] + ("A" if blob[-2] != "A" else "B") + blob[-1]
    with pytest.raises(crypto.DecryptError):
        right.open(tampered)
    for junk in ("", "!!!", "AAAA"):
        with pytest.raises(crypto.DecryptError):
            right.open(junk)


def test_hash_keeps_empty_values_empty_and_is_keyed() -> None:
    key = crypto.derive("pw", "ROOM")
    assert key.hash("") == ""
    token = key.hash("server/room.py")
    assert token.startswith("h:") and len(token) == 34
    assert crypto.derive("other", "ROOM").hash("server/room.py") != token
    assert "pw" not in repr(key) and key.enc_key.hex() not in repr(key)


def test_generated_passphrases_are_long_and_url_safe() -> None:
    keys = {crypto.generate_passphrase() for _ in range(20)}
    assert len(keys) == 20
    assert all(len(k) >= 32 and k.replace("-", "").replace("_", "").isalnum() for k in keys)


def test_missing_extra_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_cryptography(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cryptography)
    # A fresh RoomKey: derive() is cached, and a cached key may already hold
    # an AESGCM instance from an earlier test.
    key = crypto.RoomKey("ROOM", bytes(32), bytes(range(32)))
    assert key.hash("a.py").startswith("h:")  # HMAC is stdlib: still works
    assert crypto.available() is False
    with pytest.raises(crypto.E2EUnavailable, match=r"hivelane\[e2e\]"):
        key.seal("intent", {"prompt": "x"})


# --- v2: canonical room codes, PBKDF2, NFC, "2." blobs, padding ----------------


@pytest.mark.parametrize("vector", VECTORS["vectors_v2"], ids=lambda v: v["canonical_room"] + "/" + v["room"].strip())
def test_v2_vectors(vector: dict) -> None:
    assert crypto.normalize_room(vector["room"]) == vector["canonical_room"]
    assert vector["salt"] == crypto.SALT_PREFIX + vector["canonical_room"]
    assert vector["pbkdf2_iterations"] == crypto.PBKDF2_ITERATIONS
    assert crypto.stretch_v2(vector["passphrase"], vector["room"]).hex() == vector["stretched_hex"]
    key = crypto.derive(vector["passphrase"], vector["room"])
    assert key.version == 2
    assert key.enc_key.hex() == vector["enc_key_hex"]
    assert key.mac_key.hex() == vector["mac_key_hex"]
    for case in vector["encryptions"]:
        padded = crypto.pad_plaintext(case["plaintext"].encode("utf-8"))
        assert len(padded) == case["padded_length"]
        nonce = bytes.fromhex(case["nonce_hex"])
        assert key.encrypt(case["type"], padded, nonce=nonce) == case["blob"]
        assert case["blob"].startswith("2.")
        assert key.open(case["blob"]) == (case["type"], json.loads(case["plaintext"]))
    for case in vector["hashes"]:
        assert key.hash(case["value"]) == case["token"]
    for case in vector["repo_tokens"]:
        assert key.repo_token(case["repo_id"]) == case["token"]


def test_room_code_with_separators_derives_the_relays_room_key() -> None:
    """Regression (finding 2): `k7q2-xm9p` and the dashboard's `K7Q2XM9P` were
    different HKDF salts. The relay strips spaces and dashes; so do we."""
    assert crypto.normalize_room("k7q2-xm9p") == "K7Q2XM9P"
    assert crypto.normalize_room(" k7q2 \t xm9p ") == "K7Q2XM9P"
    assert crypto.normalize_room("a--b - c") == "ABC"
    dashed = crypto.derive("correct horse battery staple", "k7q2-xm9p")
    plain = crypto.derive("correct horse battery staple", "K7Q2XM9P")
    assert dashed.mac_key == plain.mac_key and dashed.room == "K7Q2XM9P"


def test_room_normalisation_matches_the_relay() -> None:
    from server.room_manager import normalize_room_code

    for raw in ("k7q2-xm9p", " AB CD ", "a b", "x-_y", "team-42", "\tq\nr"):
        assert crypto.normalize_room(raw) == normalize_room_code(raw)


def test_nfd_and_nfc_passphrases_are_one_key() -> None:
    nfc = crypto.derive("café au lait, extra long", "ROOM")
    nfd = crypto.derive("café au lait, extra long", "ROOM")
    assert nfc.enc_key == nfd.enc_key


def test_v2_key_reads_v1_blobs_but_writes_v2() -> None:
    vector = VECTORS["vectors"][0]
    key = crypto.derive(vector["passphrase"], vector["room"])
    case = vector["encryptions"][0]
    assert key.open(case["blob"]) == (case["type"], json.loads(case["plaintext"]))
    fresh = key.seal("intent", {"prompt": "x"})
    assert fresh.startswith("2.")
    old = crypto.derive(vector["passphrase"], vector["room"], version=1)
    with pytest.raises(crypto.DecryptError, match="v2"):
        old.open(fresh)


def test_v2_wrong_key_is_refused() -> None:
    vector = VECTORS["vectors_v2"][0]
    wrong = crypto.derive(VECTORS["wrong_key_v2"]["passphrase"], vector["room"])
    with pytest.raises(crypto.DecryptError, match="wrong room key"):
        wrong.open(vector["encryptions"][0]["blob"])


def test_v2_is_stretched_not_bare_hkdf() -> None:
    v1 = crypto.derive("correct horse battery staple", "K7Q2XM", version=1)
    v2 = crypto.derive("correct horse battery staple", "K7Q2XM")
    assert v1.enc_key != v2.enc_key and v1.mac_key != v2.mac_key
    assert crypto.PBKDF2_ITERATIONS >= 600_000


def test_padding_hides_exact_lengths() -> None:
    key = crypto.derive("correct horse battery staple", "ROOM")
    assert len(key.seal("intent", {"prompt": "a"})) == len(key.seal("intent", {"prompt": "a" * 150}))
    assert crypto.pad_plaintext(b"x" * 300) == b"x" * 300 + b" " * 212
    assert len(crypto.pad_plaintext(b"x" * 20000)) == 32 * 1024
    assert len(crypto.pad_plaintext(b"x" * 100_000)) == 7 * 16 * 1024


def test_strict_base64_rejects_junk() -> None:
    """Regression (finding 13): `$$\\n` inside a blob used to be skipped."""
    vector = VECTORS["vectors"][0]
    key = crypto.derive(vector["passphrase"], vector["room"], version=1)
    blob = vector["encryptions"][0]["blob"]
    with pytest.raises(crypto.DecryptError):
        key.open(blob[:10] + "$$\n" + blob[10:])
    with pytest.raises(crypto.DecryptError):
        key.open(blob.replace("-", "+"))


def test_lone_surrogates_do_not_raise() -> None:
    key = crypto.derive("correct horse battery staple", "ROOM")
    assert key.hash("src/\ud83d.ts").startswith("h:")
    _, envelope = key.open(key.seal("intent", {"prompt": "half an emoji \ud83d"}))
    assert envelope["prompt"].startswith("half an emoji")


def test_repo_token_is_keyed_and_stable() -> None:
    a = crypto.derive("correct horse battery staple", "ROOM")
    b = crypto.derive("another long passphrase here", "ROOM")
    assert a.repo_token("abc123") == a.repo_token("abc123")
    assert a.repo_token("abc123") != b.repo_token("abc123")
    assert a.repo_token("abc123") != a.hash("abc123")  # separated from path hashes
    assert a.repo_token("") == ""


@pytest.mark.parametrize(
    "weak", ["acme2024", "password", "correcthorse", "a" * 20, "1" * 22, "abababababababababab"]
)
def test_weak_passphrases_are_flagged(weak: str) -> None:
    assert crypto.passphrase_problem(weak)


@pytest.mark.parametrize("strong", ["correct horse battery staple", "Tr0ub4dor&3-Xylophone-Quartz"])
def test_strong_passphrases_pass(strong: str) -> None:
    assert crypto.passphrase_problem(strong) is None


def test_generated_keys_are_256_bit_and_pass_the_check() -> None:
    for _ in range(20):
        key = crypto.generate_passphrase()
        assert len(key) >= 43
        assert crypto.passphrase_problem(key) is None


def test_derive_is_cached() -> None:
    k1 = crypto.derive("correct horse battery staple", "ROOMC")
    assert crypto.derive("correct horse battery staple", "ROOMC") is k1
