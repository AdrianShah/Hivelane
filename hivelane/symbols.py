"""Which definitions (functions, methods, classes) an edit touches.

Runs in the daemon, never on the hook hot path. Two tiers:

* Python is parsed with the stdlib `ast` module: exact qualified names
  (`Room.check_edit`), decorators included in a definition's extent.
* Everything else (JS/TS/TSX, Go, Rust, Java, C#, Ruby) uses tree-sitter when
  the optional extra is installed (`pip install hivelane[symbols]`, which pulls
  `tree-sitter-language-pack`), else a regex + brace/indent heuristic. The
  language pack fetches each grammar on first use, so grammars are loaded in a
  background thread and the heuristic answers until one is ready.

Special names: `<imports>` is the file's import block (the relay treats it as
intersecting everything), `<module>` is any other top-level code.

Every public function is pure and never raises; callers skip files larger than
MAX_SOURCE_BYTES, where the relay falls back to file-level overlap.
"""

from __future__ import annotations

import ast
import bisect
import difflib
import hashlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

IMPORTS = "<imports>"
MODULE = "<module>"

MAX_SOURCE_BYTES = 200_000
# ast.parse costs ~0.35 ms/KB; above this, Python gets the indentation
# heuristic so a cold outline stays inside the ~20 ms budget.
PY_AST_MAX_BYTES = 48_000
# A single range is scanned line by line; a whole-file rewrite yields every
# top-level name anyway, so very long ranges are sampled at definition starts.
MAX_SYMBOLS = 64
OUTLINE_CACHE_SIZE = 128

LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
}


def _source_bytes(source: str) -> int:
    """UTF-8 size of source (the limits are in bytes, not characters)."""
    if source.isascii():
        return len(source)
    return len(source.encode("utf-8", "replace"))


def source_lines(source: str) -> list[str]:
    """source split on "\\n" only (a trailing "\\r" dropped), the way ast,
    tree-sitter, predict_ranges and symbols_for_ranges count lines.
    str.splitlines() also breaks on \\x0c, \\x0b, \\x1c-\\x1e, \\x85, U+2028/9
    and a lone \\r, which shifted every later line number by one each."""
    lines = source.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


_GENERIC_ARGS = re.compile(r"<[^<>]*>|\[[^\[\]]*\]")


def canonical_name(name: str) -> str:
    """One spelling of a definition name for both tiers: generic arguments
    (`Foo<T>`, `Stack[T]`) removed and a path (`fmt::Display`, `a::Foo`) cut
    to its last segment, so tree-sitter's `impl<T> Foo<T>` and the
    heuristic's `Foo` are the same symbol on the relay."""
    previous = None
    while previous != name:
        previous = name
        name = _GENERIC_ARGS.sub("", name)
    name = name.strip()
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    return name.lstrip("&*").strip()


def language_for(path: str) -> Optional[str]:
    dot = path.rfind(".")
    if dot == -1:
        return None
    return LANG_BY_EXT.get(path[dot:].lower())


@dataclass(frozen=True)
class Definition:
    name: str  # qualified
    start: int  # 1-based inclusive
    end: int


@dataclass
class Outline:
    defs: list[Definition] = field(default_factory=list)
    imports: list[tuple[int, int]] = field(default_factory=list)

    def finalize(self) -> "Outline":
        # Sorted by start, outer before inner, so the last containing def wins.
        self.defs.sort(key=lambda d: (d.start, -d.end))
        self.imports = merge_ranges(self.imports, gap=1)
        self._starts = [d.start for d in self.defs]
        return self

    def symbol_at(self, line: int) -> str:
        for start, end in self.imports:
            if start <= line <= end:
                return IMPORTS
        best: Optional[Definition] = None
        upto = bisect.bisect_right(self._starts, line)
        for definition in self.defs[:upto]:
            if definition.end >= line:
                if best is None or definition.start >= best.start:
                    best = definition
        return best.name if best is not None else MODULE


def merge_ranges(ranges: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


# --- python (stdlib ast) -----------------------------------------------------------


_PY_COMPOUND: tuple[type, ...] = (ast.If, ast.Try, ast.With, ast.AsyncWith) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)


