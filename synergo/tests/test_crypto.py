"""synergo.crypto: the e2e key schedule, blobs and hashes, pinned by the test
vectors the dashboard's WebCrypto implementation is checked against too
(protocol/e2e-test-vectors.json; frontend/tests/e2e-crypto.test.ts)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from synergo import crypto

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
    key = crypto.derive(vector["passphrase"], vector["room"])
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
    assert (a.enc_key, a.mac_key) == (b.enc_key, b.mac_key)
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
    wrong = crypto.derive(VECTORS["wrong_key"]["passphrase"], vector["room"])
    with pytest.raises(crypto.DecryptError, match="wrong room key"):
        wrong.open(blob)
    right = crypto.derive(vector["passphrase"], vector["room"])
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
    key = crypto.derive("pw", "ROOM")
    assert key.hash("a.py").startswith("h:")  # HMAC is stdlib: still works
    assert crypto.available() is False
    with pytest.raises(crypto.E2EUnavailable, match=r"synergo\[e2e\]"):
        key.seal("intent", {"prompt": "x"})
