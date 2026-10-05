"""Per-agent hook adapters. Each one translates a native hook payload into
synergo.events.NormalizedEvent-shaped dicts and renders that agent's response
contract; the contract is documented in synergo.adapters.base.

REGISTRY maps agent id -> module name. `load(agent_id)` imports only that one
module (stdlib-only, so it is safe on the hook path) and returns its ADAPTER.
"""

from __future__ import annotations

TYPE_CHECKING = False
if TYPE_CHECKING:
    from synergo.adapters.base import Adapter

REGISTRY: dict[str, str] = {
    "claude-code": "synergo.adapters.claude_code",
    "codex": "synergo.adapters.codex",
    "cursor": "synergo.adapters.cursor",
    "gemini-cli": "synergo.adapters.gemini_cli",
}


def ids() -> tuple[str, ...]:
    return tuple(REGISTRY)


def load(agent_id: str) -> Adapter:
    """The adapter for `agent_id`; KeyError if there is none."""
    module = __import__(REGISTRY[agent_id], fromlist=["ADAPTER"])
    return module.ADAPTER
