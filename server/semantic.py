"""Lightweight semantic matching for agent intents (pure, stdlib-only).

Two entry points, both side-effect free so Room (or anything else) can call
them without importing the relay:

* ``similar_intents(text, others, threshold)`` — which peers' declared intents
  describe roughly the same task as ``text``.
* ``files_mentioned(text, known_paths)`` — which known repo paths a prompt is
  talking about ("fix the room broadcast" -> server/room.py).

The default scorer blends character-trigram cosine (robust to inflection and
typos) with token Jaccard over a stemmed, stopword-free token set. If
``SYNERGO_EMBEDDINGS=fastembed`` and the optional ``fastembed`` package is
installed, intent similarity uses sentence embeddings instead; any failure
falls back to the lexical scorer. fastembed is never a dependency.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from functools import lru_cache
from typing import Callable, Optional

# Calibrated on server/tests/fixtures/intent_pairs.json (see test_semantic.py
# and docs/conflict-detection.md). The lexical score is compressed toward the
# middle, so its threshold is much lower than an embedding cosine would need.
DEFAULT_THRESHOLD = 0.8
LEXICAL_THRESHOLD = 0.32
EMBEDDING_THRESHOLD = 0.80

_TRIGRAM_WEIGHT = 0.5

STOPWORDS = frozenset(
    """
    a an the and or but if then so to of in on at by for with from into onto
    about as is are was were be been being it its this that these those there
    here i we you he she they me us my our your his her their them please can
    could should would will shall may might must do does did done doing have
    has had having not no yes all any some each every also just only very too
    up down out over under again more most less least new old make makes made
    get gets got let lets use using used via when where which who whom what
    why how while than now still need needs want wants try trying thing things
    stuff code file files bit way able sure ok okay
    """.split()
)

# Verbs so generic in coding prompts that they say nothing about the task.
GENERIC_VERBS = frozenset(
    "fix add update change implement refactor write clean improve handle "
    "support remove delete create rename move tweak adjust work look check".split()
)

_SUFFIXES = (
    "ations", "ation", "ings", "ing", "ers", "er", "ies", "ied", "es", "ed",
    "ly", "s",
)

_SPLIT_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[A-Za-z0-9]+")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            if suffix in ("ies", "ied"):
                return word[: -len(suffix)] + "y"
            return word[: -len(suffix)]
    return word


def tokens(text: str, *, drop_generic: bool = True) -> list[str]:
    """Lowercase, split camelCase/snake_case/paths, drop stopwords, stem."""
    out: list[str] = []
    for raw in _WORD.findall(_SPLIT_CAMEL.sub(" ", text or "")):
        word = raw.lower()
        if len(word) < 2 or word in STOPWORDS:
            continue
        if drop_generic and word in GENERIC_VERBS:
            continue
        out.append(_stem(word))
    return out


def _trigrams(toks: list[str]) -> Counter:
    grams: Counter = Counter()
    for tok in toks:
        padded = f" {tok} "
        for i in range(len(padded) - 2):
            grams[padded[i : i + 3]] += 1
    return grams


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(count * b.get(gram, 0) for gram, count in a.items())
    na = math.sqrt(sum(c * c for c in a.values()))
    nb = math.sqrt(sum(c * c for c in b.values()))
    return dot / (na * nb)


def lexical_similarity(a: str, b: str) -> float:
    """Blend of trigram cosine and stemmed-token Jaccard, in [0, 1]."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    tri = _cosine(_trigrams(ta), _trigrams(tb))
    return _TRIGRAM_WEIGHT * tri + (1 - _TRIGRAM_WEIGHT) * jaccard


# --- optional embedding backend ----------------------------------------------


@lru_cache(maxsize=1)
def _embedding_scorer() -> Optional[Callable[[str, str], float]]:
    if os.environ.get("SYNERGO_EMBEDDINGS", "").lower() != "fastembed":
        return None
    try:  # lazy, optional
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        model = TextEmbedding()
    except Exception:
        return None

    def score(a: str, b: str) -> float:
        va, vb = list(model.embed([a, b]))
        dot = sum(x * y for x, y in zip(va, vb))
        na = math.sqrt(sum(x * x for x in va))
        nb = math.sqrt(sum(y * y for y in vb))
        return dot / (na * nb) if na and nb else 0.0

    return score


def similarity(a: str, b: str) -> tuple[float, bool]:
    """Score two intents. Returns (score, used_embeddings)."""
    scorer = _embedding_scorer()
    if scorer is not None:
        try:
            return scorer(a, b), True
        except Exception:
            pass
    return lexical_similarity(a, b), False


def similar_intents(
    text: str, others: dict[str, str], threshold: float = DEFAULT_THRESHOLD
) -> list[tuple[str, float]]:
    """Peers whose intent text is similar to ``text``, best match first.

    ``threshold`` is on a normalized 0..1 "confidence" scale: 0.8 (the default)
    maps to the calibrated cut-off of whichever backend is active, so callers
    need not know which scorer ran. Returned scores use the same scale, capped
    at 1.0.
    """
    if not text or not text.strip():
        return []
    results: list[tuple[str, float]] = []
    for user_id, other in others.items():
        if not other or not other.strip():
            continue
        raw, embedded = similarity(text, other)
        cutoff = EMBEDDING_THRESHOLD if embedded else LEXICAL_THRESHOLD
        normalized = min(1.0, raw * DEFAULT_THRESHOLD / cutoff)
        if normalized >= threshold:
            results.append((user_id, round(normalized, 3)))
    results.sort(key=lambda item: item[1], reverse=True)
    return results


# --- prompt -> file matching --------------------------------------------------

_GENERIC_STEMS = frozenset(
    {"main", "index", "init", "__init__", "util", "utils", "test", "tests",
     "app", "lib", "src", "mod", "types", "config", "setup", "common"}
)


def files_mentioned(text: str, known_paths: list[str]) -> list[str]:
    """Known paths the prompt refers to, strongest evidence first.

    Evidence, strongest first: the full path, the basename ("room.py"), the
    stem as a word ("the room broadcast"), a stem that only matches after
    stemming ("rooms"), or a camelCase/snake_case symbol whose parts match the
    stem ("RoomManager" -> room_manager.py). Very generic stems (main, utils,
    index...) need the basename or full path, since they appear in almost any
    sentence.
    """
    if not text:
        return []
    lowered = text.lower().replace("\\", "/")
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    stemmed = {_stem(w) for w in tokens(text, drop_generic=False)}
    symbols = {
        "_".join(p.lower() for p in _SPLIT_CAMEL.sub(" ", raw).split())
        for raw in re.findall(r"[A-Za-z][A-Za-z0-9]*", text)
    }

    scored: list[tuple[int, str]] = []
    for path in known_paths:
        norm = path.replace("\\", "/").lower()
        base = norm.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0]
        orig_stem = path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        snake = "_".join(
            p.lower() for p in _SPLIT_CAMEL.sub(" ", orig_stem).replace("-", "_").split()
        )
        score = 0
        if norm in lowered:
            score = 4
        elif re.search(rf"(?<![\w.]){re.escape(base)}(?!\w)", lowered):
            score = 3
        elif len(stem) >= 3 and stem not in _GENERIC_STEMS:
            if stem in words or stem in symbols or snake in symbols:
                score = 2
            else:
                parts = [_stem(p) for p in snake.split("_") if p]
                if parts and all(p in stemmed for p in parts):
                    score = 1
        if score:
            scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored]
