"""Adapter-agnostic event schema spoken over the daemon's local HTTP port.

Every agent adapter (Claude Code today; Cursor, Codex and friends later)
normalizes its native hook payload into a NormalizedEvent and gets an EventAck
back. Nothing agent-specific belongs in here.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, field_validator

from hivelane.protocol import AgentKind, Decision, PeerTouch

# delegate: an agent handing work to a subagent (Claude Code's Agent/Task
# tool), carrying the task as prompt. subagent_start / subagent_stop bracket a
# subagent's life. Events fired inside a subagent carry subagent_id (and
# subagent_type), so the daemon can give it a lane of its own.
EventKind = Literal[
    "intent", "pre_edit", "post_edit", "plan", "delegate", "subagent_start", "subagent_stop"
]


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
    # Set when the hook fired inside a subagent: its id and type ("Explore").
    subagent_id: Optional[str] = None
    subagent_type: Optional[str] = None
    # A plan event's steps, [{"content", "status"[, "id"]}] (status pending,
    # in_progress, completed, removed, or "" for "as it was"): the agent's own
    # to-do list, which the daemon mirrors as room tasks. plan_partial: these
    # update single steps (Claude Code's TaskCreate / TaskUpdate) instead of
    # listing the whole plan (TodoWrite, Codex's update_plan).
    steps: Optional[list[dict[str, str]]] = None
    plan_partial: Optional[bool] = None

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
        "subagent_id",
        "subagent_type",
        "steps",
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
    # The relay's decision scope ("task": the file is reserved by a claimed
    # room task, so there is no line to wait in).
    scope: str = ""
