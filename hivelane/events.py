"""Adapter-agnostic event schema spoken over the daemon's local HTTP port.

Every agent adapter (Claude Code today; Cursor, Codex and friends later)
normalizes its native hook payload into a NormalizedEvent and gets an EventAck
back. Nothing agent-specific belongs in here.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, field_validator

from hivelane.protocol import AgentKind, Decision, PeerTouch

EventKind = Literal["intent", "pre_edit", "post_edit", "plan"]


def _encodable(value: Any) -> Any:
    """value with lone surrogates replaced, in strings nested in lists and
    dicts too. JSON allows a lone surrogate escape (a JS agent that cut an emoji in half
    sends it), but UTF-8 cannot carry it: kept as is, it would make every
    message built from it fail to encode, from hashing to the wire."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
        return value
    if isinstance(value, list):
        return [_encodable(item) for item in value]
    if isinstance(value, dict):
        return {_encodable(k): _encodable(v) for k, v in value.items()}
    return value


class NormalizedEvent(BaseModel):
    kind: EventKind
    agent: AgentKind = "unknown"
    session_id: str
    cwd: str
    prompt: Optional[str] = None
    file_path: Optional[str] = None
    old_content: Optional[str] = None
    new_content: Optional[str] = None
    tool: Optional[str] = None
    # Phase 3, all optional: string-replacement hints for pre_edit
    # ([{"old": ..., "new": ...}]), and a plan event's text plus the candidate
    # files / symbols the adapter pulled out of it.
    edit_hints: Optional[list[dict[str, str]]] = None
    summary: Optional[str] = None
    files: Optional[list[str]] = None
    symbols: Optional[list[str]] = None

    @field_validator(
        "session_id",
        "cwd",
        "prompt",
        "file_path",
        "old_content",
        "new_content",
        "tool",
        "edit_hints",
        "summary",
        "files",
        "symbols",
    )
    @classmethod
    def _no_lone_surrogates(cls, value: Any) -> Any:
        return _encodable(value)


class EventAck(BaseModel):
    decision: Decision = "allow"
    reason: str = ""
    file_path: Optional[str] = None
    others: list[PeerTouch] = []
    # From the relay's edit_check_response: the agent's place in the file's
    # wait queue (1 = next; 0 = not queued) and something to do meanwhile.
    queue_position: int = 0
    suggestion: str = ""
