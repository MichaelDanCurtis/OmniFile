"""Check portal requests and responses against service/openapi/portal-api.yaml.

Every call the web tests make through `Browser` / `App` (conftest.py) is
checked: the request body against the operation's requestBody schema, and
the response against the schema declared for its status (or `default`, for
errors only). A 2xx status the spec doesn't declare is a failure.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "portal-api.yaml"
#: Every (method, template) a test checked, for the coverage test.
SEEN: set[tuple[str, str]] = set()


@cache
def spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC_PATH.read_text())


def operation(method: str, template: str) -> dict[str, Any]:
    paths = spec()["paths"]
    assert template in paths, f"{template} is not in portal-api.yaml"
    op = paths[template].get(method.lower())
    assert op is not None, f"{method} {template} is not in portal-api.yaml"
    return op


def _deref(obj: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in obj:
        node: Any = spec()
        for part in obj["$ref"].lstrip("#/").split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        obj = node
    return obj


def _errors(schema: dict[str, Any], value: Any) -> list[Any]:
    return sorted(Draft202012Validator({**spec(), **schema}).iter_errors(value), key=str)


def check_request(method: str, template: str, body: Any) -> None:
    op = operation(method, template)
    if "requestBody" not in op or body is None:
        return
    schema = _deref(op["requestBody"])["content"]["application/json"]["schema"]
    errors = _errors(schema, body)
    assert not errors, f"request to {method} {template} violates the spec: {errors[0].message}"


def check_response(method: str, template: str, status: int, content_type: str, raw: bytes) -> None:
    op = operation(method, template)
    SEEN.add((method, template))
    responses = op["responses"]
    key = str(status)
    if key not in responses:
        assert status >= 400, f"{method} {template} returned {status}, not in the spec"
        assert "default" in responses, f"{method} {template} has no default response for {status}"
        key = "default"
    response = _deref(responses[key])
    content = response.get("content")
    if not content:
        return
    media = content_type.split(";")[0].strip()
    assert media in content, f"{method} {template} {status}: {media} is not declared"
    if not media.endswith("json"):
        return  # a download (zip, yaml): its media type is checked, not its bytes
    body = json.loads(raw)
    errors = _errors(content[media]["schema"], body)
    assert not errors, (
        f"{method} {template} {status} response violates the spec: {errors[0].message}"
        f" at {list(errors[0].absolute_path)}"
    )
