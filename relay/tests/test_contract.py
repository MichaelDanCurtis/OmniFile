"""The relay's forward body must match the service's ``TeamsIntentForward`` schema.

Reads ``../service/openapi/control-api.yaml`` (read-only). Skips with a clear message when the
file or the schema isn't there, so the relay can be tested on its own. The validator is a small
hand check of the JSON Schema keywords OpenAPI 3.x request schemas use, to avoid a jsonschema
dependency.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from omnifile_relay.sink import Forward

from .conftest import TENANT, USER, make_activity

SPEC = Path(__file__).resolve().parents[2] / "service" / "openapi" / "control-api.yaml"
SCHEMA_NAME = "TeamsIntentForward"

_TYPES = {
    "string": str,
    "object": dict,
    "array": list,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def _load_schema() -> tuple[dict, dict]:
    if not SPEC.exists():
        pytest.skip(f"{SPEC} not found: contract test needs the service OpenAPI next to relay/")
    yaml = pytest.importorskip("yaml", reason="PyYAML (dev dependency) is needed to read the spec")
    doc = yaml.safe_load(SPEC.read_text())
    schema = (doc.get("components") or {}).get("schemas", {}).get(SCHEMA_NAME)
    if schema is None:
        pytest.skip(f"components.schemas.{SCHEMA_NAME} is not in {SPEC.name} yet")
    return doc, schema


def _resolve(doc: dict, schema: dict) -> dict:
    while "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/"), f"only local refs are supported: {ref}"
        node: object = doc
        for part in ref[2:].split("/"):
            node = node[part]  # type: ignore[index]
        schema = node  # type: ignore[assignment]
    return schema


def _check_format(fmt: str, value: str, path: str) -> list[str]:
    try:
        if fmt == "uuid":
            uuid.UUID(value)
        elif fmt == "date-time":
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or "T" not in value:
                return [f"{path}: date-time needs a 'T' and an offset"]
    except ValueError:
        return [f"{path}: not a valid {fmt}"]
    return []


def validate(doc: dict, schema: dict, value: object, path: str = "$") -> list[str]:
    schema = _resolve(doc, schema)
    errors: list[str] = []
    for sub in schema.get("allOf", []):
        errors += validate(doc, sub, value, path)
    if value is None:
        if schema.get("nullable") or "null" in (schema.get("type") or []):
            return errors
        return [f"{path}: null not allowed"]
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), None)
    if typ is not None:
        expected = _TYPES[typ]
        if not isinstance(value, expected) or (typ == "integer" and isinstance(value, bool)):
            return errors + [f"{path}: expected {typ}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str):
        if "format" in schema:
            errors += _check_format(schema["format"], value, path)
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required '{name}'")
        for name, item in value.items():
            if name in props:
                errors += validate(doc, props[name], item, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{name}'")
    return errors


def test_validator_catches_mismatches():
    doc: dict = {}
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {"a": {"type": "string", "format": "uuid"}},
    }
    assert validate(doc, schema, {"a": str(uuid.uuid4())}) == []
    assert validate(doc, schema, {}) != []
    assert validate(doc, schema, {"a": "nope"}) != []
    assert validate(doc, schema, {"a": str(uuid.uuid4()), "b": 1}) != []


def test_forward_body_matches_service_schema(post, bot_token, intent, sink):
    doc, schema = _load_schema()
    resp = post(make_activity(intent()), bot_token())
    assert resp.json()["statusCode"] == 200
    body = sink.forwards[0].to_json()
    assert validate(doc, schema, body) == []
    # The schema should name every field we send (no silent extras on either side).
    props = set(_resolve(doc, schema).get("properties", {}))
    assert set(body) <= props, f"relay sends fields the schema lacks: {set(body) - props}"
    required = set(_resolve(doc, schema).get("required", []))
    assert required <= set(body), f"schema requires fields the relay omits: {required - set(body)}"


def test_every_action_matches_service_schema():
    doc, schema = _load_schema()
    for act in ("recall", "keep_online", "track"):
        body = Forward(
            intent_token="eyJ.a.b",
            intent_id=str(uuid.uuid4()),
            action=act,
            clicker_object_id=USER,
            tenant_id=TENANT,
            activity_time="2026-10-05T18:07:27.463Z",
            received_at="2026-10-05T18:07:27.500Z",
        ).to_json()
        assert validate(doc, schema, body) == [], act