def _py_branches(node: ast.AST) -> list[list[ast.stmt]]:
    branches = [getattr(node, "body", []), getattr(node, "orelse", []), getattr(node, "finalbody", [])]
    branches.extend(handler.body for handler in getattr(node, "handlers", []))
    return [b for b in branches if b]


def _python_outline(source: str) -> Optional[Outline]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    outline = Outline()

    def visit(body: list[ast.stmt], prefix: str, top: bool) -> None:
        for node in body:
            if top and isinstance(node, (ast.Import, ast.ImportFrom)):
                outline.imports.append((node.lineno, node.end_lineno or node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{node.name}"
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                outline.defs.append(Definition(name, start, node.end_lineno or node.lineno))
                visit(node.body, name + ".", False)
            elif isinstance(node, _PY_COMPOUND):
                # `if TYPE_CHECKING:`, try-import blocks (the `except` branch
                # too), `with` blocks: imports inside still belong to the
                # import block, and defs inside are still defs.
                for branch in _py_branches(node):
                    visit(branch, prefix, top)

    visit(tree.body, "", True)
    return outline.finalize()


def _python_signatures(source: str) -> dict[str, str]:
    """Top-level functions and methods -> their parameter list text."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    out: dict[str, str] = {}
    for node in tree.body:
        bodies = [("", [node])]
        if isinstance(node, ast.ClassDef):
            bodies = [(node.name + ".", node.body)]
        for prefix, items in bodies:
            for item in items:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    try:
                        out[prefix + item.name] = ast.unparse(item.args)
                    except Exception:  # noqa: BLE001
                        continue
    return out


# --- regex / brace heuristic -------------------------------------------------------

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "else", "do", "try", "new",
    "function", "await", "typeof", "using", "lock", "foreach", "when", "match", "sizeof",
}

_DECLS: dict[str, list[re.Pattern[str]]] = {
    "js": [
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?P<kw>class|interface|enum|namespace)\s+(?P<name>[A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*[<(]"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"),
        # A method (class or interface member). Only trusted directly inside a
        # class or interface: in semicolon-free JS a bare call `doThing(x)`
        # has exactly this shape (see _heuristic_outline).
        re.compile(r"^\s*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set|declare)\s+)*\*?(?P<method>[A-Za-z_$][\w$]*)\s*\??\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^={;]+)?\{?\s*;?\s*$"),
    ],
    "go": [
        re.compile(r"^func\s+\((?:\w+\s+)?\*?\s*(?P<recv>\w+)[^)]*\)\s*(?P<name>\w+)\s*[(\[]"),
        re.compile(r"^func\s+(?P<name>\w+)\s*[(\[]"),
        re.compile(r"^type\s+(?P<name>\w+)(?:\s*\[[^\]]*\])?\s+(?:struct|interface)\b"),
    ],
    "rust": [
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?(?P<name>[\w:]+)"),
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|mod|union)\s+(?P<name>\w+)"),
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+|async\s+|unsafe\s+|extern\s+\"\w+\"\s+)*fn\s+(?P<name>\w+)"),
    ],
    "java": [
        re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|readonly|unsafe|new)\s+)*(?:class|interface|enum|record|struct)\s+(?P<name>\w+)"),
        re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async|sealed|synchronized|partial|extern|unsafe|new|default)\s+)*(?:<[^>]+>\s+)?[\w<>\[\],.?]+(?:\s*<[^>]*>)?\s+(?P<name>\w+)\s*\([^;]*$"),
        re.compile(r"^\s*(?:(?:public|private|protected|internal)\s+)(?P<name>[A-Z]\w*)\s*\([^;]*$"),
    ],
    "ruby": [
        re.compile(r"^\s*(?:class|module)\s+(?P<name>[A-Z][\w:]*)"),
        re.compile(r"^\s*def\s+(?:self\.)?(?P<name>[\w]+[?!=]?)"),
    ],
}
_FAMILY = {
    "javascript": "js", "typescript": "js", "tsx": "js",
    "go": "go", "rust": "rust", "java": "java", "csharp": "java", "ruby": "ruby",
}

_IMPORT_LINE = {
    "js": re.compile(r"^\s*(?:import\b|export\s+(?:\*|\{[^}]*\})\s+from\b|(?:const|let|var)\s+[\w${}\s,:]+=\s*require\()"),
    "go": re.compile(r"^\s*(?:import\b|package\b)"),
    "rust": re.compile(r"^\s*(?:pub\s+)?(?:use\b|extern\s+crate\b|mod\s+\w+\s*;)"),
    "java": re.compile(r"^\s*(?:import\b|package\b|using\s+[\w.]+\s*;|using\s+static\b|using\s+\w+\s*=)"),
    "ruby": re.compile(r"^\s*(?:require|require_relative|load)\b"),
}

# What may precede a `/` that starts a regex literal (rather than a division).
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")


class _BraceScanner:
    """Counts braces line by line, skipping strings, `//` and `/* */`
    comments (across lines), template literals (across lines) and, for JS,
    regex literals: `function a(){ /* } */ ... }` and `/\\}/g` used to end the
    block early."""

    __slots__ = ("in_block_comment", "in_template", "js")

    def __init__(self, js: bool) -> None:
        self.in_block_comment = False
        self.in_template = False
        self.js = js

    def braces(self, line: str) -> list[str]:
        out: list[str] = []
        i = 0
        n = len(line)
        last = ""  # last significant character, for regex detection
        while i < n:
            c = line[i]
            if self.in_block_comment:
                end = line.find("*/", i)
                if end == -1:
                    return out
                self.in_block_comment = False
                i = end + 2
                continue
            if self.in_template:
                if c == "\\":
                    i += 2
                    continue
                if c == "`":
                    self.in_template = False
                    last = "`"
                i += 1
                continue
            if c == "/" and i + 1 < n and line[i + 1] == "/":
                return out
            if c == "/" and i + 1 < n and line[i + 1] == "*":
                self.in_block_comment = True
                i += 2
                continue
            if c == "`" and self.js:
                self.in_template = True
                i += 1
                continue
            if c in "\"'":
                j = i + 1
                while j < n and line[j] != c:
                    j += 2 if line[j] == "\\" else 1
                if j >= n:
                    # Unterminated on this line (a Rust lifetime `'a`): only
                    # the quote itself is skipped.
                    i += 1
                    continue
                i = j + 1
                last = c
                continue
            if c == "/" and self.js and (last == "" or last in _REGEX_PRECEDERS):
                j = i + 1
                in_class = False
                while j < n:
                    ch = line[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        break
                    j += 1
                if j < n:
                    i = j + 1
                    last = "/"
                    continue
            if c in "{}":
                out.append(c)
            if not c.isspace():
                last = c
            i += 1
        return out


def _brace_end(lines: list[str], start: int, js: bool = False) -> int:
    """0-based last line of a block opening at or just after `start`; `start`
    itself when no block opens (a declaration without a body never runs to
    the end of the file)."""
    depth = 0
    opened = False
    scanner = _BraceScanner(js)
    for index in range(start, len(lines)):
        for char in scanner.braces(lines[index]):
            if char == "{":
                depth += 1
                opened = True
            else:
                depth -= 1
                if opened and depth <= 0:
                    return index
                if not opened:
                    return start  # the enclosing block closed first
        if not opened:
            if index - start >= 3 or lines[index].rstrip().endswith(";"):
                return start
    return len(lines) - 1 if opened else start


_RUBY_ONE_LINE_DEF = re.compile(
    # `def a; 1; end`, and the endless `def b = 2`, `def c(x) = x * 2` (not
    # the setter `def x=(v)`).
    r"^\s*def\s+(?:self\.)?\w+[?!]?(?:\s*\([^)]*\)\s*|\s+)=(?![=~>])|^\s*def\b[^#]*;[^#]*\bend\s*(?:#.*)?$"
)


def _ruby_end(lines: list[str], start: int) -> int:
    if _RUBY_ONE_LINE_DEF.match(lines[start]):
        return start
    indent = len(lines[start]) - len(lines[start].lstrip())
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "end" or stripped.startswith("end ") or stripped.startswith("end#"):
            if len(lines[index]) - len(lines[index].lstrip()) <= indent:
                return index
    return start


def _heuristic_outline(source: str, lang: str) -> Outline:
    family = _FAMILY.get(lang, "js")
    lines = source_lines(source)
    outline = Outline()
    # start0, end0, name, recv, kind ("class"/"interface"/... or "method"), braced
    raw: list[tuple[int, int, str, str, str, bool]] = []
    import_re = _IMPORT_LINE[family]
    in_go_import = False
    in_go_types = False
    for index, line in enumerate(lines):
        if family == "go":
            if in_go_import:
                outline.imports.append((index + 1, index + 1))
                if line.strip().startswith(")"):
                    in_go_import = False
                continue
            if re.match(r"^import\s*\($", line.strip()):
                in_go_import = True
                outline.imports.append((index + 1, index + 1))
                continue
            # A grouped `type ( A struct {...}; B interface {...} )`.
            if in_go_types:
                if line.startswith(")"):
                    in_go_types = False
                    continue
                match = _GO_GROUPED_TYPE.match(line)
                if match:
                    end = _brace_end(lines, index)
                    raw.append((index, end, canonical_name(match.group("name")), "", "type", True))
                continue
            if re.match(r"^type\s*\(\s*$", line):
                in_go_types = True
                continue
        if import_re.match(line):
            end = index
            if family == "js" and "{" in line and "}" not in line:
                while end + 1 < len(lines) and "}" not in lines[end]:
                    end += 1
            outline.imports.append((index + 1, end + 1))
            continue
        for pattern in _DECLS[family]:
            match = pattern.match(line)
            if not match:
                continue
            groups = match.groupdict()
            is_method = groups.get("method") is not None
            name = groups["method"] if is_method else groups["name"]
            # Statement keywords only look like declarations in C-like
            # languages (Rust's `fn new` is a real definition).
            if family in ("js", "java") and name in _KEYWORDS:
                break
            if family == "ruby":
                end = _ruby_end(lines, index)
            else:
                end = _brace_end(lines, index, js=family == "js")
            braced = "{" in line or (
                index + 1 < len(lines) and lines[index + 1].lstrip().startswith("{")
            )
            kind = "method" if is_method else (groups.get("kw") or "def")
            if is_method and "abstract" in line.split("(", 1)[0]:
                kind = "abstract-method"
            recv = canonical_name(groups.get("recv") or "")
            raw.append((index, end, canonical_name(name), recv, kind, braced))
            break
    # Qualify by containment.
    raw.sort(key=lambda r: (r[0], -r[1]))
    stack: list[tuple[int, str, str]] = []  # (end0, qualified, kind)
    for start, end, name, recv, kind, braced in raw:
        while stack and stack[-1][0] < start:
            stack.pop()
        if kind in ("method", "abstract-method"):
            # Methods live directly in a class or an interface; anything else
            # with this shape is a call statement. Interface members (and
            # abstract methods) have no body; a class method has one.
            parent = stack[-1][2] if stack else ""
            if parent == "interface" or (parent == "class" and (braced or kind == "abstract-method")):
                end = end if braced else start
            else:
                continue
        if recv:
            qualified = f"{recv}.{name}"
        elif stack:
            qualified = f"{stack[-1][1]}.{name}"
        else:
            qualified = name
        outline.defs.append(Definition(qualified, start + 1, end + 1))
        stack.append((end, qualified, kind))
    return outline.finalize()


_GO_GROUPED_TYPE = re.compile(r"^\s+(?P<name>\w+)(?:\s*\[[^\]]*\])?\s+(?:struct|interface)\b")


# --- tree-sitter (optional) ----------------------------------------------------------

_TS_DEF_TYPES = {
    "function_declaration", "generator_function_declaration", "method_definition",
    "class_declaration", "abstract_class_declaration", "interface_declaration",
    "enum_declaration", "method_declaration", "constructor_declaration",
    "record_declaration", "struct_declaration", "function_item", "struct_item",
    "enum_item", "trait_item", "mod_item", "impl_item", "type_spec", "method",
    "singleton_method", "class", "module", "internal_module",
    "method_signature", "abstract_method_signature",
}
# Definitions whose bodies are not searched for further definitions: walking
# every expression node in Python is what makes a large file slow.
_TS_LEAF_DEFS = {
    "function_declaration", "generator_function_declaration", "method_definition",
    "method_declaration", "constructor_declaration", "function_item", "method",
    "singleton_method", "method_signature", "abstract_method_signature",
}
_TS_IMPORT_TYPES = {
    "import_statement", "import_declaration", "use_declaration", "using_directive",
    "package_clause", "package_declaration", "extern_crate_declaration",
}

_ts_lock = threading.Lock()
_ts_parsers: dict[str, Any] = {}
_ts_loading: set[str] = set()
_ts_available: Optional[bool] = None


def _ts_parser(lang: str) -> Any:
    """A ready tree-sitter parser for lang, or None (starting a background
    load the first time: the language pack may download the grammar)."""
    global _ts_available
    if _ts_available is False:
        return None
    parser = _ts_parsers.get(lang)
    if parser is not None:
        return parser
    with _ts_lock:
        if _ts_available is None:
            try:
                import tree_sitter_language_pack  # noqa: F401
                _ts_available = True
            except Exception:  # noqa: BLE001 - optional extra
                _ts_available = False
                return None
        if lang in _ts_loading:
            return None
        _ts_loading.add(lang)
    threading.Thread(target=_load_ts, args=(lang,), daemon=True, name=f"ts:{lang}").start()
    return None


def _load_ts(lang: str) -> None:
    try:
        from tree_sitter_language_pack import get_parser

        _ts_parsers[lang] = get_parser(lang)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - stay on the heuristic for this language
        pass


# Non-definition nodes that can hold definitions (class/impl/namespace bodies).
_TS_CONTAINERS = {
    "class_body", "declaration_list", "field_declaration_list", "interface_body",
    "enum_body", "body_statement", "type_declaration", "namespace_declaration",
    "file_scoped_namespace_declaration", "statement_block", "module_body", "export_statement",
}
_TS_SKIP = {"comment", "string", "expression_statement"}


def _ts_is_require(node: Any, source: bytes) -> bool:
    """`const x = require('y')` (JS) or `require 'y'` (Ruby) at top level."""
    if node.type not in ("lexical_declaration", "variable_declaration", "call", "expression_statement"):
        return False
    head = source[node.start_byte : min(node.end_byte, node.start_byte + 200)]
    if node.type == "call" or node.type == "expression_statement":
        return head.startswith((b"require", b"require_relative", b"load "))
    return b"require(" in head and b"=>" not in head and b"function" not in head


def _ts_name(node: Any, source: bytes) -> Optional[str]:
    target = node.child_by_field_name("name")
    if node.type == "impl_item":
        target = node.child_by_field_name("type")
    if target is None:
        return None
    text = source[target.start_byte : target.end_byte].decode("utf-8", "replace")
    return canonical_name(text) or None


def _ts_outline(parser: Any, source: str) -> Optional[Outline]:
    data = source.encode("utf-8")
    try:
        root = parser.parse(data).root_node
    except Exception:  # noqa: BLE001
        return None
    outline = Outline()

    def visit(node: Any, prefix: str, depth: int) -> None:
        for child in node.named_children:
            kind = child.type
            start, end = child.start_point[0] + 1, child.end_point[0] + 1
            if depth == 0 and (kind in _TS_IMPORT_TYPES or _ts_is_require(child, data)):
                outline.imports.append((start, end))
                continue
            if kind == "export_statement":
                visit(child, prefix, depth)
                continue
            name = None
            if kind in _TS_DEF_TYPES:
                name = _ts_name(child, data)
                if kind == "method_declaration" and child.child_by_field_name("receiver") is not None:
                    receiver = child.child_by_field_name("receiver")
                    text = data[receiver.start_byte : receiver.end_byte].decode("utf-8", "replace")
                    match = re.search(r"(\w+)(?:\[[^\]]*\])?\s*\)\s*$", text)
                    if match and name:
                        name = f"{match.group(1)}.{name}"
                        outline.defs.append(Definition(name, start, end))
                        continue
            elif kind in ("lexical_declaration", "variable_declaration"):
                for declarator in child.named_children:
                    value = declarator.child_by_field_name("value")
                    if value is not None and value.type in ("arrow_function", "function", "function_expression"):
                        name = _ts_name(declarator, data)
                        break
            if name:
                qualified = f"{prefix}{name}"
                outline.defs.append(Definition(qualified, start, end))
                if kind not in _TS_LEAF_DEFS and kind not in ("lexical_declaration", "variable_declaration"):
                    visit(child, qualified + ".", depth + 1)
            elif kind in _TS_CONTAINERS or (depth == 0 and kind not in _TS_SKIP):
                visit(child, prefix, depth + 1)

    try:
        visit(root, "", 0)
    except RecursionError:
        return None
    return outline.finalize()


# --- outlines, cached -----------------------------------------------------------------

_cache: OrderedDict[tuple[str, str], Outline] = OrderedDict()
_cache_lock = threading.Lock()


def outline_for(path: str, source: str) -> Optional[Outline]:
    """The file's definitions and import block, or None for an unknown
    language / oversized file."""
    lang = language_for(path)
    if lang is None or _source_bytes(source) > MAX_SOURCE_BYTES:
        return None
    # A UTF-8 BOM (common from Windows editors) made ast.parse fail, so every
    # BOM'd Python file got the heuristic. It is not on a line of its own, so
    # dropping it shifts no line number.
    source = source.removeprefix("﻿")
    key = (path, hashlib.sha1(source.encode("utf-8", "replace")).hexdigest())
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    outline: Optional[Outline] = None
    try:
        if lang == "python":
            if _source_bytes(source) <= PY_AST_MAX_BYTES:
                outline = _python_outline(source)
            if outline is None:
                outline = _heuristic_python(source)
        else:
            parser = _ts_parser(lang)
            if parser is not None:
                outline = _ts_outline(parser, source)
            if outline is None:
                outline = _heuristic_outline(source, lang)
    except Exception:  # noqa: BLE001 - best effort, never fatal
        return None
    with _cache_lock:
        _cache[key] = outline
        while len(_cache) > OUTLINE_CACHE_SIZE:
            _cache.popitem(last=False)
    return outline


_PY_DEF = re.compile(r"^(?P<indent>\s*)(?:async\s+)?(?:def|class)\s+(?P<name>\w+)")
_PY_IMPORT = re.compile(r"^﻿?(?:import|from\s+\S+\s+import)\b")


def _heuristic_python(source: str) -> Outline:
    """Indentation-based outline for Python that does not parse (mid-edit):
    decorators belong to the definition below them, and lines inside a
    triple-quoted string are neither definitions nor dedents."""
    lines = source_lines(source)
    outline = Outline()
    stack: list[tuple[int, str, int]] = []  # indent, qualified, start0

    def close(upto_indent: int, last: int) -> None:
        while stack and stack[-1][0] >= upto_indent:
            indent, name, start = stack.pop()
            outline.defs.append(Definition(name, start + 1, last + 1))

    last_code = 0
    in_string: Optional[str] = None  # the open triple quote
    decorator_start: Optional[int] = None
    for index, line in enumerate(lines):
        if in_string is not None:
            last_code = index
            if line.count(in_string) % 2 == 1:
                in_string = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        close(indent, last_code)
        if indent == 0 and _PY_IMPORT.match(stripped):
            outline.imports.append((index + 1, index + 1))
        match = _PY_DEF.match(line)
        if stripped.startswith("@"):
            if decorator_start is None:
                decorator_start = index
        elif match:
            prefix = stack[-1][1] + "." if stack else ""
            start = decorator_start if decorator_start is not None else index
            stack.append((indent, prefix + match.group("name"), start))
            decorator_start = None
        else:
            decorator_start = None
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                in_string = quote
                break
        last_code = index
    close(-1, last_code)
    return outline.finalize()


# --- public API ----------------------------------------------------------------------


def symbols_for_ranges(path: str, source: str, line_ranges: list[tuple[int, int]]) -> list[str]:
    """Qualified names of the innermost definitions enclosing each range
    (1-based inclusive lines in `source`). [] when unknown language/too big."""
    if not line_ranges:
        return []
    outline = outline_for(path, source)
    if outline is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    total = max(1, source.count("\n") + (0 if source.endswith("\n") else 1))
    for start, end in line_ranges:
        start = max(1, min(start, total))
        end = max(start, min(end, total))
        lines: Any = range(start, end + 1)
        if end - start > 400:
            # Sample: the endpoints plus every definition start inside.
            lines = sorted({start, end, *(d.start for d in outline.defs if start <= d.start <= end)})
        for line in lines:
            name = outline.symbol_at(line)
            if name not in seen:
                seen.add(name)
                names.append(name)
                if len(names) >= MAX_SYMBOLS:
                    return names
    return names


def changed_line_ranges(old: str, new: str) -> list[tuple[int, int]]:
    """Ranges of lines (1-based inclusive, in `new`) that differ from `old`.
    A pure deletion maps to the line just before it (or line 1)."""
    if old == new:
        return []
    # Split on "\n" only, like every other line count here (see source_lines).
    a, b = source_lines(old), source_lines(new)
    # The unchanged head and tail are cut off before diffing: SequenceMatcher
    # is quadratic on files full of repeated lines, a typical edit is local.
    head = 0
    limit = min(len(a), len(b))
    while head < limit and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < limit - head and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]:
        tail += 1
    mid_a, mid_b = a[head : len(a) - tail], b[head : len(b) - tail]
    if not mid_a and not mid_b:
        # Only line endings / a trailing newline differ.
        line = max(1, min(len(b), head + 1))
        return [(line, line)]
    matcher = difflib.SequenceMatcher(
        None, mid_a, mid_b, autojunk=len(mid_a) + len(mid_b) > 4000
    )
    ranges: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        j1 += head
        j2 += head
        if tag == "delete" or j2 == j1:
            line = max(1, min(j1, len(b)) if b else 1)
            ranges.append((line, line))
        else:
            ranges.append((j1 + 1, j2))
    return merge_ranges(ranges)


def predict_ranges(old_source: str, old_string: str, limit: int = 50) -> list[tuple[int, int]]:
    """Where an Edit's `old_string` sits in the current file (every
    occurrence, for replace_all). [] if absent or empty."""
    if not old_string or not old_source:
        return []
    span = old_string.rstrip("\n").count("\n")
    ranges: list[tuple[int, int]] = []
    index = old_source.find(old_string)
    line = 1
    position = 0
    while index != -1 and len(ranges) < limit:
        line += old_source.count("\n", position, index)
        position = index
        ranges.append((line, line + span))
        index = old_source.find(old_string, index + max(1, len(old_string)))
    return merge_ranges(ranges)


def signatures(path: str, source: str) -> dict[str, str]:
    """Top-level function/method name -> parameter text (Python exact; other
    languages via a declaration-line regex). Used by hivelane.depgraph."""
    lang = language_for(path)
    if lang is None or _source_bytes(source) > MAX_SOURCE_BYTES:
        return {}
    source = source.removeprefix("﻿")
    key = ("sig:" + path, hashlib.sha1(source.encode("utf-8", "replace")).hexdigest())
    with _cache_lock:
        cached = _sig_cache.get(key)
    if cached is not None:
        return dict(cached)
    found = _python_signatures(source) if lang == "python" else _js_sig_iter(source)
    with _cache_lock:
        _sig_cache[key] = found
        while len(_sig_cache) > OUTLINE_CACHE_SIZE:
            _sig_cache.popitem(last=False)
    return dict(found)


_sig_cache: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()


_JS_SIG = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\((?P<params>[^)]*)\)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name2>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\((?P<params2>[^)]*)\)\s*=>",
    re.M,
)


def _js_sig_iter(source: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _JS_SIG.finditer(source):
        name = match.group("name") or match.group("name2")
        params = match.group("params") if match.group("name") else match.group("params2")
        out[name] = " ".join((params or "").split())
    return out
