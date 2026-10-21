"""Tests for server/semantic.py (intent similarity + prompt -> file matching)."""

import json
from pathlib import Path

import pytest

try:
    from server import semantic
except ImportError:  # running with server/ on sys.path
    import semantic  # type: ignore[no-redef]

FIXTURE = Path(__file__).parent / "fixtures" / "intent_pairs.json"


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    monkeypatch.delenv("HIVELANE_EMBEDDINGS", raising=False)
    semantic._embedding_scorer.cache_clear()
    yield
    semantic._embedding_scorer.cache_clear()


def _precision_recall(threshold: float = semantic.DEFAULT_THRESHOLD):
    pairs = json.loads(FIXTURE.read_text())
    tp = fp = fn = 0
    for pair in pairs:
        hit = bool(semantic.similar_intents(pair["a"], {"x": pair["b"]}, threshold))
        if hit and pair["similar"]:
            tp += 1
        elif hit:
            fp += 1
        elif pair["similar"]:
            fn += 1
    return tp / max(1, tp + fp), tp / max(1, tp + fn), len(pairs)


def test_fixture_precision_recall_at_default_threshold():
    precision, recall, n = _precision_recall()
    assert n >= 40
    assert precision >= 0.9
    assert recall >= 0.9


def test_similar_intents_ranks_and_filters():
    others = {
        "bob": "Spectators are missing events from the room broadcast",
        "carol": "Translate the landing page into Spanish",
        "dave": "",
    }
    result = semantic.similar_intents("Fix the room broadcast for spectators", others)
    assert [user for user, _ in result] == ["bob"]
    assert 0.8 <= result[0][1] <= 1.0


def test_similar_intents_empty_text():
    assert semantic.similar_intents("   ", {"bob": "anything"}) == []
    assert semantic.similar_intents("the a of", {"bob": "the a of"}) == []


def test_tokens_stem_and_split():
    assert semantic.tokens("RoomManager handles caching") == ["room", "manag", "handl", "cach"]


def test_embedding_backend_falls_back_when_missing(monkeypatch):
    monkeypatch.setenv("HIVELANE_EMBEDDINGS", "fastembed")
    monkeypatch.setitem(__import__("sys").modules, "fastembed", None)
    semantic._embedding_scorer.cache_clear()
    score, embedded = semantic.similarity("fix room", "fix room")
    assert embedded is False and score > 0.9


PATHS = [
    "server/room.py",
    "server/room_manager.py",
    "server/main.py",
    "server/store.py",
    "frontend/src/components/Lane.tsx",
    "frontend/src/components/ConflictBanner.tsx",
    "hivelane/daemon.py",
]


@pytest.mark.parametrize(
    "prompt,expected_first",
    [
        ("fix the room broadcast", "server/room.py"),
        ("refactor server/store.py caching", "server/store.py"),
        ("the Lane component shows wrong symbols", "frontend/src/components/Lane.tsx"),
        ("RoomManager evicts rooms too early", "server/room_manager.py"),
        ("make the conflict banner dismissible", "frontend/src/components/ConflictBanner.tsx"),
        ("daemon reconnect backoff", "hivelane/daemon.py"),
        ("edit main.py startup", "server/main.py"),
    ],
)
def test_files_mentioned(prompt, expected_first):
    assert semantic.files_mentioned(prompt, PATHS)[0] == expected_first


def test_files_mentioned_skips_generic_stems_and_unrelated():
    assert "server/main.py" not in semantic.files_mentioned("the main problem is speed", PATHS)
    assert semantic.files_mentioned("translate the landing page", PATHS) == []
    assert semantic.files_mentioned("", PATHS) == []
