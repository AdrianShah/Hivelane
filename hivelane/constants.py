"""Constants the CLI needs before it knows which command runs.

Kept free of imports beyond the standard library: `hivelane --help` (and every
argument parse) reads these, and importing them from hivelane.daemon used to
pull in fastapi, uvicorn and pydantic, roughly a second on a cold start.
hivelane.daemon and hivelane.protocol re-export them; a test keeps
AGENT_KINDS equal to the protocol's.
"""

from __future__ import annotations

# The relay a single-room `hivelane join` daemon talks to without --server.
DEFAULT_SERVER = "ws://localhost:8000/ws"

# Mirrors hivelane.protocol.AGENT_KINDS (pydantic-free copy).
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
