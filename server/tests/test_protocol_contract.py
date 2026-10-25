"""The wire contract is written down three times, server/models.py (relay),
hivelane/protocol.py (daemon, which must not import the relay package) and
frontend/src/types.ts (dashboard), plus the generated protocol/schema.json.

Hand-mirrored copies drift silently: a field added on one side is dropped or
rejected on the other, and nothing fails until two agents collide in a real
room. These tests make every copy answer to the same models."""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from hivelane import protocol as client

try:
    from server import models as relay
except ImportError:  # Render rootDir=server
    import models as relay  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "protocol" / "generate.py"
TYPES_TS = REPO_ROOT / "frontend" / "src" / "types.ts"

# Defaults that differ on purpose, with the reason. Everything else must match.
INTENTIONAL_DEFAULT_DIFFERENCES = {
    ("JoinMessage", "protocol_version"): (
        "the relay reads an omitted version as 0 (a client too old to declare "
        "one); the client defaults to the version it actually speaks"
    ),
}

# Relay -> client frames that never reach a spectator, so the dashboard need
# not declare them.
NOT_SENT_TO_DASHBOARD = {
    "edit_check_response": "point-to-point reply to a member's edit_check",
    "version_rejected": "spectators are exempt from the version check",
    "file_requested": "sent only to the member(s) holding the requested file",
    "file_released": "sent only to the queued member first in line for the file",
}


def _models(module: ModuleType) -> dict[str, type[BaseModel]]:
    """Every pydantic model defined in (not imported into) module, by name."""
    return {
        name: obj
        for name, obj in vars(module).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj.__module__ == module.__name__
    }


def _messages(module: ModuleType) -> dict[str, type[BaseModel]]:
    """The top-level wire messages: models carrying a `type` discriminator."""
    return {
        name: model
        for name, model in _models(module).items()
        if "type" in model.model_fields
    }


def _type_literal(model: type[BaseModel]) -> str:
    return str(model.model_fields["type"].default)


def _normalize(node: Any, *, names: bool = False) -> Any:
    """Drop what is documentation rather than contract.

    Descriptions come from docstrings, which only the relay's copy carries;
    `required` follows declaration order, which carries no meaning. `names`
    marks a mapping whose keys are property or definition names rather than
    schema keywords, so a field literally called "description" survives.
    """
    if isinstance(node, list):
        return [_normalize(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if names:
            out[key] = _normalize(value)
        elif key == "description" and isinstance(value, str):
            continue
        elif key == "required" and isinstance(value, list):
            out[key] = sorted(value)
        else:
            out[key] = _normalize(value, names=key in ("properties", "$defs"))
    return out


def _comparable_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = _normalize(model.model_json_schema())
    for (model_name, field), _reason in INTENTIONAL_DEFAULT_DIFFERENCES.items():
        if model.__name__ == model_name:
            schema["properties"][field].pop("default", None)
    return schema


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hivelane_protocol_generate", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHARED_MODELS = sorted(set(_models(relay)) & set(_models(client)))


# --- relay <-> daemon ---------------------------------------------------------


def test_protocol_versions_match() -> None:
    assert client.PROTOCOL_VERSION == relay.PROTOCOL_VERSION
    assert client.MIN_SUPPORTED_PROTOCOL == relay.MIN_SUPPORTED_PROTOCOL


def test_both_sides_define_the_same_messages() -> None:
    """A message only one side knows is one the other silently drops."""
    assert sorted(_messages(relay)) == sorted(_messages(client))


def test_every_relay_model_has_a_client_twin() -> None:
    """Nested models too (FileChange, PeerTouch): they are on the wire."""
    assert sorted(_models(relay)) == SHARED_MODELS


@pytest.mark.parametrize("name", SHARED_MODELS)
def test_shared_model_schemas_are_identical(name: str) -> None:
    relay_schema = _comparable_schema(_models(relay)[name])
    client_schema = _comparable_schema(_models(client)[name])

    assert relay_schema == client_schema, (
        f"{name} differs between server/models.py and hivelane/protocol.py; "
        "change both (and run `python protocol/generate.py`)"
    )


def test_intentional_differences_are_still_the_documented_ones() -> None:
    """The allowlist must not outlive its reason, or quietly hide a new one."""
    assert relay.JoinMessage().protocol_version == 0
    assert client.JoinMessage().protocol_version == client.PROTOCOL_VERSION


def test_direction_lists_cover_every_client_message() -> None:
    listed = [*client.CLIENT_TO_RELAY, *client.RELAY_TO_CLIENT]

    assert len(listed) == len(set(listed))
    assert {model.__name__ for model in listed} == set(_messages(client))


def test_type_literals_are_unique() -> None:
    literals = [_type_literal(model) for model in _messages(client).values()]
    assert len(literals) == len(set(literals))


# --- protocol/schema.json -----------------------------------------------------


def test_schema_json_is_up_to_date() -> None:
    generator = _load_generator()
    assert generator.is_current(), (
        "protocol/schema.json is stale: run `python protocol/generate.py`"
    )


def test_schema_json_lists_every_message_by_direction() -> None:
    schema = _load_generator().build_schema()

    assert set(schema["x-client-to-relay"]) == {
        _type_literal(model) for model in client.CLIENT_TO_RELAY
    }
    assert set(schema["x-relay-to-client"]) == {
        _type_literal(model) for model in client.RELAY_TO_CLIENT
    }
    assert schema["x-protocol-version"] == relay.PROTOCOL_VERSION
    for ref in [*schema["x-client-to-relay"].values(), *schema["x-relay-to-client"].values()]:
        assert ref.removeprefix("#/$defs/") in schema["$defs"]


# --- frontend/src/types.ts ----------------------------------------------------

_TS_INTERFACE = re.compile(r"export interface (\w+)\s*\{(.*?)\n\}", re.DOTALL)
_TS_TYPE_LITERAL = re.compile(r"^\s*type\s*:\s*'([a-z_]+)'\s*;", re.MULTILINE)
_TS_FIELD = re.compile(r"^\s*(\w+)\??\s*:", re.MULTILINE)


def _ts_message_fields() -> dict[str, set[str]]:
    """type literal -> field names of the TS interface that declares it.

    Textual on purpose: running tsc from pytest would tie the Python suite to a
    Node toolchain, and the shape checked here is simple and regular.
    """
    source = TYPES_TS.read_text(encoding="utf-8").replace("\r\n", "\n")
    declared: dict[str, set[str]] = {}
    for match in _TS_INTERFACE.finditer(source):
        body = match.group(2)
        literal = _TS_TYPE_LITERAL.search(body)
        if literal:
            declared[literal.group(1)] = set(_TS_FIELD.findall(body))
    return declared


DASHBOARD_MESSAGES = sorted(
    (_type_literal(model), model)
    for model in client.RELAY_TO_CLIENT
    if _type_literal(model) not in NOT_SENT_TO_DASHBOARD
)


def test_dashboard_declares_every_relay_message() -> None:
    declared = _ts_message_fields()
    missing = [literal for literal, _ in DASHBOARD_MESSAGES if literal not in declared]

    assert not missing, f"frontend/src/types.ts has no interface for {missing}"


@pytest.mark.parametrize(
    "literal,model", DASHBOARD_MESSAGES, ids=[lit for lit, _ in DASHBOARD_MESSAGES]
)
def test_dashboard_interface_has_every_field(literal: str, model: type[BaseModel]) -> None:
    fields = _ts_message_fields().get(literal, set())
    missing = sorted(set(model.model_fields) - fields)

    assert not missing, f"types.ts interface for {literal!r} lacks {missing}"
