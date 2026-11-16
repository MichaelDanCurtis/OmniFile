"""docs/contracts/portal-api.md, service/openapi/portal-api.yaml and the `web`
app must agree.

- The endpoint index (portal-api.md section 10) and the YAML list the same
  operations, with the same auth (S, or S and B).
- The app routes exactly those operations (`web.app.ROUTES`, plus sign-in
  and liveness), and lets bearer tokens in exactly where the YAML does.
- Every operation is exercised by a web test through the spec-checking
  client (tests/web/conftest.py), which validates each request body and
  response against the YAML: so the shapes are checked, not just the paths.
- The YAML's schemas are valid JSON Schema, and its error codes are the
  service's (`api.problems.CODES`) and section 7's.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from omnifile_service.api.problems import CODES
from omnifile_service.web.app import ROUTES, UNWRAPPED

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "contracts" / "portal-api.md"
SPEC = ROOT / "service" / "openapi" / "portal-api.yaml"
WEB_TESTS = Path(__file__).resolve().parent / "web"
#: The Settings and API client tests use the same spec-checking client.
SPEC_CHECKED = [WEB_TESTS, WEB_TESTS.parent / "settings", WEB_TESTS.parent / "apiclients"]
METHODS = ("get", "post", "put", "patch", "delete")


def _spec() -> dict:
    return yaml.safe_load(SPEC.read_text())


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _spec_ops() -> dict[tuple[str, str], dict]:
    return {
        (m.upper(), _norm(p)): op
        for p, item in _spec()["paths"].items()
        for m, op in item.items()
        if m in METHODS
    }


def _doc_index() -> dict[tuple[str, str], str]:
    text = DOC.read_text()
    section = text.split("## 10. Endpoint index", 1)[1].split("\n## ", 1)[0]
    out = {}
    for m in re.finditer(r"^\| `(GET|POST|PUT|PATCH|DELETE) (/[^`]+)` \| ([^|]+) \|", section,
                         re.M):  # fmt: skip
        out[(m[1], _norm(m[2]))] = m[3].strip()
    return out


def test_doc_index_and_spec_list_the_same_operations() -> None:
    doc, spec = _doc_index(), _spec_ops()
    assert doc, "no endpoint index found in portal-api.md"
    assert not set(doc) - set(spec), f"in portal-api.md only: {sorted(set(doc) - set(spec))}"
    assert not set(spec) - set(doc), f"in portal-api.yaml only: {sorted(set(spec) - set(doc))}"


def _bearer(op: dict) -> bool:
    security = op.get("security", _spec().get("security"))
    return any("partnerToken" in s for s in security or [])


def test_doc_and_spec_agree_on_bearer_auth() -> None:
    doc, spec = _doc_index(), _spec_ops()
    for key, auth in doc.items():
        assert ("B" in auth.split(", ")) == _bearer(spec[key]), (key, auth)


def test_the_app_routes_exactly_the_spec() -> None:
    routed = {(r.method, _norm(r.path)) for r in ROUTES} | {(m, p) for m, p in UNWRAPPED}
    spec = set(_spec_ops())
    assert not routed - spec, f"routed but not in the spec: {sorted(routed - spec)}"
    assert not spec - routed, f"in the spec but not routed: {sorted(spec - routed)}"
    assert len(routed) == len(ROUTES) + len(UNWRAPPED), "a route is listed twice"


def test_bearer_tokens_are_let_in_exactly_where_the_spec_says() -> None:
    spec = _spec_ops()
    for r in ROUTES:
        assert r.opts.get("bearer", False) == _bearer(spec[(r.method, _norm(r.path))]), r.path


def test_every_m5_partner_route_is_routed() -> None:
    routed = {_norm(r.path) for r in ROUTES}
    for path in ("/v1/adopt", "/v1/sources", "/v1/locations", "/v1/files", "/v1/usage",
                 "/v1/restores", "/v1/events"):  # fmt: skip
        assert path in routed


def test_every_operation_is_exercised_by_a_spec_checked_test() -> None:
    """The web tests call every operation through `Browser.call` / `App.call`,
    which validate request and response bodies against the YAML."""
    source = "\n".join(p.read_text() for d in SPEC_CHECKED for p in d.glob("test_*.py"))
    missing = []
    for p, item in _spec()["paths"].items():
        for m in item:
            if m not in METHODS or p in ("/auth/login", "/auth/callback", "/healthz"):
                continue
            if f'"{p}"' not in source and f"'{p}'" not in source:
                if not (m == "post" and f'"{p.rsplit("/", 1)[0]}/{{' in source):
                    missing.append(f"{m.upper()} {p}")
    worm = '"/v1/shares/{{share_id}}/worm/{action}"'
    missing = [x for x in missing if not (x.startswith("POST /v1/shares/{share_id}/worm/")
                                          and worm in source)]  # fmt: skip
    assert not missing, f"no spec-checked test calls: {missing}"


def test_schemas_are_valid_json_schema() -> None:
    for name, schema in _spec()["components"]["schemas"].items():
        Draft202012Validator.check_schema(schema)
        assert schema, name


def test_problem_codes_match_the_service_and_section_7() -> None:
    enum = set(_spec()["components"]["schemas"]["Problem"]["properties"]["code"]["enum"])
    assert enum <= set(CODES), f"codes the service doesn't know: {sorted(enum - set(CODES))}"
    text = DOC.read_text()
    section = text.split("## 7. Errors", 1)[1].split("\n## ", 1)[0]
    table = set(re.findall(r"^\| [0-9]{3} \| `([a-z_]+)` \|", section, re.M))
    reused = set(re.findall(r"`([a-z_]+)`", section.split("Codes from the control API", 1)[1]
                            .split("\n", 1)[0]))  # fmt: skip
    assert table <= enum and reused <= enum, (table - enum, reused - enum)
    for code in table:
        status = int(re.search(rf"^\| ([0-9]{{3}}) \| `{code}` \|", section, re.M)[1])
        assert CODES[code][0] == status, (code, status, CODES[code][0])


def test_main_response_schemas_carry_valid_examples() -> None:
    """The SPA's mocks are generated from these (portal-api.md, Status)."""
    spec = _spec()
    schemas = spec["components"]["schemas"]
    names = ("Me", "Session", "Share", "Entry", "FileDetail", "Version", "RecallOptions",
             "Recall", "Pin", "Hold", "VersionRestore", "Download", "PolicyVersion",
             "Proposal", "Dataset", "AuditRecord", "Problem", "GroupRef", "PersonRef")  # fmt: skip
    for name in names:
        examples = schemas[name].get("examples")
        assert examples, f"{name} has no examples"
        validator = Draft202012Validator({**spec, "$ref": f"#/components/schemas/{name}"})
        for ex in examples:
            errors = list(validator.iter_errors(ex))
            assert not errors, (name, errors[0].message, list(errors[0].absolute_path))


