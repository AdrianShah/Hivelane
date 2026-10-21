"""hivelane.redact: what may leave the machine. Realistic secrets are caught,
ordinary code is left alone, sensitive files are withheld whole, paths are
never rewritten, and a 200 KB diff costs a few milliseconds."""

from __future__ import annotations

import difflib
import time

import pytest

from hivelane import config as cfg
from hivelane.redact import WITHHELD, Redactor, is_secret_name, is_sensitive_file, validate_patterns

R = Redactor()


def unified_diff(file_path: str, old: str, new: str) -> str:
    """The daemon's diff format (hivelane.daemon.unified_diff), without
    importing the daemon."""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=2,
    )
    return "".join(lines).strip()

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
    'TOKEN_HEADER = "X-Hivelane-Token"',
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


def test_a_200kb_diff_is_redacted_quickly() -> None:
    """Typically ~3 ms. The budget is a guard against catastrophic slowdowns
    (regex backtracking costs seconds), not a benchmark: shared CI runners
    are too noisy for a tight one."""
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
    assert best < 0.050, f"redacting 200 KB took {best * 1000:.1f} ms"


def test_config_keys(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
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


# --- review fixes: header bypass, new formats, names by word, sensitive files ----

NEW_POSITIVES = [
    ("DATABASE = 'postgres://app:pg_S3cret!x@db.internal:5432/app'", "url-password", "pg_S3cret!x"),
    ("mongo = mongodb+srv://admin:Hunter2xyz@cluster0.mongodb.net/test", "url-password", "Hunter2xyz"),
    ("curl https://bob:pa55w0rd@example.com/api", "url-password", "pa55w0rd"),
    ("Server=db;Database=app;User Id=sa;Password=Sup3rS3cret;", "connection-password", "Sup3rS3cret"),
    (
        "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=" + "Ab1+" * 21 + "Ab==;EndpointSuffix=core.windows.net",
        "azure-key",
        "Ab1+Ab1+",
    ),
    ("https://acct.blob.core.windows.net/c?sv=2022-11-02&sig=AbCdEf1234567890GhIjKl%3D", "azure-key", "AbCdEf1234567890"),
    ("token = npm_" + "a1B2c3D4e5" * 3 + "a1B2c3", "npm-token", "npm_a1B2"),
    ("//registry.npmjs.org/:_authToken=9f8e7d6c-5b4a-3210-fedc-ba9876543210", "npm-token", "9f8e7d6c"),
    ("password = pypi-AgEIcHlwaS5vcmc" + "x1Y2z3" * 10, "pypi-token", "pypi-AgEIcHlwaS5vcmc"),
    ("HF = hf_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgH", "huggingface-token", "hf_aBcDeF"),
    ("gl = glpat-" + "x1Y2z3W4v5U6t7S8r9Q0", "gitlab-token", "glpat-x1Y2"),
    ("sg = SG." + "a1b2c3d4e5f6g7h8i9j0k1" + "." + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0U1V2W", "sendgrid-key", "SG.a1b2"),
    ("twilio_sid = AC" + "0123456789abcdef" * 2, "twilio-key", "AC0123"),
    ("api = SK" + "fedcba9876543210" * 2, "twilio-key", "SKfedcba"),
    ("stripe_test = sk_test_4eC39HqLyjWDarjtT1zdp7dc", "stripe-key", "sk_test_"),
    ("hook_secret = whsec_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3", "stripe-key", "whsec_"),
    ("slack_app = xapp-1-A0123456789-1234567890-abcdef0123", "slack-token", "xapp-1"),
    ('{"auths": {"ghcr.io": {"auth": "dXNlcjpwYXNzd29yZDEyMw=="}}}', "docker-auth", "dXNlcjpw"),
    ("Authorization: Bearer abcDEF123456ghiJKL789", "auth-header", "abcDEF123456"),
    ("headers = {'Authorization': 'Basic dXNlcjpwYXNzd29yZDEyMw=='}", "auth-header", "dXNlcjpw"),
    ("mysql -u root -pS3cr3tPw db < dump.sql", "mysql-password", "S3cr3tPw"),
    ('const apiKey: string = "a8f5f167f44f4964"', "secret", "a8f5f167f44f4964"),
    ('password?: string | undefined = "c0rrect-h0rse"', "secret", "c0rrect-h0rse"),
    ('db_password = f"c0rrect-h0rse"', "secret", "c0rrect-h0rse"),
    ("SECRET = b'abc123def456'", "secret", "abc123def456"),
    ('passphrase = "correct horse battery staple"', "secret", "correct horse"),
    ("else: password = 's3cret-pw-123'", "secret", "s3cret-pw-123"),
]


@pytest.mark.parametrize(("line", "kind", "secret"), NEW_POSITIVES)
def test_new_secret_formats_are_redacted(line: str, kind: str, secret: str) -> None:
    out = R.text(line, "app.py")
    assert f"[REDACTED:{kind}]" in out, out
    assert secret not in out, out


def test_pgp_and_putty_private_keys() -> None:
    pgp = "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBF0abc\n=AbCd\n-----END PGP PRIVATE KEY BLOCK-----"
    assert R.text(f"x\n{pgp}\ny", "k.txt") == "x\n[REDACTED:private-key]\ny"
    ppk = (
        "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nPublic-Lines: 2\nAAAAC3Nz\nAAAA\n"
        "Private-Lines: 1\nAAAAIFsecretbits\nPrivate-MAC: 0123abcd\n"
    )
    out = R.text(ppk, "notes.txt")
    assert "AAAAIFsecretbits" not in out and "0123abcd" not in out
    assert "Public-Lines: 2" in out


NEW_NEGATIVES = [
    'tokenizer = "bert-base-uncased"',
    'max_tokens: "gpt-4o-mini"',
    'bypass_mode = "permissive"',
    'compass = "north-north-east"',
    'passenger_class = "economy-plus"',
    'tokens_file = "vocab.txt"',
    'secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:x"',
    'password_hash_algo = "argon2id"',
    'token_url = "oauth2/v1/token"',
    'credentials_path = "config/creds.json"',
    'auth_key_id = "kid-2024-01"',
    'pwd_hint = "your first pet1"',
    'tokenType: "refresh_token_2"',
    "login(user, password=password)",
    "f(password=pw);",
    'headers["Authorization"] = "Bearer " + token',
    "this is basic stuff for beginners",
    "https://host.example.com:8443/path@v2",
    'label = "Enter your password here"',
    "ssh://git@github.com:22/org/repo.git",
]


@pytest.mark.parametrize("line", NEW_NEGATIVES)
def test_names_match_by_word_not_substring(line: str) -> None:
    assert R.text(line, "app.py") == line


def test_is_secret_name_words() -> None:
    for name in ("db_password", "apiKey", "API_KEY", "CLIENT_SECRET", "x-auth-token", "privateKey",
                 "stripe.api_key", "SECRET_TOKEN", "pass", "githubToken", "AccessKey"):
        assert is_secret_name(name), name
    for name in ("tokenizer", "max_tokens", "bypass", "token_url", "secret_arn", "keyboard",
                 "password_hash_algo", "credentials_path", "monkey", "key", "passenger"):
        assert not is_secret_name(name), name


def test_header_lookalike_inside_a_hunk_is_still_redacted() -> None:
    """Regression (finding 14): an added `++ b/...` line is `+++ b/...` in the
    diff and used to be exempt from every rule."""
    stripe = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
    diff = unified_diff("q.sql", "select 1;\n", f"select 1;\n++ b/ api_key = \"{stripe}\"\n-- a/ {stripe}\n")
    out = R.diff("q.sql", diff)
    assert stripe not in out, out
    assert out.startswith("--- a/q.sql\n+++ b/q.sql\n")
    removed = unified_diff("q.lua", f"-- a/ token = '{stripe}'\nx\n", "x\n")
    assert stripe not in R.diff("q.lua", removed)


def test_multi_file_git_diff_headers_are_kept() -> None:
    odd = "src/ghp_" + "a" * 36 + "/c.py"
    one = unified_diff(odd, "x = 1\n", "x = 2\n")
    two = unified_diff(odd, "y = 1\n", "y = 'ghp_" + "b" * 36 + "'\n")
    diff = f"diff --git a/{odd} b/{odd}\nindex 1..2 100644\n{one}\ndiff --git a/{odd} b/{odd}\n{two}"
    out = R.diff(odd, diff)
    assert out.count(f"+++ b/{odd}") == 2 and out.count(f"diff --git a/{odd}") == 2
    assert "+y = '[REDACTED:github-token]'" in out


@pytest.mark.parametrize(
    "path",
    [".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".pgpass", "infra/terraform.tfstate",
     "infra/terraform.tfstate.backup", "prod.tfvars", "kubeconfig", "home/.kube/config",
     "home/.docker/config.json", "home/.aws/credentials", "C:\\Users\\me\\.aws\\credentials",
     "keys/server.ppk", ".htpasswd", "gcp/service-account-prod.json", "gcp/deployer-key.json",
     "config/secrets.yml", "deploy/prod.env"],
)
def test_more_sensitive_files_are_withheld(path: str) -> None:
    assert is_sensitive_file(path)
    assert R.diff(path, "+x") == WITHHELD


@pytest.mark.parametrize("path", ["src/config.json", "docs/aws.md", "src/environment.ts", "monkey.json", "app/kube.py"])
def test_ordinary_files_are_not_withheld(path: str) -> None:
    assert not is_sensitive_file(path)


def test_unquoted_secrets_in_prompts_are_redacted() -> None:
    prompt = "use this config:\npassword: hunter2isgreat9\nmode: fast\npassword: required"
    out = R.text(prompt)
    assert "hunter2isgreat9" not in out and "password: required" in out


def test_env_line_rule_is_linear() -> None:
    """Regression (finding 19): `A=` + 20k spaces + `#` took 3.4 s."""
    text = "A=" + " " * 200_000 + "#"
    started = time.perf_counter()
    assert R.text(text, "x/.env") == text
    assert time.perf_counter() - started < 0.2
    assert R.text("TOKEN=abc def123  # note", "x/.env") == "TOKEN=[REDACTED:env-value]  # note"


@pytest.mark.parametrize(
    "unit",
    ["-- a/x\n", "++ b/y\n", "awssecret", "password=", "://u:", "Bearer ", "mysql ", "AC", '"auth": "',
     "a = '", "x: string = ", "sig=", "token "],
)
def test_pathological_200kb_inputs_stay_fast(unit: str) -> None:
    text = unit * (200_000 // len(unit))
    started = time.perf_counter()
    R.text(text, "app.py")
    R.diff("app.py", "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n" + text)
    assert time.perf_counter() - started < 1.0, unit
