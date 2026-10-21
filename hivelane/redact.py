"""Secret redaction for everything the daemon sends off the machine.

Diffs, prompts, plan summaries and intent text pass through a Redactor before
they reach the relay (and, in an e2e room, before they are encrypted: a
teammate's dashboard has no more business seeing an API key than the relay
does). A likely secret is replaced by `[REDACTED:<kind>]`; the rest of the text
is left exactly as it was, so a diff still reads as a diff.

Two layers:

* Whole files: a diff of a file whose name says it holds secrets (`.env*`,
  `*.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials*.json`, `.npmrc`,
  `.netrc`, `.git-credentials`, `*.tfstate`, `kubeconfig`, `.aws/credentials`,
  ...) is never sent at all; the path still goes out (so overlaps are still
  detected) with the diff replaced by WITHHELD.
* Patterns: well-known credential formats (AWS, GitHub, GitLab, Slack, Stripe,
  Google, OpenAI/Anthropic, npm, PyPI, Hugging Face, SendGrid, Twilio, Azure
  keys, PEM/PGP/PuTTY private keys, JWTs, `Authorization: Bearer` values,
  passwords inside URLs and connection strings), quoted values assigned to
  secret-sounding names (`api_key = "..."`, `const apiKey: string = "..."`),
  `.env`-style `KEY=value` lines, unquoted `key: value` secrets in config
  files and in prompts, plus a repo's own `redact_patterns` from
  `.hivelane.toml`.

A name is secret-sounding by its words, not by substring: `api_key`,
`clientSecret`, `DB_PASSWORD` are; `tokenizer`, `max_tokens`, `bypass_mode`,
`token_url` and `credentials_path` are not.

File paths are never rewritten: the header lines of a unified diff
(`diff --git`, `--- a/...`, `+++ b/...`) are skipped, but only where a header
can actually be (before the first hunk, or after a hunk's line counts are
used up), so a content line that merely looks like one is still redacted.
Callers never pass a path through text().

Speed matters: post_edit runs this on every batch. Each rule only runs when a
cheap substring trigger is present (a C-speed `in` scan), every regex is
compiled once at import and none of them backtracks super-linearly; a 200 KB
diff takes a few milliseconds.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable
from typing import Optional

WITHHELD = "[withheld: sensitive file]"

# Basenames whose content is a secret by convention. Matched case-insensitively.
_SENSITIVE_NAME = re.compile(
    r"""^(?:
        \.env.*                                   # .env, .env.local, .env.production
      | .+\.env                                   # prod.env, staging.env
      | .*\.(?:pem|key|p12|pfx|jks|keystore|ppk|kdbx)  # private keys and keystores
      | id_(?:rsa|dsa|ecdsa|ed25519)(?:[._-].*)?  # ssh keys (and their .pub twins)
      | credentials.*\.json                       # cloud service-account files
      | service[-_]?account.*\.json
      | .+[-_]key\.json                           # gcp-key.json, sa_key.json
      | secrets?\.(?:ya?ml|json|toml)
      | \.npmrc | \.pypirc | [._]netrc | \.git-credentials | \.pgpass | \.htpasswd
      | \.vault-token | \.dockercfg
      | .*\.tfstate(?:\.backup)? | .*\.tfvars(?:\.json)?
      | kubeconfig(?:[._-].*)?
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
# Whole relative paths (forward slashes) that hold secrets though their
# basename is generic.
_SENSITIVE_PATH = re.compile(
    r"(?:^|/)(?:\.aws/(?:credentials|config)|\.docker/config\.json|\.kube/config"
    r"|\.config/gh/hosts\.ya?ml|\.terraform\.d/credentials\.tfrc\.json)$",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"^(?:\.env|.+\.env$)", re.IGNORECASE)
# Config formats where `password: hunter2abc` (unquoted) is data, not code.
_CONFIG_EXTENSIONS = (
    ".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml", ".toml", ".env",
)


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def is_sensitive_file(path: str) -> bool:
    """True for files whose diffs are never sent (see module docstring)."""
    if not path:
        return False
    if _SENSITIVE_NAME.match(_basename(path)) is not None:
        return True
    return _SENSITIVE_PATH.search(path.replace("\\", "/")) is not None


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


# A value naming the kind of thing it is ("X-Hivelane-Token", "bearer_token",
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


# --- secret-sounding names, by whole words ---------------------------------------

# One word that makes a name secret.
_SECRET_WORDS = frozenset(
    {
        "password", "passwd", "passphrase", "pass", "pwd", "secret", "secrets",
        "token", "apikey", "accesskey", "authkey", "privatekey", "secretkey",
        "clientsecret", "credential", "credentials", "creds", "bearer",
    }
)
# Two adjacent words that do (api_key, access-key, privateKey, ...).
_SECRET_PAIRS = frozenset(
    {
        ("api", "key"), ("access", "key"), ("auth", "key"), ("private", "key"),
        ("secret", "key"), ("signing", "key"), ("encryption", "key"), ("master", "key"),
        ("client", "secret"), ("account", "key"), ("auth", "token"),
    }
)
# A last word that makes the name about a secret rather than the secret
# itself: token_url, secret_arn, credentials_path, password_hash_algo, ...
_NOT_SECRET_SUFFIXES = frozenset(
    {
        "path", "paths", "file", "files", "filename", "dir", "url", "uri", "endpoint",
        "host", "name", "names", "id", "ids", "arn", "hint", "algo", "algorithm",
        "type", "kind", "field", "label", "header", "len", "length", "count",
        "size", "prefix", "env", "var", "mode", "format", "version", "expiry",
        "expires", "expiration", "ttl", "scope", "scopes", "policy", "regex",
        "pattern", "validator", "required", "enabled", "help", "placeholder",
        "column", "attr", "param", "key",
    }
)
_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _name_words(name: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(name)]


def is_secret_name(name: str) -> bool:
    """Whether an identifier (or mapping key) names a secret, word by word:
    `db_password`, `apiKey`, `CLIENT_SECRET`, `x-auth-token` do; `tokenizer`,
    `max_tokens`, `bypass`, `token_url`, `password_hash_algo` do not."""
    words = _name_words(name)
    if not words:
        return False
    if words[-1] in _NOT_SECRET_SUFFIXES and not (
        # ..._key at the end is fine when it completes a secret pair (api_key).
        words[-1] == "key" and len(words) >= 2 and (words[-2], "key") in _SECRET_PAIRS
    ):
        return False
    if any(word in _SECRET_WORDS for word in words):
        return True
    return any(pair in _SECRET_PAIRS for pair in zip(words, words[1:]))


# Names that may hold a value with spaces in it (a passphrase).
_SPACED_SECRET_WORDS = frozenset({"password", "passwd", "passphrase", "pass", "pwd", "secret"})


def _allows_spaces(name: str) -> bool:
    return any(word in _SPACED_SECRET_WORDS for word in _name_words(name))


# A quoted value of 8+ characters right after an assignment or a mapping
# separator, optionally with a Python/JS string prefix (f"", b"", r""); whether
# it counts depends on the name before it (looked up by _name_before, not by the
# regex: a regex that starts at the name backtracks over every identifier in
# the file and is 100x slower). Matches
#   api_key = "sk_abc...", "password": "hunter2!x", SECRET_TOKEN := 'abc123..'
# but not `token = get_token()` (no literal), `token == "abcdefgh"`
# (comparison) or `token = "Bearer " + x` (whitespace in the literal, allowed
# only for password-like names).
_QUOTED_VALUE = re.compile(
    r"(?P<sep>[:=][=>]?)[ \t]*(?:[fFbBrRuU]{1,2})?(?P<q>[\"'`])(?P<value>[^\"'`\r\n]{8,400})(?P=q)"
)
_ASSIGNMENT_SEPARATORS = frozenset((":", "=", ":=", "=>"))
# The unquoted form at the start of a line, for config files and free text
# (in code, `password = hunter2abc` names a variable).
_UNQUOTED_ASSIGNMENT = re.compile(
    r"(?m)^[+\- ]?[ \t]*(?:export[ \t]+)?[\"']?(?P<name>[A-Za-z0-9_.\-]+)[\"']?"
    r"[ \t]*[:=][ \t]*(?P<value>[^\s\"'`#]{8,})[ \t]*$"
)
# KEY=value lines of a .env file: every value is a secret by convention. The
# value starts with a non-space so `[ \t]*` and the value never compete for
# the same characters (the old form was quadratic on a long run of spaces).
_ENV_LINE = re.compile(
    r"(?m)^[+\- ]?[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_.]*)"
    r"[ \t]*=[ \t]*(?P<value>[^\s#](?:[^\r\n#]*[^\s#])?)"
)
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
# What a type annotation between a name and its `=` may contain:
# `apiKey: string`, `password?: string | undefined`, `token: Optional[str]`.
_TYPE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.[]<>|, ?")


def _name_at(text: str, i: int) -> str:
    j = i
    while j > 0 and i - j < 80 and text[j - 1] in _NAME_CHARS:
        j -= 1
    return text[j:i]


def _names_before(text: str, end: int, sep: str) -> tuple[str, ...]:
    """The identifier (or quoted key) ending just before `end`, skipping the
    spaces and closing quote/bracket between it and the separator; and, past a
    type annotation, the annotated name: `const apiKey: string = "..."` gives
    (string, apiKey). Either name being secret-sounding counts."""
    i = end
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i > 0 and text[i - 1] in "\"']":
        i -= 1
    name = _name_at(text, i)
    if sep != "=" and sep != ":=":
        return (name,)
    # Look back over a type annotation for its `:`.
    k = i - len(name)
    limit = max(0, k - 80)
    while k > limit and text[k - 1] in _TYPE_CHARS:
        k -= 1
    if k > 0 and text[k - 1] == ":" and (k < 2 or text[k - 2] != ":"):
        k -= 1
        if k > 0 and text[k - 1] == "?":
            k -= 1
        while k > 0 and text[k - 1] in " \t":
            k -= 1
        annotated = _name_at(text, k)
        if annotated:
            return (name, annotated)
    return (name,)


def _has_digit_and_letter(match: "re.Match[str]") -> bool:
    token = match.group(0)
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def _value_has_digit(match: "re.Match[str]") -> bool:
    value = match.group("value")
    return any(c.isdigit() for c in value) and _looks_real(value)


def _value_looks_real(match: "re.Match[str]") -> bool:
    return _looks_real(match.group("value"))


def _url_password_real(match: "re.Match[str]") -> bool:
    value = match.group("value")
    return not _PLACEHOLDER.search(value) and len(set(value)) > 1 and not value.startswith("[REDACTED:")


def _conn_string_password(match: "re.Match[str]") -> bool:
    # `Password=x;` or `;Password=x`: a connection string, not a kwarg.
    text = match.string
    i = match.start()
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    pre = i > 0 and text[i - 1] == ";"
    return (pre or match.group("post") == ";") and _url_password_real(match)


_Check = Optional[Callable[["re.Match[str]"], bool]]

# (kind, literal prefixes, pattern, optional check, case_insensitive). Each
# pattern is only tried with .match() where one of its prefixes occurs (found
# with str.find on the text, or on its lowercased copy for case-insensitive
# rules, at C speed), so a rule costs nothing on text that cannot contain it.
# The whole match is replaced, or only its `value` group when it has one.
_TOKEN_RULES: tuple[tuple[str, tuple[str, ...], "re.Pattern[str]", _Check, bool], ...] = (
    (
        "private-key",
        ("-----BEGIN ",),
        # To the END line, or (a truncated diff) to the end of the text. PGP's
        # armour says `PRIVATE KEY BLOCK`.
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
            r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
        None,
        False,
    ),
    (
        "private-key",
        ("Private-Lines:",),  # PuTTY .ppk
        re.compile(r"Private-Lines:[ \t]*\d+[\s\S]*?(?:Private-MAC:[^\r\n]*|\Z)"),
        None,
        False,
    ),
    (
        "aws-access-key",
        ("AKIA", "ASIA", "AGPA", "AIDA", "AROA", "AIPA", "ANPA", "ANVA", "AKPA"),
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|AKPA)[A-Z0-9]{16}\b"),
        None,
        False,
    ),
    (
        "github-token",
        ("ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_"),
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
        None,
        False,
    ),
    (
        "gitlab-token",
        ("glpat-", "gldt-", "glptt-", "glrt-", "glsoat-", "glcbt-", "glimt-", "gloas-"),
        re.compile(r"\bgl(?:pat|dt|ptt|rt|soat|cbt|imt|oas)-[A-Za-z0-9_\-]{20,255}"),
        None,
        False,
    ),
    (
        "slack-token",
        ("xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-", "xoxe", "xapp-", "https://hooks.slack.com/"),
        re.compile(
            r"\bxox(?:[abposr]|e(?:\.xox[bp])?)-[A-Za-z0-9-]{10,250}\b"
            r"|\bxapp-[A-Za-z0-9-]{10,250}\b"
            r"|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}"
        ),
        None,
        False,
    ),
    (
        "stripe-key",
        ("sk_live_", "rk_live_", "sk_test_", "rk_test_", "whsec_"),
        re.compile(r"\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,247}|whsec_[A-Za-z0-9]{24,}\b)\b"),
        None,
        False,
    ),
    (
        "google-api-key",
        ("AIza",),
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"),
        None,
        False,
    ),
    (
        "anthropic-key",
        ("sk-ant-",),
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        None,
        False,
    ),
    (
        "openai-key",
        ("sk-",),
        # sk-<48 alnum> (legacy), sk-proj-/sk-svcacct-/sk-admin-<long>. Must
        # mix letters and digits, so a CSS class like sk-loading-spinner-large
        # is left alone.
        re.compile(r"(?<![A-Za-z0-9_\-])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
        _has_digit_and_letter,
        False,
    ),
    (
        "npm-token",
        ("npm_",),
        re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
        None,
        False,
    ),
    (
        "npm-token",
        ("_authtoken", "_auth=", "_password="),  # .npmrc lines inside prompts or docs
        re.compile(r"(?i)_(?:authtoken|auth|password)[ \t]*=[ \t]*[\"']?(?P<value>[^\s\"'#]{8,})"),
        _value_looks_real,
        True,
    ),
    (
        "pypi-token",
        ("pypi-AgE",),
        re.compile(r"\bpypi-AgE[A-Za-z0-9_\-]{50,}"),
        None,
        False,
    ),
    (
        "huggingface-token",
        ("hf_",),
        re.compile(r"\bhf_[A-Za-z0-9]{30,40}\b"),
        None,
        False,
    ),
    (
        "sendgrid-key",
        ("SG.",),
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,32}\.[A-Za-z0-9_\-]{30,64}"),
        None,
        False,
    ),
    (
        "twilio-key",
        ("AC", "SK"),
        re.compile(r"\b(?:AC|SK)[0-9a-f]{32}\b"),
        None,
        False,
    ),
    (
        "azure-key",
        ("accountkey=", "sharedaccesskey=", "sig="),
        # Storage/Service Bus connection strings, and a SAS token's signature.
        re.compile(
            r"(?i)(?:account|sharedaccess)key[ \t]*=[ \t]*(?P<value>[A-Za-z0-9+/]{20,}={0,2})"
            r"|sig=(?P<sig>[A-Za-z0-9%+/]{20,}(?:%3D|=){0,2})"
        ),
        None,
        True,
    ),
    (
        "url-password",
        ("://",),
        # scheme://user:password@host: only the password goes.
        re.compile(r"://[^\s:/@\"'`<>]{0,128}:(?P<value>[^\s@/\"'`<>]{1,256})@"),
        _url_password_real,
        False,
    ),
    (
        "connection-password",
        ("password=", "pwd="),
        # JDBC/ODBC/ADO.NET: Server=x;User Id=u;Password=s3cret;
        re.compile(r"(?i)(?:password|pwd)=(?P<value>[^;\"'\s(),]{3,256})(?P<post>;?)"),
        _conn_string_password,
        True,
    ),
    (
        "docker-auth",
        ('"auth"',),
        re.compile(r"\"auth\"[ \t]*:[ \t]*\"(?P<value>[A-Za-z0-9+/]{12,}={0,2})\""),
        None,
        False,
    ),
    (
        "auth-header",
        ("bearer ", "basic ", "token "),
        # Authorization: Bearer <token> / Basic <base64> / token <ghp...>.
        re.compile(r"(?i)\b(?:bearer|basic|token)[ \t]+(?P<value>[A-Za-z0-9\-._~+/]{16,}=*)"),
        _value_has_digit,
        True,
    ),
    (
        "mysql-password",
        ("mysql",),
        re.compile(r"(?i)mysql\w{0,10}[^\r\n]{0,200}?[ \t](?:-p|--password=)(?P<value>[^\s\"']{3,})"),
        _url_password_real,
        True,
    ),
    (
        "jwt",
        ("eyJ",),
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        None,
        False,
    ),
)
# 40 base64 characters are too generic to match alone: only a value assigned
# to an aws...secret name counts, and only the value is replaced. Found from
# the (lowercased) "aws" prefix.
_AWS_SECRET = re.compile(
    r"(?i)aws[A-Za-z0-9_.\-]{0,30}(?:secret|private)[A-Za-z0-9_.\-]{0,30}"
    r"[\"']?[ \t]*(?::=|=>|=|:)[ \t]*[\"']?(?P<value>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
)

