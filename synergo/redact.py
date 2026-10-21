"""Secret redaction for everything the daemon sends off the machine.

Diffs, prompts, plan summaries and intent text pass through a Redactor before
they reach the relay (and, in an e2e room, before they are encrypted: a
teammate's dashboard has no more business seeing an API key than the relay
does). A likely secret is replaced by `[REDACTED:<kind>]`; the rest of the text
is left exactly as it was, so a diff still reads as a diff.

Two layers:

* Whole files: a diff of a file whose name says it holds secrets (`.env*`,
  `*.pem`, `*.key`, `id_rsa*`, `credentials*.json`, ...) is never sent at all;
  the path still goes out (so overlaps are still detected) with the diff
  replaced by WITHHELD.
* Patterns: well-known credential formats (AWS, GitHub, Slack, Stripe, Google,
  OpenAI/Anthropic keys, PEM private key blocks, JWTs), quoted values assigned
  to secret-sounding names (`api_key = "..."`), `.env`-style `KEY=value` lines
  in files named `.env*`, unquoted `key: value` secrets in config files, plus a
  repo's own `redact_patterns` from `.synergo.toml`.

File paths are never rewritten: diff header lines (`diff --git`, `--- a/...`,
`+++ b/...`) are skipped, and callers never pass a path through text().

Speed matters: post_edit runs this on every batch. Each rule only runs when a
cheap substring trigger is present (a C-speed `in` scan), and every regex is
compiled once at import; a 200 KB diff takes a few milliseconds.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Optional

WITHHELD = "[withheld: sensitive file]"

# Basenames whose content is a secret by convention. Matched case-insensitively.
_SENSITIVE_NAME = re.compile(
    r"""^(?:
        \.env.*                                   # .env, .env.local, .env.production
      | .*\.(?:pem|key|p12|pfx|jks|keystore)      # private keys and keystores
      | id_(?:rsa|dsa|ecdsa|ed25519)(?:[._-].*)?  # ssh keys (and their .pub twins)
      | credentials.*\.json                       # cloud service-account files
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
_ENV_NAME = re.compile(r"^\.env", re.IGNORECASE)
# Config formats where `password: hunter2abc` (unquoted) is data, not code.
_CONFIG_EXTENSIONS = (
    ".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml", ".toml", ".env",
)


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def is_sensitive_file(path: str) -> bool:
    """True for files whose diffs are never sent (see module docstring)."""
    return bool(path) and _SENSITIVE_NAME.match(_basename(path)) is not None


def _is_env_file(path: Optional[str]) -> bool:
    return bool(path) and _ENV_NAME.match(_basename(path or "")) is not None


def _is_config_file(path: Optional[str]) -> bool:
    return bool(path) and _basename(path or "").lower().endswith(_CONFIG_EXTENSIONS)


def _tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# A placeholder rather than a secret: an env reference, a template slot, a mask.
_PLACEHOLDER = re.compile(
    r"^(?:\$|\{\{|\{|<|%\(|%[A-Z_]|\*{3,}|x{6,}|X{6,}|\.{3,})|^(?:changeme|change_me|"
    r"password|secret|token|example|placeholder|your[_-].*|none|null|true|false|redacted)$",
    re.IGNORECASE,
)


# A value naming the kind of thing it is ("X-Synergo-Token", "bearer_token",
# "db-credentials") is a label in code, not a credential: real secrets do not
# spell out the word. Only trusted when the value has no digits.
_LABEL_WORDS = ("token", "pass", "secret", "key", "auth", "bearer", "credential", "header")


def _looks_real(value: str) -> bool:
    """Whether a value assigned to a secret-sounding name is worth redacting."""
    if _PLACEHOLDER.search(value):
        return False
    if value.startswith("[REDACTED:"):
        return False
    # One repeated character ("aaaaaaaa") is a mask, not a credential.
    if len(set(value)) <= 2:
        return False
    # A URL without userinfo (https://host/oauth/token) or a filesystem path.
    if "://" in value and "@" not in value:
        return False
    if value[0] in "/~" or value.startswith("./") or "\\" in value:
        return False
    lowered = value.lower()
    if not any(c.isdigit() for c in value) and any(word in lowered for word in _LABEL_WORDS):
        return False
    return True


_SECRET_NAME = re.compile(
    r"(?i)(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|auth[_-]?key|client[_-]?secret|private[_-]?key|credentials?|bearer)"
)
# A quoted value of 8+ non-space characters right after an assignment or a
# mapping separator; whether it counts depends on the name before it (looked
# up by _name_before, not by the regex: a regex that starts at the name
# backtracks over every identifier in the file and is 100x slower). Matches
#   api_key = "sk_abc...", "password": "hunter2!x", SECRET_TOKEN := 'abc123..'
# but not `token = get_token()` (no literal), `token == "abcdefgh"`
# (comparison) or `token = "Bearer " + x` (whitespace in the literal).
_QUOTED_VALUE = re.compile(
    r"(?P<sep>[:=][=>]?)[ \t]*(?P<q>[\"'`])(?P<value>[^\"'`\s]{8,})(?P=q)"
)
_ASSIGNMENT_SEPARATORS = frozenset((":", "=", ":=", "=>"))
# The unquoted form at the start of a line, for config files only (in code,
# `password = hunter2abc` names a variable).
_UNQUOTED_ASSIGNMENT = re.compile(
    r"(?m)^[+\- ]?[ \t]*(?:export[ \t]+)?[\"']?(?P<name>[A-Za-z0-9_.\-]+)[\"']?"
    r"[ \t]*[:=][ \t]*(?P<value>[^\s\"'`#]{8,})[ \t]*$"
)
# KEY=value lines of a .env file: every value is a secret by convention.
_ENV_LINE = re.compile(
    r"(?m)^[+\- ]?[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_.]*)"
    r"[ \t]*=[ \t]*(?P<value>[^\r\n#]*[^\s#])"
)
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")


def _name_before(text: str, end: int) -> str:
    """The identifier (or quoted key) ending just before `end`, skipping the
    spaces and closing quote/bracket between it and the separator."""
    i = end
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i > 0 and text[i - 1] in "\"']":
        i -= 1
    j = i
    while j > 0 and i - j < 80 and text[j - 1] in _NAME_CHARS:
        j -= 1
    return text[j:i]


def _has_digit_and_letter(match: "re.Match[str]") -> bool:
    token = match.group(0)
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


_Check = Optional[Callable[["re.Match[str]"], bool]]

# (kind, literal prefixes, pattern, optional check). Each pattern is only tried
# with .match() where one of its prefixes occurs (found with str.find, which
# runs at C speed), so a rule costs nothing on text that cannot contain it.
# The whole match is replaced.
_TOKEN_RULES: tuple[tuple[str, tuple[str, ...], "re.Pattern[str]", _Check], ...] = (
    (
        "private-key",
        ("-----BEGIN ",),
        # To the END line, or (a truncated diff) to the end of the text.
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
            r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"
        ),
        None,
    ),
    (
        "aws-access-key",
        ("AKIA", "ASIA", "AGPA", "AIDA", "AROA", "AIPA", "ANPA", "ANVA", "AKPA"),
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|AKPA)[A-Z0-9]{16}\b"),
        None,
    ),
    (
        "github-token",
        ("ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_"),
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
        None,
    ),
    (
        "slack-token",
        ("xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-", "https://hooks.slack.com/"),
        re.compile(
            r"\bxox[abposr]-[A-Za-z0-9-]{10,250}\b"
            r"|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}"
        ),
        None,
    ),
    (
        "stripe-key",
        ("sk_live_", "rk_live_"),
        re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,247}\b"),
        None,
    ),
    (
        "google-api-key",
        ("AIza",),
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"),
        None,
    ),
    (
        "anthropic-key",
        ("sk-ant-",),
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        None,
    ),
    (
        "openai-key",
        ("sk-",),
        # sk-<48 alnum> (legacy), sk-proj-/sk-svcacct-/sk-admin-<long>. Must
        # mix letters and digits, so a CSS class like sk-loading-spinner-large
        # is left alone.
        re.compile(r"(?<![A-Za-z0-9_\-])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
        _has_digit_and_letter,
    ),
    (
        "jwt",
        ("eyJ",),
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        None,
    ),
)
# 40 base64 characters are too generic to match alone: only a value assigned
# to an aws...secret name counts, and only the value is replaced. Found from
# the (lowercased) "aws" prefix.
_AWS_SECRET = re.compile(
    r"(?i)aws[A-Za-z0-9_.\-]{0,30}(?:secret|private)[A-Za-z0-9_.\-]{0,30}"
    r"[\"']?[ \t]*(?::=|=>|=|:)[ \t]*[\"']?(?P<value>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
)
# Lines that name the file (unified_diff's and git's): never rewritten.
_HEADER_MARKERS = ("diff --git ", "--- a/", "+++ b/", "--- /dev/null", "+++ /dev/null")


