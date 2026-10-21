"""hivelane.symbols: exact Python outlines, heuristic outlines for the other
languages, import blocks, changed/predicted line ranges and the size cap."""

from __future__ import annotations

import importlib.util
import time

import pytest

from hivelane import symbols
from hivelane.symbols import IMPORTS, MODULE, changed_line_ranges, predict_ranges, symbols_for_ranges


@pytest.fixture(autouse=True)
def heuristic_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the dependency-free tier so results do not depend on whether the
    optional tree-sitter extra happens to be installed."""
    monkeypatch.setattr(symbols, "_ts_available", False)
    symbols._cache.clear()


PY = '''\
import os
from typing import (
    Any,
)

CONSTANT = 1


class Room:
    """A room."""

    @property
    def code(self) -> str:
        return "x"

    def check_edit(self, path: str) -> bool:
        def inner() -> None:
            pass
        return True


async def main(argv: list[str]) -> int:
    return 0
'''


def lines_of(path: str, source: str) -> list[str]:
    total = len(source.splitlines())
    return [symbols_for_ranges(path, source, [(n, n)])[0] for n in range(1, total + 1)]


def test_python_is_exact() -> None:
    per_line = lines_of("server/room.py", PY)
    assert per_line[0:4] == [IMPORTS] * 4  # multi-line import block included
    assert per_line[5] == MODULE  # CONSTANT
    assert per_line[8] == "Room"  # class line
    assert per_line[11] == "Room.code"  # the decorator belongs to the method
    assert per_line[12] == "Room.code"
    assert per_line[15] == "Room.check_edit"
    assert per_line[16] == "Room.check_edit.inner"
    assert per_line[18] == "Room.check_edit"
    assert per_line[21] == "main"


def test_ranges_return_each_symbol_once_in_order() -> None:
    assert symbols_for_ranges("a.py", PY, [(15, 19), (1, 1)]) == [
        "Room",
        "Room.check_edit",
        "Room.check_edit.inner",
        IMPORTS,
    ]
    assert symbols_for_ranges("a.py", PY, []) == []
    # Out-of-range lines clamp instead of failing.
    assert symbols_for_ranges("a.py", PY, [(500, 900)]) == ["main"]


def test_unparseable_python_falls_back_to_indentation() -> None:
    broken = "import os\n\nclass A:\n    def m(self):\n        x = (\n\ndef f():\n    return 1\n"
    assert lines_of("a.py", broken)[3] == "A.m"
    assert symbols_for_ranges("a.py", broken, [(8, 8)]) == ["f"]
    assert symbols_for_ranges("a.py", broken, [(1, 1)]) == [IMPORTS]


def test_unknown_language_and_oversized_files_yield_nothing() -> None:
    assert symbols_for_ranges("notes.txt", "hello\n", [(1, 1)]) == []
    big = "x = 1\n" * (symbols.MAX_SOURCE_BYTES // 6 + 10)
    assert symbols_for_ranges("big.py", big, [(1, 1)]) == []


HEURISTIC_CASES = {
    "web/room.ts": (
        "import { x } from './x';\nimport y from 'y';\n\nexport class Room {\n  checkEdit(a: number) {\n"
        "    return a;\n  }\n}\n\nexport function helper(b) {\n  return b;\n}\nconst arrow = (c) => {\n"
        "  return c;\n};\n",
        {1: IMPORTS, 2: IMPORTS, 3: MODULE, 4: "Room", 6: "Room.checkEdit", 11: "helper", 14: "arrow"},
    ),
    "web/App.tsx": (
        "import React from 'react';\n\nexport function App() {\n  return <div/>;\n}\n",
        {1: IMPORTS, 4: "App"},
    ),
    "lib/a.js": (
        "const fs = require('fs');\n\nclass A {\n  m() {\n    return 1;\n  }\n}\nfunction f() {\n  return 2;\n}\n",
        {1: IMPORTS, 5: "A.m", 9: "f"},
    ),
    "cmd/main.go": (
        "package main\n\nimport (\n\t\"fmt\"\n)\n\ntype Room struct {\n\tx int\n}\n\nfunc (r *Room) Check(a int) int {\n"
        "\treturn a\n}\n\nfunc main() {\n\tfmt.Println(1)\n}\n",
        {4: IMPORTS, 8: "Room", 12: "Room.Check", 16: "main"},
    ),
    "src/lib.rs": (
        "use std::io;\n\nstruct Room { x: i32 }\n\nimpl Room {\n    fn check(&self) -> i32 {\n        self.x\n"
        "    }\n}\n\nfn main() {\n    println!(\"hi\");\n}\n",
        {1: IMPORTS, 7: "Room.check", 12: "main"},
    ),
    "src/Room.java": (
        "package a;\nimport java.util.List;\n\npublic class Room {\n    public int check(int a) {\n"
        "        return a;\n    }\n}\n",
        {2: IMPORTS, 6: "Room.check", 8: "Room"},
    ),
    "src/Room.cs": (
        "using System;\n\nnamespace N {\n    public class Room {\n        public int Check(int a) {\n"
        "            return a;\n        }\n    }\n}\n",
        {1: IMPORTS, 6: "Room.Check"},
    ),
    "lib/room.rb": (
        "require 'json'\n\nclass Room\n  def check(a)\n    a\n  end\nend\n",
        {1: IMPORTS, 5: "Room.check", 7: "Room"},
    ),
}


@pytest.mark.parametrize("path", sorted(HEURISTIC_CASES))
def test_heuristic_outlines(path: str) -> None:
    source, expected = HEURISTIC_CASES[path]
    for line, name in expected.items():
        assert symbols_for_ranges(path, source, [(line, line)]) == [name], (path, line)


@pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_language_pack") is None,
    reason=(
        "needs the optional `symbols` extra (pip install -e .[symbols]); the dev "
        "install and CI do not include it, so this only runs where it is installed"
    ),
)
def test_tree_sitter_matches_heuristic_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(symbols, "_ts_available", None)
    for path, (source, expected) in HEURISTIC_CASES.items():
        lang = symbols.language_for(path)
        assert lang is not None
        symbols._ts_parser(lang)
        deadline = time.monotonic() + 30
        while symbols._ts_parsers.get(lang) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if symbols._ts_parsers.get(lang) is None:
            pytest.skip(f"no tree-sitter grammar for {lang} (offline?)")
        symbols._cache.clear()
        for line, name in expected.items():
            assert symbols_for_ranges(path, source, [(line, line)]) == [name], (path, line)


# --- line ranges -------------------------------------------------------------


def test_changed_line_ranges() -> None:
    old = "a\nb\nc\nd\n"
    assert changed_line_ranges(old, old) == []
    assert changed_line_ranges(old, "a\nB\nc\nd\n") == [(2, 2)]
    assert changed_line_ranges(old, "a\nb\nx\ny\nc\nd\n") == [(3, 4)]  # insertion, new-file lines
    assert changed_line_ranges(old, "a\nd\n") == [(1, 1)]  # pure deletion -> adjacent line
    assert changed_line_ranges(old, "b\nc\nd\n") == [(1, 1)]  # deleting line 1
    assert changed_line_ranges(old, "A\nb\nc\nD\n") == [(1, 1), (4, 4)]
    assert changed_line_ranges("", "x\ny\n") == [(1, 2)]


def test_changed_line_ranges_is_fast_on_repetitive_files() -> None:
    old = "".join(f"def f{i}(a):\n    x = a + 1\n    return x\n\n" for i in range(4000))
    new = old.replace("x = a + 1", "x = a + 2", 1)
    started = time.perf_counter()
    assert changed_line_ranges(old, new) == [(2, 2)]
    assert time.perf_counter() - started < 0.5


def test_predict_ranges() -> None:
    source = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    assert predict_ranges(source, "def b():\n    return 1") == [(4, 5)]
    assert predict_ranges(source, "return 1") == [(2, 2), (5, 5)]  # every occurrence
    assert predict_ranges(source, "absent") == []
    assert predict_ranges(source, "") == []
    assert symbols_for_ranges("m.py", source, predict_ranges(source, "def b():")) == ["b"]


def test_outlines_are_cached_by_content() -> None:
    symbols_for_ranges("c.py", PY, [(1, 1)])
    first = symbols.outline_for("c.py", PY)
    assert symbols.outline_for("c.py", PY) is first
    assert symbols.outline_for("c.py", PY + "\n") is not first
    for i in range(symbols.OUTLINE_CACHE_SIZE + 5):
        symbols.outline_for("c.py", f"x = {i}\n")
    assert len(symbols._cache) <= symbols.OUTLINE_CACHE_SIZE


def test_latency_budget_for_a_typical_file() -> None:
    source = "import os\n\n" + "".join(
        f"class C{i}:\n    def m(self, a):\n        x = a + 1\n        return x\n\n" for i in range(600)
    )
    assert len(source) < symbols.PY_AST_MAX_BYTES
    started = time.perf_counter()
    symbols_for_ranges("big.py", source, [(300, 305)])
    cold = time.perf_counter() - started
    # Generous for CI noise; measured ~10 ms.
    assert cold < 0.2


def test_signatures() -> None:
    py = "def foo(a, b=1):\n    pass\n\nclass K:\n    def m(self, x):\n        pass\n"
    assert symbols.signatures("a.py", py) == {"foo": "a, b=1", "K.m": "self, x"}
    js = "export function foo(a, b) {}\nconst bar = async (x) => x;\n"
    assert symbols.signatures("a.ts", js) == {"foo": "a, b", "bar": "x"}
    assert symbols.signatures("a.txt", "x") == {}


# --- review fixes: one name format for both tiers, extents, line counting ------

from hivelane.tests.symbol_cases import CONFORMANCE, LANGS, defs_of  # noqa: E402


@pytest.mark.parametrize("path", sorted(CONFORMANCE))
def test_heuristic_conformance(path: str) -> None:
    """Findings 21-25: bare JS calls, Rust generics, Ruby one-line/endless
    defs, Go receivers/generics/grouped types, TS interface members and
    generic arrows, block comments and regex literals."""
    source, expected = CONFORMANCE[path]
    assert defs_of(symbols._heuristic_outline(source, LANGS[path[path.rfind("."):]])) == expected


@pytest.mark.parametrize("path", sorted(CONFORMANCE))
def test_tree_sitter_conformance_when_installed(path: str) -> None:
    pytest.importorskip("tree_sitter_language_pack")
    from tree_sitter_language_pack import get_parser

    source, expected = CONFORMANCE[path]
    try:
        parser = get_parser(LANGS[path[path.rfind("."):]])
    except Exception:  # noqa: BLE001 - grammar download unavailable
        pytest.skip("no grammar (offline?)")
    assert defs_of(symbols._ts_outline(parser, source)) == expected


def test_canonical_name() -> None:
    assert symbols.canonical_name("Foo<T>") == "Foo"
    assert symbols.canonical_name("Map<K, Vec<V>>") == "Map"
    assert symbols.canonical_name("Stack[T]") == "Stack"
    assert symbols.canonical_name("fmt::Display") == "Display"
    assert symbols.canonical_name("check?") == "check?"


def test_bare_call_in_semicolon_free_js_is_not_a_definition() -> None:
    source = "function outer() {\n  const x = 1\n  doThing(x)\n  more(x)\n  return x\n}\n"
    assert {symbols_for_ranges("a.js", source, [(n, n)])[0] for n in range(2, 6)} == {"outer"}


def test_lines_are_counted_on_newlines_only() -> None:
    """Finding 26: a form feed or U+2028 split lines in changed_line_ranges
    but not in ast/predict_ranges, shifting post_edit by one per character."""
    old = "def a():\n    return 1\n\x0c\ndef b():\n    s = 'x y'\n    return 2\n"
    new = old.replace("return 2", "return 3")
    assert changed_line_ranges(old, new) == [(6, 6)]
    assert predict_ranges(old, "return 2") == [(6, 6)]
    assert symbols_for_ranges("m.py", new, [(6, 6)]) == ["b"]
    assert symbols.source_lines("a\r\nb\n") == ["a", "b"]


def test_bom_python_uses_ast() -> None:
    """Finding 27: a UTF-8 BOM made ast.parse fail."""
    source = "﻿import os\n\n@dec\ndef f():\n    s = '''\ndef fake():\n'''\n    return 1\n"
    assert symbols_for_ranges("m.py", source, [(1, 1)]) == [IMPORTS]
    assert symbols_for_ranges("m.py", source, [(3, 3)]) == ["f"]
    assert symbols_for_ranges("m.py", source, [(6, 8)]) == ["f"]


def test_heuristic_python_keeps_decorators_and_skips_strings() -> None:
    broken = "@dec\ndef f():\n    s = '''\ndef fake():\n'''\n    x = (\n"
    outline = symbols._heuristic_python(broken)
    assert [(d.name, d.start) for d in outline.defs] == [("f", 1)]


def test_ast_sees_defs_and_imports_inside_compound_statements() -> None:
    source = (
        "try:\n    import fast\nexcept ImportError:\n    import slow as fast\n\n"
        "if True:\n    def g():\n        return 1\n"
    )
    assert symbols_for_ranges("m.py", source, [(4, 4)]) == [IMPORTS]
    assert symbols_for_ranges("m.py", source, [(8, 8)]) == ["g"]


def test_size_limit_is_in_bytes() -> None:
    big = "# " + "é" * (symbols.MAX_SOURCE_BYTES // 2 + 10) + "\nx = 1\n"
    assert len(big) < symbols.MAX_SOURCE_BYTES
    assert symbols_for_ranges("big.py", big, [(1, 1)]) == []