# --- unified diff headers ---------------------------------------------------------

_FILE_HEADER_MARKERS = ("diff --git ", "--- ", "+++ ", "index ", "new file mode", "deleted file mode",
                        "old mode", "new mode", "similarity index", "rename from", "rename to",
                        "copy from", "copy to", "Binary files ")
_HUNK = re.compile(r"@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")


def _header_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of the lines that name the file: header lines before the
    first hunk, or between a hunk whose counted lines are used up and the
    next one. A `--- a/x` line inside a hunk is a removed `-- a/x` line, and
    is content (it used to be trusted and exempted from every rule)."""
    spans: list[tuple[int, int]] = []
    old_left = new_left = 0
    pos = 0
    size = len(text)
    while pos < size:
        end = text.find("\n", pos)
        if end == -1:
            end = size
        if old_left > 0 or new_left > 0:
            first = text[pos] if pos < end else ""
            if first == "-":
                old_left -= 1
            elif first == "+":
                new_left -= 1
            elif first == "\\":
                pass  # "\ No newline at end of file"
            else:
                old_left -= 1
                new_left -= 1
        elif text.startswith("@@", pos):
            hunk = _HUNK.match(text, pos)
            if hunk is None:
                break  # not a diff we understand: trust no further headers
            old_left = int(hunk.group("old") or 1)
            new_left = int(hunk.group("new") or 1)
        elif text.startswith(_FILE_HEADER_MARKERS, pos):
            spans.append((pos, end))
        else:
            break  # content outside a hunk: not a unified diff past here
        pos = end + 1
    return spans


# (start, end, priority, kind): lower priority wins a tie at the same start.
_Span = tuple[int, int, int, str]


def _rule_matches(pattern: "re.Pattern[str]", haystack: str, text: str, prefix: str):
    pos = haystack.find(prefix)
    while pos != -1:
        match = pattern.match(text, pos)
        if match is not None:
            yield match
            pos = haystack.find(prefix, max(match.end(), pos + 1))
        else:
            pos = haystack.find(prefix, pos + 1)


def _token_spans(text: str, lowered: Optional[str], folded: str) -> list[_Span]:
    spans: list[_Span] = []
    for kind, prefixes, pattern, check, insensitive in _TOKEN_RULES:
        if insensitive and lowered is None:
            # Offsets of a lowercased copy would not line up: scan the text
            # itself, but only when a trigger occurs at all.
            if not any(prefix in folded for prefix in prefixes):
                continue
            matches: Iterable[re.Match[str]] = pattern.finditer(text)
        else:
            haystack = lowered if insensitive else text
            assert haystack is not None
            matches = (m for prefix in prefixes for m in _rule_matches(pattern, haystack, text, prefix))
        for match in matches:
            if check is not None and not check(match):
                continue
            groups = match.re.groupindex
            if "value" in groups and match.group("value") is not None:
                spans.append((match.start("value"), match.end("value"), 0, kind))
            elif "sig" in groups and match.group("sig") is not None:
                spans.append((match.start("sig"), match.end("sig"), 0, kind))
            else:
                spans.append((match.start(), match.end(), 0, kind))
    if lowered is None:
        aws: Iterable[re.Match[str]] = _AWS_SECRET.finditer(text)
    else:
        aws = _rule_matches(_AWS_SECRET, lowered, text, "aws")
    for match in aws:
        spans.append((match.start("value"), match.end("value"), 0, "aws-secret-key"))
    return spans


def _assignment_spans(text: str, path: Optional[str]) -> list[_Span]:
    spans: list[_Span] = []
    for match in _QUOTED_VALUE.finditer(text):
        start = match.start()
        sep = match.group("sep")
        # `==`, `!=`, `<=`, `>=`: a comparison against a literal, not a secret.
        if sep not in _ASSIGNMENT_SEPARATORS or (start and text[start - 1] in "=!<>"):
            continue
        value = match.group("value")
        name = next((n for n in _names_before(text, start, sep) if n and is_secret_name(n)), "")
        if not name:
            continue
        if (" " in value or "\t" in value) and not _allows_spaces(name):
            continue
        if value.strip() != value or not _looks_real(value):
            continue
        spans.append((match.start("value"), match.end("value"), 1, "secret"))
    # Unquoted `password: hunter2abc` lines: data in a config file, and in free
    # text (a prompt or a plan quoting a config). In code it names a variable.
    if path is None or _is_config_file(path):
        for match in _UNQUOTED_ASSIGNMENT.finditer(text):
            value = match.group("value")
            if path is None and value.replace("_", "").replace(".", "").isalpha():
                continue  # prose: `password: required`, `token = refreshToken`
            if is_secret_name(match.group("name")) and _looks_real(value):
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
    folded = text.lower()
    # Some non-ASCII characters lowercase to two; offsets would not line up.
    lowered: Optional[str] = folded if len(folded) == len(text) else None
    spans = _token_spans(text, lowered, folded)
    spans.extend(_assignment_spans(text, path))
    for pattern in extra:
        spans.extend((m.start(), m.end(), 3, "custom") for m in pattern.finditer(text) if m.end() > m.start())
    if not spans:
        return text
    if protect_headers:
        headers = _header_spans(text)
        if headers:
            starts = [h[0] for h in headers]

            def in_header(span: _Span) -> bool:
                # The header that starts at or before the span's end - 1.
                i = bisect.bisect_right(starts, span[1] - 1) - 1
                return i >= 0 and headers[i][1] > span[0]

            spans = [s for s in spans if not in_header(s)]
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
        from, never rewritten itself) enables the per-file-type rules; with no
        path the text is treated as free text (a prompt, a plan)."""
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