def _header_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for marker in _HEADER_MARKERS:
        if text.startswith(marker):
            end = text.find("\n")
            spans.append((0, len(text) if end == -1 else end))
        pos = text.find("\n" + marker)
        while pos != -1:
            end = text.find("\n", pos + 1)
            spans.append((pos + 1, len(text) if end == -1 else end))
            pos = text.find("\n" + marker, pos + 1)
    return spans

# (start, end, priority, kind): lower priority wins a tie at the same start.
_Span = tuple[int, int, int, str]


def _token_spans(text: str, lowered: Optional[str]) -> list[_Span]:
    spans: list[_Span] = []
    for kind, prefixes, pattern, check in _TOKEN_RULES:
        for prefix in prefixes:
            pos = text.find(prefix)
            while pos != -1:
                match = pattern.match(text, pos)
                if match is not None and (check is None or check(match)):
                    spans.append((match.start(), match.end(), 0, kind))
                    pos = text.find(prefix, max(match.end(), pos + 1))
                else:
                    pos = text.find(prefix, pos + 1)
    if lowered is None:
        matches = _AWS_SECRET.finditer(text)
    else:
        matches = _matches_at(_AWS_SECRET, text, lowered, "aws")
    for match in matches:
        spans.append((match.start("value"), match.end("value"), 0, "aws-secret-key"))
    return spans


