"""Checking a reply against the schema it was asked to satisfy.

The provider is asked to enforce the schema, and Relay does not take that on trust: what
came back is checked here against the schema that was recorded with the call, before any
of it is read as anything. Only the part of JSON Schema that Relay's own reply schemas
use is understood, and a schema using anything else is refused, not skipped.
"""

from __future__ import annotations

from typing import Any

_UNDERSTOOD = {"type", "properties", "required", "additionalProperties", "items", "enum"}
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


def require_checkable(schema: dict[str, Any]) -> None:
    """Refuse a schema, the whole of it, that asks for anything Relay does not check:
    asked before a call is made on it, so that no reply is paid for that could not then
    be checked."""
    _require_understood(schema)
    for inner in schema.get("properties", {}).values():
        require_checkable(inner)
    if "items" in schema:
        require_checkable(schema["items"])


def _require_understood(schema: dict[str, Any]) -> None:
    beyond = set(schema) - _UNDERSTOOD
    if beyond:
        raise ValueError(f"a reply schema uses {sorted(beyond)}, which Relay does not check")
    if "type" in schema and schema["type"] not in _TYPES:
        raise ValueError(f"a reply schema asks for a {schema['type']}, which Relay does not check")
    if schema.get("additionalProperties", True) not in (True, False):
        raise ValueError(
            "a reply schema gives additionalProperties a schema, which Relay does not check"
        )


def problem_with(value: Any, schema: dict[str, Any], at: str = "the reply") -> str | None:
    """Why the value does not satisfy the schema, or None if it does."""
    _require_understood(schema)
    if "enum" in schema and value not in schema["enum"]:
        return f"{at} is {value!r}, not one of {schema['enum']}"
    wanted = schema.get("type")
    if wanted is not None:
        # A boolean is an integer to Python and is not one to JSON.
        is_a_boolean_where_a_number_is_wanted = isinstance(value, bool) and wanted != "boolean"
        if not isinstance(value, _TYPES[wanted]) or is_a_boolean_where_a_number_is_wanted:
            return f"{at} is not a {wanted}"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                return f"{at} has no {name}"
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    return f"{at} has {name}, which was not asked for"
        for name, inner in properties.items():
            if name in value and (found := problem_with(value[name], inner, f"{name} of {at}")):
                return found
    if isinstance(value, list) and "items" in schema:
        for position, each in enumerate(value, start=1):
            if found := problem_with(each, schema["items"], f"item {position} of {at}"):
                return found
    return None
