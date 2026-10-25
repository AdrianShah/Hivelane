"""Configuration loading: development defaults, and refusing to boot when a
staging/production deploy has no CORS allowlist."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

try:
    from server import config
except ImportError:  # Render rootDir=server
    import config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_development_defaults_to_localhost_origins() -> None:
    settings = config.load_settings({})

    assert settings.env == "development"
    assert settings.is_development
    assert not settings.is_production
    assert settings.cors_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    assert settings.cors_origin_regex is None
    assert settings.trust_proxy_header is False


def test_default_rate_limits() -> None:
    settings = config.load_settings({})

    assert settings.room_create_limit == 10
    assert settings.room_create_window_seconds == 3600
    assert settings.ws_attempt_limit == 30
    assert settings.ws_attempt_window_seconds == 60


@pytest.mark.parametrize("env_name", ["staging", "production"])
@pytest.mark.parametrize("cors", [None, "", "  ,  "])
def test_missing_cors_origins_is_fatal_outside_development(
    env_name: str, cors: str | None
) -> None:
    environ = {"HIVELANE_ENV": env_name}
    if cors is not None:
        environ["CORS_ORIGINS"] = cors

    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings(environ)

    assert "CORS_ORIGINS" in str(excinfo.value)


def test_missing_cors_origins_is_fine_in_development() -> None:
    settings = config.load_settings({"HIVELANE_ENV": "development"})

    assert settings.cors_origins == config.DEV_CORS_ORIGINS


def test_production_origins_are_parsed_exactly() -> None:
    settings = config.load_settings(
        {
            "HIVELANE_ENV": "production",
            "CORS_ORIGINS": " https://app.example.com , https://hivelane.example.com ",
        }
    )

    assert settings.is_production
    assert settings.cors_origins == (
        "https://app.example.com",
        "https://hivelane.example.com",
    )
    assert settings.cors_origin_regex is None


def test_origin_regex_is_opt_in() -> None:
    settings = config.load_settings(
        {
            "HIVELANE_ENV": "production",
            "CORS_ORIGINS": "https://app.example.com",
            "CORS_ORIGIN_REGEX": r"https://.*\.example\.com",
        }
    )

    assert settings.cors_origin_regex == r"https://.*\.example\.com"


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({"HIVELANE_ENV": "prod"})

    assert "HIVELANE_ENV" in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("on", True),
        ("0", False),
        ("", False),
    ],
)
def test_trust_proxy_header_parsing(raw: str, expected: bool) -> None:
    settings = config.load_settings({"TRUST_PROXY_HEADER": raw})

    assert settings.trust_proxy_header is expected


def test_non_numeric_limit_is_rejected() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({"ROOM_CREATE_LIMIT": "lots"})

    assert "ROOM_CREATE_LIMIT" in str(excinfo.value)


def test_import_fails_fast_when_production_has_no_cors_origins(
    tmp_path: Path,
) -> None:
    """Importing the module, not just calling load_settings, must abort, so a
    misconfigured deploy never starts serving. Runs outside the repo so no local
    .env can supply CORS_ORIGINS."""
    result = subprocess.run(
        [sys.executable, "-c", "import server.config"],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
            "HIVELANE_ENV": "production",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "CORS_ORIGINS" in result.stderr
