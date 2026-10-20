"""Constants the CLI needs before it knows which command runs.

Kept free of imports beyond the standard library: `synergo --help` (and every
argument parse) reads these, and importing them from synergo.daemon used to
pull in fastapi, uvicorn and pydantic, roughly a second on a cold start.
synergo.daemon and synergo.protocol re-export them; a test keeps
AGENT_KINDS equal to the protocol's.
"""

from __future__ import annotations

# The relay a single-room `synergo join` daemon talks to without --server.
DEFAULT_SERVER = "ws://localhost:8000/ws"

# Mirrors synergo.protocol.AGENT_KINDS (pydantic-free copy).
AGENT_KINDS: tuple[str, ...] = (
    "cursor",
    "claude-code",
    "codex",
    "antigravity",
    "gemini-cli",
    "devin",
    "grok",
    "aider",
    "human",
    "unknown",
)
