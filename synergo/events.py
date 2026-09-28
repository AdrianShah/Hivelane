"""Adapter-agnostic event schema spoken over the daemon's local HTTP port.

Every agent adapter (Claude Code today; Cursor, Codex and friends later)
normalizes its native hook payload into a NormalizedEvent and gets an EventAck
back. Nothing agent-specific belongs in here.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from synergo.protocol import AgentKind, Decision, PeerTouch

EventKind = Literal["intent", "pre_edit", "post_edit"]


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


class EventAck(BaseModel):
    decision: Decision = "allow"
    reason: str = ""
    file_path: Optional[str] = None
    others: list[PeerTouch] = []
