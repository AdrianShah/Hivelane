"""Regenerate protocol/schema.json from the client wire models.

    python protocol/generate.py           rewrite protocol/schema.json
    python protocol/generate.py --check   exit 1 if schema.json is stale

The relay (server/models.py) and the daemon (hivelane/protocol.py) each keep
their own copy of the wire models, because the CLI package must not import the
server package. schema.json is the third, language-neutral copy: what a client
in another language, or a reviewer, reads to know the contract. It is
generated from hivelane/protocol.py, and server/tests/test_protocol_contract.py
fails when it is stale or when the relay's models disagree with it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCHEMA_PATH = HERE / "schema.json"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import BaseModel  # noqa: E402
from pydantic.json_schema import models_json_schema  # noqa: E402

from hivelane import protocol  # noqa: E402

DEFS_REF = "#/$defs/{model}"


def message_type(model: type[BaseModel]) -> str:
    """The literal `type` discriminator a message carries on the wire."""
    return str(model.model_fields["type"].default)


def build_schema() -> dict[str, Any]:
    """One JSON Schema document describing every wire message.

    `oneOf` covers any single frame; the x-* maps say which direction each
    message travels, keyed by its `type` literal.
    """
    messages = [*protocol.CLIENT_TO_RELAY, *protocol.RELAY_TO_CLIENT]
    _, document = models_json_schema(
        [(model, "validation") for model in messages], ref_template=DEFS_REF
    )

    def refs(models: tuple[type[BaseModel], ...]) -> dict[str, str]:
        return {
            message_type(model): DEFS_REF.format(model=model.__name__)
            for model in models
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Hivelane wire protocol",
        "description": (
            "Every WebSocket frame between a Hivelane client and the relay. "
            "Generated from hivelane/protocol.py by protocol/generate.py; "
            "do not edit by hand."
        ),
        "x-protocol-version": protocol.PROTOCOL_VERSION,
        "x-min-supported-protocol": protocol.MIN_SUPPORTED_PROTOCOL,
        "x-client-to-relay": refs(protocol.CLIENT_TO_RELAY),
        "x-relay-to-client": refs(protocol.RELAY_TO_CLIENT),
        "oneOf": [
            {"$ref": DEFS_REF.format(model=model.__name__)} for model in messages
        ],
        "$defs": document.get("$defs", {}),
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def is_current(path: Path = SCHEMA_PATH) -> bool:
    """Compared as data, not text, so line endings and a JSON reformat by an
    editor do not count as staleness."""
    try:
        on_disk = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(on_disk == build_schema())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if schema.json does not match the models",
    )
    args = parser.parse_args(argv)

    if args.check:
        if is_current():
            print(f"{SCHEMA_PATH.relative_to(REPO_ROOT).as_posix()} is up to date")
            return 0
        print(
            f"{SCHEMA_PATH.relative_to(REPO_ROOT).as_posix()} is stale: "
            "run `python protocol/generate.py`",
            file=sys.stderr,
        )
        return 1

    SCHEMA_PATH.write_text(render(build_schema()), encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