def _matches_at(pattern: "re.Pattern[str]", text: str, lowered: str, prefix: str):
    pos = lowered.find(prefix)
    while pos != -1:
        match = pattern.match(text, pos)
        if match is not None:
            yield match
            pos = lowered.find(prefix, max(match.end(), pos + 1))
        else:
            pos = lowered.find(prefix, pos + 1)


def _assignment_spans(text: str, path: Optional[str]) -> list[_Span]:
    spans: list[_Span] = []
    for match in _QUOTED_VALUE.finditer(text):
        start = match.start()
        # `==`, `!=`, `<=`, `>=`: a comparison against a literal, not a secret.
        if match.group("sep") not in _ASSIGNMENT_SEPARATORS or (start and text[start - 1] in "=!<>"):
            continue
        name = _name_before(text, start)
        if name and _SECRET_NAME.search(name) and _looks_real(match.group("value")):
            spans.append((match.start("value"), match.end("value"), 1, "secret"))
    if _is_config_file(path):
        for match in _UNQUOTED_ASSIGNMENT.finditer(text):
            if _SECRET_NAME.search(match.group("name")) and _looks_real(match.group("value")):
                spans.append((match.start("value"), match.end("value"), 1, "secret"))
    if _is_env_file(path):
        for match in _ENV_LINE.finditer(text):
            if _looks_real(match.group("value")):
                spans.append((match.start("value"), match.end("value"), 2, "env-value"))
    return spans


def _redact(
    text: str,
    path: Optional[str],
    extra: tuple["re.Pattern[str]", ...],
    *,
    protect_headers: bool = False,
) -> str:
    lowered: Optional[str] = text.lower()
    if len(lowered) != len(text):
        # Some non-ASCII characters lowercase to two; offsets would not line up.
        lowered = None
    spans = _token_spans(text, lowered)
    spans.extend(_assignment_spans(text, path))
    for pattern in extra:
        spans.extend((m.start(), m.end(), 3, "custom") for m in pattern.finditer(text) if m.end() > m.start())
    if not spans:
        return text
    if protect_headers:
        headers = _header_spans(text)
        if headers:
            spans = [s for s in spans if not any(s[0] < h_end and h_start < s[1] for h_start, h_end in headers)]
    spans.sort(key=lambda span: (span[0], span[2], -span[1]))
    out: list[str] = []
    cursor = 0
    for start, end, _priority, kind in spans:
        if start < cursor:
            continue  # inside a span already replaced
        out.append(text[cursor:start])
        out.append(_tag(kind))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


class Redactor:
    """Configured redaction for one repo (`redact`, `redact_patterns`)."""

    def __init__(self, enabled: bool = True, extra_patterns: Iterable[str] = ()) -> None:
        self.enabled = enabled
        self.patterns: tuple[str, ...] = tuple(p for p in extra_patterns if p)
        # Raises re.error for a bad pattern: config validation reports it.
        self._extra = tuple(re.compile(p) for p in self.patterns)

    def __repr__(self) -> str:
        return f"Redactor(enabled={self.enabled}, extra_patterns={list(self.patterns)!r})"

    # --- text ----------------------------------------------------------------

    def text(self, value: Optional[str], path: Optional[str] = None) -> Optional[str]:
        """value with likely secrets replaced. `path` (the file the text came
        from, never rewritten itself) enables the per-file-type rules."""
        if not value or not self.enabled:
            return value
        return _redact(value, path, self._extra)

    def diff(self, path: str, diff: str) -> str:
        """What may be sent of path's diff: WITHHELD for a sensitive file,
        else the diff with secrets redacted and its header lines untouched."""
        if not diff:
            return diff
        if is_sensitive_file(path):
            # Withheld whether or not redaction is on: turning `redact` off is
            # for false positives in code, not for shipping private keys.
            return WITHHELD
        if not self.enabled:
            return diff
        # The header lines carry the file's path, which is never rewritten.
        return _redact(diff, path, self._extra, protect_headers=True)


DEFAULT = Redactor()


def validate_patterns(patterns: Iterable[str]) -> Optional[str]:
    """None if every pattern compiles, else a message naming the bad one."""
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"{pattern!r} is not a valid regular expression ({exc})"
    return None
