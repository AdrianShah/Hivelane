"""synergo.depgraph: signature changes (local and from a peer's diff) turn
into "you're editing a caller" notes for files that import and call them."""

from __future__ import annotations

from synergo.daemon import unified_diff
from synergo.depgraph import DepTracker, changed_signatures, diff_signature_changes

OLD = "def foo(a):\n    return a\n\n\ndef bar():\n    return 1\n"
NEW = "def foo(a, b=2):\n    return a + b\n\n\ndef bar():\n    return 1\n"
CALLER = "from pkg.util import foo\n\n\ndef run():\n    return foo(1)\n"
NOT_A_CALLER = "from pkg.util import foo\n\n\ndef run():\n    return 1\n"
UNRELATED = "from other import foo\n\nfoo(1)\n"


def test_changed_signatures() -> None:
    assert changed_signatures("pkg/util.py", OLD, NEW) == ["foo"]
    assert changed_signatures("pkg/util.py", OLD, OLD.replace("return 1", "return 2")) == []
    assert diff_signature_changes(unified_diff("pkg/util.py", OLD, NEW)) == ["foo"]
    js_old = "export function load(path) {\n}\n"
    js_new = "export function load(path, opts) {\n}\n"
    assert changed_signatures("web/api.ts", js_old, js_new) == ["load"]
    assert diff_signature_changes(unified_diff("web/api.ts", js_old, js_new)) == ["load"]


def test_local_change_warns_callers_only() -> None:
    deps = DepTracker()
    deps.record_local("pkg/util.py", OLD, NEW)
    note = deps.note_for("app/main.py", CALLER)
    assert note is not None
    assert "caller of `foo`" in note and "you changed just now" in note
    assert deps.note_for("app/main.py", NOT_A_CALLER) is None
    assert deps.note_for("app/main.py", UNRELATED) is None
    assert deps.note_for("pkg/util.py", CALLER) is None  # the defining file itself


def test_peer_diff_warns_with_their_name_and_age() -> None:
    deps = DepTracker()
    deps.record_peer_diff("sam", "pkg/util.py", unified_diff("pkg/util.py", OLD, NEW), at=1000.0)
    note = deps.note_for("app/main.py", CALLER, now=1000.0 + 5 * 60)
    assert note is not None and "sam changed 5 min ago" in note
    # Old news expires.
    assert deps.note_for("app/main.py", CALLER, now=1000.0 + 2 * 3600) is None


def test_js_importer() -> None:
    deps = DepTracker()
    deps.record_peer_diff(
        "kai", "web/api.ts",
        unified_diff("web/api.ts", "export function load(p) {}\n", "export function load(p, o) {}\n"),
    )
    caller = "import { load } from './api';\n\nload('x');\n"
    assert "caller of `load`" in (deps.note_for("web/page.ts", caller) or "")
