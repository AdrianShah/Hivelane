"""Shared fixtures for the two symbol tiers: tree-sitter and the regex/brace
heuristic must give the same qualified names and extents for these, so
teammates with and without `hivelane[symbols]` report intersecting symbols.

Plain data (no pytest import), so the conformance check can also run in a
scratch venv that has tree-sitter-language-pack but not the test deps:

    python -c "from hivelane.tests.symbol_cases import check_tree_sitter; check_tree_sitter()"
"""

from __future__ import annotations

# path -> (source, [(qualified name, start line, end line), ...] in order)
CONFORMANCE: dict[str, tuple[str, list[tuple[str, int, int]]]] = {
    "generic.rs": (
        "struct Foo<T>(T);\nimpl<T> Foo<T> {\n    fn new(t: T) -> Self {\n        Foo(t)\n    }\n}\n"
        "impl<T: Clone> From<T> for Foo<T> {\n    fn from(t: T) -> Self { Foo(t) }\n}\n",
        [("Foo", 1, 1), ("Foo", 2, 6), ("Foo.new", 3, 5), ("Foo", 7, 9), ("Foo.from", 8, 8)],
    ),
    "nosemi.js": (
        "function outer() {\n  const x = 1\n  doThing(x)\n  return x\n}\n"
        "describe('suite', function () {\n  it('works', () => {\n    run(1)\n  })\n})\n",
        [("outer", 1, 5)],
    ),
    "oneline.rb": (
        "class A\n  def a; 1; end\n  def b = 2\n  def c\n    3\n  end\n  def d(x) = x * 2\n  def e\n  end\nend\n",
        [("A", 1, 10), ("A.a", 2, 2), ("A.b", 3, 3), ("A.c", 4, 6), ("A.d", 7, 7), ("A.e", 8, 9)],
    ),
    "recv.go": (
        "package x\n\nfunc (Server) Handle(w int) {\n}\n\ntype Stack[T any] struct {\n}\n\n"
        "func (s *Stack[T]) Push(v T) {\n}\n",
        [("Server.Handle", 3, 4), ("Stack", 6, 7), ("Stack.Push", 9, 10)],
    ),
    "grouped.go": (
        "package x\n\ntype (\n\tA struct {\n\t\tx int\n\t}\n\tB interface {\n\t\tM()\n\t}\n)\n",
        [("A", 4, 6), ("B", 7, 9)],
    ),
    "members.ts": (
        "export const id = <T,>(x: T): T => x;\nexport class K {\n  @dec()\n  method(a: number) {\n"
        "    return a\n  }\n}\ninterface I {\n  m(): void\n  n(x: number): string;\n}\n",
        [("id", 1, 1), ("K", 2, 7), ("K.method", 4, 6), ("I", 8, 11), ("I.m", 9, 9), ("I.n", 10, 10)],
    ),
    "comments.js": (
        "function a() {\n  /* } */\n  const re = /\\}/g\n  /*\n   } still a comment\n  */\n  return 1\n}\n"
        "function b() {\n  return `}\n  ${x}`\n}\n",
        [("a", 1, 8), ("b", 9, 12)],
    ),
    "Outer.java": (
        "public class Outer {\n  public static class Inner {\n    void run() {\n    }\n  }\n  public Outer() {\n  }\n}\n",
        [("Outer", 1, 8), ("Outer.Inner", 2, 5), ("Outer.Inner.run", 3, 4), ("Outer.Outer", 6, 7)],
    ),
    "N.cs": (
        "namespace N {\n  public class C {\n    public void M() {\n    }\n  }\n}\n",
        [("C", 2, 5), ("C.M", 3, 4)],
    ),
}

LANGS = {".rs": "rust", ".js": "javascript", ".rb": "ruby", ".go": "go", ".ts": "typescript",
         ".java": "java", ".cs": "csharp"}


def defs_of(outline) -> list[tuple[str, int, int]]:  # type: ignore[no-untyped-def]
    return [(d.name, d.start, d.end) for d in outline.defs]


def check_tree_sitter() -> None:
    """Assert tree-sitter's outline equals CONFORMANCE for every case."""
    from tree_sitter_language_pack import get_parser

    from hivelane import symbols

    failures = []
    for path, (source, expected) in CONFORMANCE.items():
        lang = LANGS[path[path.rfind("."):]]
        got = defs_of(symbols._ts_outline(get_parser(lang), source))
        if got != expected:
            failures.append((path, got, expected))
    assert not failures, failures
    print(f"tree-sitter conformance: {len(CONFORMANCE)} cases OK")
