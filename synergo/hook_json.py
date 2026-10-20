"""JSON for the hook hot path, built on the json package's C core.

`import json` pulls in re, enum, functools and collections; on a cold Windows
start that is ~30 ms of the hook's budget, paid around every edit. The C
accelerator `_json` underneath it imports in well under a millisecond and is
what json.loads/json.dumps call into anyway, so this module drives it
directly. Falls back to the pure-Python parts of the json package wherever
`_json` is missing (non-CPython).

Output matches json.dumps with default arguments character for character
(synergo/tests/test_hook_fast.py checks), so callers can switch freely.
"""

from __future__ import annotations

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

try:
    from _json import encode_basestring_ascii as _encode_str
    from _json import make_scanner as _make_scanner
except ImportError:  # pragma: no cover - CPython always ships _json
    # The pure-Python scanner reads parse_object/parse_array/parse_string off
    # a real JSONDecoder, which _Context below is not: use one.
    import json as _json_module

    _encode_str = _json_module.encoder.encode_basestring_ascii
    _make_scanner = None

_WHITESPACE = " \t\n\r"
_INF = float("inf")


class _Context:
    """The attributes the scanner reads off a JSONDecoder, at their defaults."""

    strict = True
    object_hook = None
    object_pairs_hook = None
    parse_float = float
    parse_int = int
    # json maps NaN / Infinity / -Infinity to floats; float() does the same.
    parse_constant = float
    memo: dict[Any, Any] = {}


if _make_scanner is not None:
    _scan_once = _make_scanner(_Context())
else:  # pragma: no cover - non-CPython
    _decoder = _json_module.JSONDecoder()

    def _scan_once(text: str, start: int) -> Any:
        try:
            return _decoder.raw_decode(text, start)
        except _json_module.JSONDecodeError as exc:
            raise StopIteration(exc.pos) from None


def loads(text: str | bytes) -> Any:
    """json.loads for str or UTF-8 bytes; raises ValueError on bad input."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8-sig")
    start = len(text) - len(text.lstrip(_WHITESPACE))
    try:
        value, end = _scan_once(text, start)
    except StopIteration as exc:
        raise ValueError(f"Expecting value at char {exc.value}") from None
    if text[end:].strip(_WHITESPACE):
        raise ValueError(f"Extra data at char {end}")
    return value


def dumps(value: Any) -> str:
    """json.dumps(value) for the plain JSON types; TypeError for anything else."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _encode_str(value)
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value in (_INF, -_INF):
            return "Infinity" if value > 0 else "-Infinity"
        return float.__repr__(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_key(k)}: {dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(dumps(item) for item in value) + "]"
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _key(key: Any) -> str:
    if isinstance(key, str):
        return _encode_str(key)
    if key is None or isinstance(key, (bool, int, float)):
        return _encode_str(dumps(key))
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")
