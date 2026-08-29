"""Build the strict JSON-Schema subset accepted by Codex structured output.

The authoritative contracts remain unchanged.  This module only materializes a
provider-facing schema: unsupported annotations are removed and every object
property is required, as required by strict structured output.  The returned
artifact is still validated against the authoritative local contract after the
model turn.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .validators import canonical_json


_DROPPED_KEYWORDS = frozenset(
    {"$schema", "$id", "format", "uniqueItems", "default", "examples"}
)
_UNSUPPORTED_COMBINATORS = frozenset(
    {"allOf", "anyOf", "oneOf", "not", "if", "then", "else", "patternProperties"}
)


def _inferred_type(value: Any) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return None


def strict_codex_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic, strict provider schema without weakening local QA."""

    if not isinstance(schema, Mapping):
        raise ValidationError("Codex schema root must be a JSON object")

    def convert(node: Any, path: str) -> Any:
        if isinstance(node, list):
            return [convert(item, f"{path}[]") for item in node]
        if not isinstance(node, Mapping):
            return node
        unsupported = sorted(set(node) & _UNSUPPORTED_COMBINATORS)
        if unsupported:
            raise ValidationError(
                f"Codex schema at {path} uses unsupported combinators: "
                + ", ".join(unsupported)
            )
        result = {
            key: convert(value, f"{path}.{key}")
            for key, value in node.items()
            if key not in _DROPPED_KEYWORDS and key != "required"
        }
        if "type" not in result:
            inferred: str | None = None
            if "const" in result:
                inferred = _inferred_type(result["const"])
            elif isinstance(result.get("enum"), list) and result["enum"]:
                types = {_inferred_type(value) for value in result["enum"]}
                if None not in types and len(types) == 1:
                    inferred = next(iter(types))
            if inferred is not None:
                result["type"] = inferred
        properties = result.get("properties")
        node_type = result.get("type")
        is_object = node_type == "object" or (
            isinstance(node_type, list) and "object" in node_type
        )
        if is_object:
            if not isinstance(properties, Mapping):
                raise ValidationError(f"object schema at {path} must define properties")
            result["additionalProperties"] = False
            result["required"] = list(properties.keys())
        return result

    converted = convert(dict(schema), "$")
    if converted.get("type") != "object":
        raise ValidationError("Codex schema root must have type object")
    return converted


def materialize_codex_schema(
    source_path: str | Path,
    destination_path: str | Path,
) -> Path:
    """Atomically write a provider schema and return its absolute path."""

    source = Path(source_path).expanduser().resolve()
    destination = Path(destination_path).expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load authoritative schema {source}: {exc}") from exc
    converted = strict_codex_schema(raw)
    payload = (canonical_json(converted) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = ["materialize_codex_schema", "strict_codex_schema"]
