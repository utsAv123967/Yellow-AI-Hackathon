"""Stdlib validator for the subset of JSON Schema draft-07 that
schema/loop-report.schema.json actually uses.

The kit ships no validator, and `jsonschema` is not installed on every machine
(it is not on the one this was written on) — a pipeline that imports it can
crash on the sealed run before writing a single byte. So this has no
dependencies at all: type, enum, const, minimum/maximum, required, properties,
additionalProperties (schema form), items, minItems, and if/then.
"""
from __future__ import annotations

import json
from typing import List

_PY_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value, type_name: str) -> bool:
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _PY_TYPES[type_name])


def _check(value, schema: dict, path: str, errors: List[str]) -> None:
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            errors.append("%s: is %s, expected %s" % (path, type(value).__name__, "/".join(types)))
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: %r is not one of %s" % (path, value, schema["enum"]))
    if "const" in schema and value != schema["const"]:
        errors.append("%s: expected constant %r" % (path, schema["const"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: %r is below minimum %r" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: %r is above maximum %r" % (path, value, schema["maximum"]))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append("%s: missing required field '%s'" % (path, key))
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, "%s.%s" % (path, key), errors)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key in value:
                if key not in props:
                    _check(value[key], extra, "%s.%s" % (path, key), errors)
        if "if" in schema:
            probe: List[str] = []
            _check(value, schema["if"], path, probe)
            if not probe and "then" in schema:
                _check(value, schema["then"], path, errors)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s: has %d item(s), needs at least %d" % (path, len(value), schema["minItems"]))
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], "%s[%d]" % (path, i), errors)


def validate(instance: dict, schema: dict) -> List[str]:
    """Return every violation found (empty list = valid)."""
    errors: List[str] = []
    _check(instance, schema, "$", errors)
    return errors


def validate_file(instance: dict, schema_path: str) -> List[str]:
    with open(schema_path, encoding="utf-8") as f:
        return validate(instance, json.load(f))
