"""The `suggestion` on a blocked or warned edit_check: one short, actionable
alternative built from what the room already knows. Deterministic (same
room state, same text) and model-free, because it is on the agent's blocking
hook.

What it can offer, in order of preference:

1. another file to work on meanwhile: this module's tests if the room has
   seen them (tests/test_<stem>.py, <stem>_test.go, <stem>.test.ts, ...),
   else the next file of the requester's own active plan, else a file in the
   same directory; never one an active teammate is touching, planning or
   queued for, and never a manifest or lockfile;
2. how long to wait: when the blocking touch lapses, or how long the member
   ahead has left to take a released file.

In an end-to-end encrypted room the relay sees only hashed paths, which have
no directories or names to reason about, so only the wait is offered.
"""

from __future__ import annotations

import math
import posixpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Optional

HASH_PREFIX = "h:"

# Room passes room.is_always_warn_file: manifests and lockfiles make poor
# "work on this instead" suggestions.
AlwaysWarn = Optional[Callable[[str], bool]]

# Test-file names for a module stem, across the ecosystems agents work in.
_TEST_NAMES = (
    "test_{stem}.py",
    "{stem}_test.py",
    "tests_{stem}.py",
    "{stem}_test.go",
    "{stem}.test.ts",
    "{stem}.test.tsx",
    "{stem}.test.js",
    "{stem}.test.jsx",
    "{stem}.spec.ts",
    "{stem}.spec.tsx",
    "{stem}.spec.js",
    "{stem}_spec.rb",
    "{stem}_test.rs",
    "{stem}Test.java",
    "{stem}Test.kt",
    "{stem}Tests.cs",
)


@dataclass(frozen=True)
class WaitHint:
    """How long until the requester can expect the file, and why."""

    seconds: float
    why: str


def _stem(path: str) -> str:
    name = posixpath.basename(path)
    for suffix in (".test", ".spec"):
        root, ext = posixpath.splitext(name)
        if root.endswith(suffix):
            return root[: -len(suffix)]
    return posixpath.splitext(name)[0]


def test_names(file_path: str) -> set[str]:
    stem = _stem(file_path)
    if not stem:
        return set()
    return {pattern.format(stem=stem) for pattern in _TEST_NAMES}


def is_test_path(path: str) -> bool:
    name = posixpath.basename(path)
    stem = posixpath.splitext(name)[0]
    parts = path.split("/")[:-1]
    return (
        name.startswith("test_")
        or stem.endswith(("_test", "_spec", "Test", "Tests"))
        or ".test." in name
        or ".spec." in name
        or any(part in ("tests", "test", "__tests__", "spec") for part in parts)
    )


def _shared_dirs(a: str, b: str) -> int:
    count = 0
    for left, right in zip(a.split("/"), b.split("/")):
        if left != right:
            break
        count += 1
    return count


def pick_alternative(
    file_path: str,
    known: Iterable[str],
    busy: set[str],
    own_plan: Iterable[str] = (),
    *,
    always_warn: AlwaysWarn = None,
) -> Optional[tuple[str, str]]:
    """(path, why) of a file to work on instead, or None. why is "tests",
    "plan" or "nearby"."""
    if not file_path or file_path.startswith(HASH_PREFIX):
        return None

    def usable(path: str) -> bool:
        return (
            bool(path)
            and path != file_path
            and not path.startswith(HASH_PREFIX)
            and path not in busy
            and not (always_warn is not None and always_warn(path))
        )

    candidates = sorted({path for path in known if usable(path)})
    directory = posixpath.dirname(file_path)

    if not is_test_path(file_path):
        names = test_names(file_path)
        tests = [path for path in candidates if posixpath.basename(path) in names]
        if tests:
            tests.sort(key=lambda path: (-_shared_dirs(posixpath.dirname(path), directory), path))
            return tests[0], "tests"

    for path in own_plan:
        if usable(path):
            return path, "plan"

    nearby = [path for path in candidates if posixpath.dirname(path) == directory]
    if nearby:
        return nearby[0], "nearby"
    return None


def wait_text(hint: Optional[WaitHint]) -> str:
    if hint is None:
        return ""
    seconds = max(1, math.ceil(hint.seconds))
    return f"wait ~{seconds}s ({hint.why})"


def build_suggestion(
    decision: str,
    file_path: str,
    *,
    known: Iterable[str] = (),
    busy: Optional[set[str]] = None,
    own_plan: Iterable[str] = (),
    wait: Optional[WaitHint] = None,
    always_warn: AlwaysWarn = None,
) -> str:
    """The suggestion for a block or warn ("" for allow, or nothing to say)."""
    if decision not in ("block", "warn"):
        return ""
    alternative = pick_alternative(
        file_path, known, busy or set(), own_plan, always_warn=always_warn
    )
    alt = ""
    if alternative is not None:
        path, why = alternative
        note = {"tests": " (tests for this module)", "plan": " (next in your plan)"}.get(why, "")
        if decision == "block":
            alt = f"try {path}{note} meanwhile"
        else:
            alt = f"{path}{note} is free if you'd rather stay clear"
    if decision == "warn":
        return alt
    waiting = wait_text(wait)
    if alt and waiting:
        return f"{alt}, or {waiting}"
    return alt or waiting