OPEN_ENUMS = {
    ("LogicalTier", None),
    ("TierState", "restore_state"),
    ("Entry", "state"),
    ("RecallOption", "priority"),
    ("Recall", "priority"),
    ("RecallCost", "priority"),
    ("TimelineSegment", "account"),
    ("CostItem", "item"),
    ("PriceTable", "backend_kind"),
    ("Dataset", "backend_kind"),
    ("Dataset", "storage_layout"),
    ("Dataset", "migration_state"),
    # The partner API (portal-api.md sections 5.11-5.19).
    ("PartnerEvent", "kind"),
    ("PartnerEvent", "entity"),
    ("Location", "state"),
    ("Location", "mode"),
    ("Migration", "state"),
    ("Migration", "mode"),
    ("Migration", "cutover"),
    ("Migration", "copier_kind"),
    ("RecallBatch", "state"),
    ("RecallBatchItem", "status"),
}

#: Partner routes of the base contract's M5 draft and of line B's partner API
#: (PR #10), all routed on line A.
M5_PARTNER_ROUTES = (
    "/v1/adopt", "/v1/sources", "/v1/locations", "/v1/files", "/v1/usage", "/v1/restores",
    "/v1/events", "/v1/migrations", "/v1/migrations/{}", "/v1/migrations/{}/cutover",
    "/v1/migrations/{}/cancel", "/v1/migrations/{}/retry", "/v1/migrations/{}/runs",
    "/v1/migrations/{}/exclude", "/v1/migrations/{}/tombstones", "/v1/recall-batches",
    "/v1/recall-batches/{}",
)  # fmt: skip


def test_every_ported_partner_route_is_routed() -> None:
    routed = {_norm(r.path) for r in ROUTES}
    assert not set(M5_PARTNER_ROUTES) - routed, sorted(set(M5_PARTNER_ROUTES) - routed)


def test_lifecycle_event_kinds_are_listed_and_open() -> None:
    """The base contract's typed events (section 11) are in the open enum, next
    to the catalog's own kinds, and an unknown future kind still validates."""
    spec = _spec()
    node = spec["components"]["schemas"]["PartnerEvent"]["properties"]["kind"]
    kinds = set(node["x-extensible-enum"])
    assert {"file.upsert", "recall.upsert", "migration.started", "migration.verified",
            "migration.cut_over", "migration.handed_off", "recall.state", "restore.state",
            "proposal.decided"} <= kinds  # fmt: skip
    validator = Draft202012Validator({**spec, "$ref": "#/components/schemas/PartnerEvent"})
    for ex in spec["components"]["schemas"]["PartnerEvent"]["examples"]:
        assert not list(validator.iter_errors({**ex, "kind": "dataset.handed_off"}))


