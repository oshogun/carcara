"""Strict JSON schemas for each ``carcara run`` stage's structured output.

``validate`` is a deliberately small checker (types, enums, required keys,
``additionalProperties: false``, array items, ``maxItems``, ``maxLength``) so
no jsonschema dependency is needed; it covers exactly the subset used by these
schemas.
"""

from __future__ import annotations

from typing import Any


def _obj(properties: dict[str, Any], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": [k for k in properties if k not in optional],
        "additionalProperties": False,
    }


def _arr(items: dict[str, Any], max_items: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


_STR: dict[str, Any] = {"type": "string"}
_INT: dict[str, Any] = {"type": "integer"}
_BOOL: dict[str, Any] = {"type": "boolean"}

UNVERIFIED_KINDS = ("external", "normative", "untested")
MAX_UNVERIFIED = 20
MAX_UNVERIFIED_TEXT = 200

TRIAGE_RANGES = ("S", "M", "L", "S-M", "M-L", "S-L")
UNCERTAINTY_KINDS = ("external", "normative", "untested", "none")

_UNVERIFIED: dict[str, Any] = {
    "type": "array",
    "maxItems": MAX_UNVERIFIED,
    "items": _obj(
        {
            "id": _STR,
            "kind": {"type": "string", "enum": list(UNVERIFIED_KINDS)},
            "text": {"type": "string", "maxLength": MAX_UNVERIFIED_TEXT},
            "probe": _obj(
                {
                    "name": _STR,
                    "arg": {"type": "string", "maxLength": MAX_UNVERIFIED_TEXT},
                    "expect": {"type": "string", "enum": ["exists", "absent"]},
                }
            ),
        },
        optional=("probe",),
    ),
}

MAX_SCOPE_AREAS = 4

_REVIEW: dict[str, Any] = _obj(
    {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "findings": _arr(
            _obj(
                {
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "major", "minor", "nit"],
                    },
                    "path": _STR,
                    "line": _INT,
                    "issue": _STR,
                    "fix": _STR,
                },
                optional=("line",),
            )
        ),
        "unverified": _UNVERIFIED,
    }
)


def _plan(step: dict[str, Any]) -> dict[str, Any]:
    return _obj(
        {
            "goal": _STR,
            "steps": _arr(step),
            "tests": _arr(_STR),
            "acceptance": _arr(_STR),
            "risks": _arr(_STR),
        }
    )


SCHEMAS: dict[str, dict[str, Any]] = {
    "triage": _obj(
        {
            "size": {"type": "string", "enum": ["S", "M", "L"]},
            "rationale": _STR,
            "triageRange": {"type": "string", "enum": list(TRIAGE_RANGES)},
            "uncertaintyKind": {"type": "string", "enum": list(UNCERTAINTY_KINDS)},
        }
    ),
    "explore": _obj(
        {
            "summary": _STR,
            "findings": _arr(_obj({"path": _STR, "line": _INT, "fact": _STR}, optional=("line",))),
        }
    ),
    "plan": _plan(_obj({"id": _STR, "files": _arr(_STR), "change": _STR})),
    # Ultra runs may order steps with depends_on; non-ultra requests keep "plan".
    "plan-ultra": _plan(
        _obj(
            {"id": _STR, "files": _arr(_STR), "change": _STR, "depends_on": _arr(_STR)},
            optional=("depends_on",),
        )
    ),
    "implement": _obj(
        {
            "changed": _arr(_obj({"path": _STR, "summary": _STR})),
            "verified": _STR,
            "notes": _STR,
            "blocked": _BOOL,
            "user_facing_change": _BOOL,
        }
    ),
    "test": _obj(
        {
            "passed": _BOOL,
            "commands": _arr(_STR),
            "failures": _arr(_obj({"name": _STR, "detail": _STR})),
        }
    ),
    "review": _REVIEW,
    "review-dim": _REVIEW,
    "scope": _obj(
        {
            "areas": _arr(_obj({"id": _STR, "focus": _STR}), max_items=MAX_SCOPE_AREAS),
            "rationale": _STR,
        }
    ),
    "docs": _obj({"changed": _arr(_STR)}),
}


def schema_for(stage: str) -> dict[str, Any]:
    return SCHEMAS[stage]


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
}


def _check(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    kind = schema.get("type")
    if kind is not None:
        ok = isinstance(value, _TYPES[kind])
        if kind == "integer" and isinstance(value, bool):
            ok = False
        if not ok:
            errors.append(f"{path}: expected {kind}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if kind == "string" and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if kind == "object":
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        for key, item in value.items():
            if key in props:
                _check(props[key], item, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected key {key!r}")
    elif kind == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        for i, item in enumerate(value if "items" in schema else ()):
            _check(schema["items"], item, f"{path}[{i}]", errors)


def validate(stage_or_schema: str | dict[str, Any], data: Any) -> list[str]:
    """Return a list of validation errors (empty when ``data`` conforms)."""
    schema = SCHEMAS[stage_or_schema] if isinstance(stage_or_schema, str) else stage_or_schema
    errors: list[str] = []
    _check(schema, data, "$", errors)
    return errors
