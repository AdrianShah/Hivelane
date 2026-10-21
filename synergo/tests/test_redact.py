"""synergo.redact: what may leave the machine. Realistic secrets are caught,
ordinary code is left alone, sensitive files are withheld whole, paths are
never rewritten, and a 200 KB diff costs a few milliseconds."""

from __future__ import annotations

import time

import pytest

from synergo import config as cfg
from synergo.daemon import unified_diff
from synergo.redact import WITHHELD, Redactor, is_sensitive_file, validate_patterns

R = Redactor()

# Realistic shapes (none of these are live credentials).
POSITIVES = [
    ('AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"', "aws-access-key"),
    ('aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"', "aws-secret-key"),
    ("token = 'ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + "'", "github-token"),
    ("GH=gho_" + "Z" * 36, "github-token"),
    ("pat: github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", "github-token"),
    ("SLACK_BOT = 'xoxb-1234567890-abcdefghij'", "slack-token"),
    ("hook = 'https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX'", "slack-token"),
    ("stripe.api_key = 'sk_live_4eC39HqLyjWDarjtT1zdp7dc'", "stripe-key"),
    ("restricted = 'rk_live_4eC39HqLyjWDarjtT1zdp7dc'", "stripe-key"),
    ("maps_key=AIzaSyA-1234567890abcdefghijklmnopqrstu", "google-api-key"),
    ("OPENAI_API_KEY=sk-proj-abcdEFGH1234ijklMNOP5678qrstUVWX9012yzAB", "openai-key"),
    ("legacy = sk-" + "aB3" * 16, "openai-key"),
    ('client = Anthropic(api_key="sk-ant-api03-abcdefghijklmnop1234567890")', "anthropic-key"),
    ("auth = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'", "jwt"),
    ('db_password = "c0rrect-h0rse"', "secret"),
    ('"client_secret": "a8f5f167f44f4964e6c998dee827110c"', "secret"),
    ("SECRET_TOKEN := 'abc123def456'", "secret"),
    ("'api-key' => 'xk29dkd02kd9'", "secret"),
]

NEGATIVES = [
    "token = get_token()",
    'password = request.form["password"]',
    'TOKEN_HEADER = "X-Synergo-Token"',
    'api_key = os.environ["API_KEY"]',
    'secret_name = "db-credentials"',
    "def check_password(password: str) -> bool:",
    'token_url = "https://login.example.com/v2/oauth/token"',
    'const className = "sk-loading-spinner-container-large"',
    'password = "${DB_PASSWORD}"',
    "self.token = token",
    'if token == "abcdefgh1234":',
    'if password != "hunter2hunter2":',
    'secret_path = "/etc/app/secret.json"',
    'password = "changeme"',
    'masked = "********"',
    "AKIA is the prefix of an AWS key id",
    "the eyJ prefix starts every JWT",
    'url = f"{base}/tokens/{token_id}"',
]


@pytest.mark.parametrize(("line", "kind"), POSITIVES)
def test_realistic_secrets_are_redacted(line: str, kind: str) -> None:
    out = R.text(line, "app.py")
    assert f"[REDACTED:{kind}]" in out, out
    # Only the secret goes: what surrounds it is still there to read.
    head = line.split("=")[0].split(":")[0].split("(")[0].strip(" '\"")
    assert head in out


@pytest.mark.parametrize("line", NEGATIVES)
def test_ordinary_code_is_left_alone(line: str) -> None:
    assert R.text(line, "app.py") == line


def test_private_key_blocks_go_whole_even_unterminated() -> None:
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    assert R.text(f"key:\n{block}\ndone", "notes.md") == "key:\n[REDACTED:private-key]\ndone"
    truncated = "+-----BEGIN RSA PRIVATE KEY-----\n+MIIEpAIBAAKCAQEA\n+abc"
    assert R.text(truncated) == "+[REDACTED:private-key]"
    # A public key is not a secret.
    public = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBg\n-----END PUBLIC KEY-----"
    assert R.text(public) == public


def test_env_files_and_config_files_get_their_own_rules() -> None:
    env = "DATABASE_URL=postgres://u:p4ss@h/db\nPORT=8080\nDEBUG=true\nexport API=abc123xyz\n# NOTE=keep"
    out = R.text(env, "deploy/.env.local")
    assert out == "DATABASE_URL=[REDACTED:env-value]\nPORT=8080\nDEBUG=true\nexport API=[REDACTED:env-value]\n# NOTE=keep"
    yaml = "database:\n  password: hunter2isgreat\n  host: localhost\n  token: ${{ secrets.TOKEN }}"
    assert R.text(yaml, "config/app.yaml") == (
        "database:\n  password: [REDACTED:secret]\n  host: localhost\n  token: ${{ secrets.TOKEN }}"
    )
    # Unquoted in code is a variable name, not a value.
    assert R.text("password = hunter2isgreat", "app.py") == "password = hunter2isgreat"


@pytest.mark.parametrize(
    ("path", "sensitive"),
    [
        (".env", True),
        ("deploy/.env.production", True),
        ("certs/server.pem", True),
        ("tls/private.key", True),
        ("home/.ssh/id_rsa", True),
        ("home/.ssh/id_ed25519.pub", True),
        ("gcp/credentials-prod.json", True),
        ("credentials.json", True),
        ("src/keyboard.ts", False),
        ("monkey.py", False),
        ("docs/environment.md", False),
        ("src/credentials.py", False),
    ],
)
def test_sensitive_files(path: str, sensitive: bool) -> None:
    assert is_sensitive_file(path) is sensitive


def test_a_sensitive_files_diff_is_withheld_even_with_redaction_off() -> None:
    diff = unified_diff(".env", "A=1\n", "A=2\n")
    assert R.diff(".env", diff) == WITHHELD
    assert Redactor(enabled=False).diff("certs/server.pem", "-----BEGIN") == WITHHELD
    assert Redactor(enabled=False).diff("app.py", "+token = 'ghp_" + "a" * 36 + "'") == "+token = 'ghp_" + "a" * 36 + "'"


def test_paths_in_diff_headers_are_never_rewritten() -> None:
    odd = "src/ghp_" + "a" * 36 + "/config.py"
    diff = unified_diff(odd, "x = 1\n", "x = 'ghp_" + "b" * 36 + "'\n")
    out = R.diff(odd, diff)
    assert f"--- a/{odd}" in out and f"+++ b/{odd}" in out
    assert "+x = '[REDACTED:github-token]'" in out


def test_custom_patterns_and_validation() -> None:
    custom = Redactor(extra_patterns=[r"ACME-[0-9]{6}"])
    assert custom.text("ticket ACME-123456 and ACME-12") == "ticket [REDACTED:custom] and ACME-12"
    assert validate_patterns(["ok", "("]) is not None
    with pytest.raises(Exception):
        Redactor(extra_patterns=["("])


def test_a_200kb_diff_is_redacted_in_under_10ms() -> None:
    chunk = (
        "@@ -10,7 +10,9 @@ def handle(request):\n"
        "-    token = get_token(request)\n"
        "+    token = get_token(request, fresh=True)\n"
        "     user = lookup(token)\n"
        '+    password = request.form["password"]\n'
        "     if not user:\n"
        '         raise PermissionError("no such user: %s" % token)\n'
        "+    api_key = settings.API_KEY  # read from env\n"
        '     return render(user, key="profile")\n'
    )
    diff = "--- a/app.py\n+++ b/app.py\n" + chunk * (200_000 // len(chunk))
    diff += "\n+leak = 'ghp_" + "c" * 36 + "'\n+jwt = 'eyJabcdefghij.eyJabcdefghij.abcdefghij'\n"
    assert len(diff) >= 199_000
    best = float("inf")
    for _ in range(5):
        started = time.perf_counter()
        out = R.diff("app.py", diff)
        best = min(best, time.perf_counter() - started)
    assert out.count("[REDACTED:") == 2
    assert best < 0.010, f"redacting 200 KB took {best * 1000:.1f} ms"


def test_config_keys(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNERGO_HOME", str(tmp_path / "home"))
    (tmp_path / cfg.PROJECT_FILE).write_text(
        'room = "T1"\nredact = false\nredact_patterns = ["ACME-[0-9]+"]\n', encoding="utf-8"
    )
    config = cfg.load_config(tmp_path)
    assert (config.redact, config.redact_patterns) == (False, ("ACME-[0-9]+",))
    (tmp_path / cfg.PROJECT_FILE).write_text('room = "T1"\nredact_patterns = ["("]\n', encoding="utf-8")
    with pytest.raises(cfg.ConfigFileError, match="redact_patterns"):
        cfg.load_config(tmp_path)
    (tmp_path / cfg.PROJECT_FILE).write_text('room = "T1"\nredact = "yes"\n', encoding="utf-8")
    with pytest.raises(cfg.ConfigFileError, match="redact must be true or false"):
        cfg.load_config(tmp_path)
    path = cfg.write_project_config(tmp_path, redact=True, redact_patterns=["X-[0-9]+"])
    assert 'redact_patterns = ["X-[0-9]+"]' in path.read_text(encoding="utf-8")