def test_recall_batch_costs_are_per_currency() -> None:
    """Fees in different currencies are never added (portal-api.md 5.19): the
    batch carries one Money total per currency, each item its own fee."""
    schemas = _spec()["components"]["schemas"]
    batch = schemas["RecallBatch"]
    assert "costs" in batch["required"] and "cost" in batch["required"]
    assert batch["properties"]["costs"]["items"] == {"$ref": "#/components/schemas/Money"}
    assert "currency" in schemas["Money"]["required"]
    validator = Draft202012Validator({**_spec(), "$ref": "#/components/schemas/RecallBatch"})
    two = {**batch["examples"][0], "cost": None,
           "costs": [{"amount": "1.00", "currency": "EUR"},
                     {"amount": "2.00", "currency": "USD"}]}  # fmt: skip
    assert not list(validator.iter_errors(two))


def test_inline_backend_takes_only_the_allow_list() -> None:
    """The YAML's inline backend config is the server's allow-list
    (routes_sources.S3_PARTNER_FIELDS); everything else is config_invalid."""
    from omnifile_service.web.routes_sources import DEPLOYMENT_FIELDS, S3_PARTNER_FIELDS

    config = _spec()["components"]["schemas"]["InlineBackend"]["properties"]["config"]
    assert config["additionalProperties"] is False
    assert set(config["properties"]) == S3_PARTNER_FIELDS
    assert not S3_PARTNER_FIELDS & DEPLOYMENT_FIELDS


def test_response_enums_that_will_grow_are_open() -> None:
    """portal-api.md section 1: they may gain values (new backend kinds, layouts,
    migration modes), so unknown values must validate."""
    spec = _spec()
    schemas = spec["components"]["schemas"]
    for name, field in OPEN_ENUMS:
        node = schemas[name] if field is None else schemas[name]["properties"][field]
        assert "enum" not in node and node.get("x-extensible-enum"), (name, field)
    example = schemas["Dataset"]["examples"][0]
    future = {**example, "backend_kind": "netapp_ontap", "storage_layout": "nas_volume",
              "migration_state": "handed_off"}  # fmt: skip
    validator = Draft202012Validator({**spec, "$ref": "#/components/schemas/Dataset"})
    assert not list(validator.iter_errors(future))
    rule = DOC.read_text().split("**Open enums.**", 1)[1].split("\n", 1)[0]
    assert "x-extensible-enum" in rule and "raw value" in rule


_ARN = "arn:aws:s3tables:eu-west-1:123456789012:bucket/aws-s3"
#: (inventory, whether `POST /v1/sources` takes it). Region and backend
#: checks come later, in `create_source`; these are the request's shape.
INVENTORIES = [
    ({"kind": "s3_metadata", "table_bucket_arn": _ARN}, True),
    ({"kind": "s3_metadata", "table_bucket_arn": _ARN, "namespace": "b_acme",
      "table": "inventory", "journal_table": "journal"}, True),
    ({"kind": "s3_inventory", "bucket": "inv", "manifest_key": "a/manifest.json"}, True),
    ({"kind": "listing"}, True),
    ({"kind": "s3_metadata"}, False),
    ({"kind": "s3_metadata", "table_bucket_arn": "arn:aws:s3:::bucket"}, False),
    ({"kind": "s3_metadata", "table_bucket_arn": _ARN, "uri": "https://x/iceberg"}, False),
    ({"kind": "s3_metadata", "table_bucket_arn": _ARN, "namespace": "Bad-Name"}, False),
    ({"kind": "s3_inventory", "bucket": "inv"}, False),
    ({"kind": "s3_inventory", "bucket": "inv", "manifest_key": "k", "table": "t"}, False),
    ({"kind": "listing", "bucket": "b"}, False),
    ({"kind": "parquet", "bucket": "b", "manifest_key": "k"}, False),
    ({}, False),
]  # fmt: skip


def test_source_inventory_variants_match_what_the_endpoint_takes() -> None:
    """Each inventory kind is its own schema (oneOf on `kind`), so a client
    generated from the YAML can't build a request the endpoint refuses, and
    the endpoint takes every request the YAML allows."""
    from omnifile_service.sources.metadata_s3 import check_config
    from omnifile_service.sources.model import SourceError
    from omnifile_service.web.problems import WebError
    from omnifile_service.web.routes_sources import _inventory

    spec = _spec()
    schemas = spec["components"]["schemas"]
    inv = schemas["SourceInventory"]
    assert inv["discriminator"]["propertyName"] == "kind"
    assert set(inv["discriminator"]["mapping"]) == {"s3_metadata", "s3_inventory", "listing"}
    for ref in inv["oneOf"]:
        variant = schemas[ref["$ref"].rsplit("/", 1)[1]]
        assert variant["additionalProperties"] is False and "kind" in variant["required"]
    for name in ("SourceCreate", "Source"):
        assert {"$ref": "#/components/schemas/SourceInventory"} in (
            schemas[name]["properties"]["inventory"]["oneOf"]
        )
    validator = Draft202012Validator({**spec, "$ref": "#/components/schemas/SourceInventory"})
    for body, ok in INVENTORIES:
        assert validator.is_valid(body) is ok, body
        try:
            got = _inventory(body)
            if got is not None and got["kind"] == "s3_metadata":
                check_config(got)
            taken = True
        except (WebError, SourceError):
            taken = False
        assert taken is ok, body
