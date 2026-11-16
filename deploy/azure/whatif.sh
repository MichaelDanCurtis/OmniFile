#!/usr/bin/env bash
# Preview a deployment with ARM what-if, summarise it, and give the digest a deploy must be handed.
#
#   deploy/azure/whatif.sh -g RESOURCE_GROUP -p PARAMS.bicepparam [-f TEMPLATE.bicep] [-n NAME]
#                          [--from-file WHATIF.json] [--json-out FILE]
#   deploy/azure/whatif.sh -g RESOURCE_GROUP -p PARAMS.bicepparam [-f ...] [-n NAME]
#                          --deploy --approve sha256:HEX
#
# Arguments:
#   -g, --resource-group RG   the resource group (it must exist)
#   -p, --parameters FILE     a .bicepparam file (it may read environment variables)
#   -f, --template FILE       default main.bicep next to this script
#   -n, --name NAME           the deployment's name (default omnifile): read its outputs with
#                             az deployment group show -g RG -n NAME --query properties.outputs
#   --from-file JSON          summarise a saved what-if result instead of calling az (offline)
#   --json-out FILE           also write the summary as JSON: {digest, status, counts, changes}
#   --deploy --approve DIGEST run what-if again and deploy only if its digest is DIGEST
#
# It compiles the template and the parameters with the Bicep CLI first, and passes the compiled
# JSON to az, so what is previewed, and deployed, is exactly what the digest covers. The plan
# step is read-only: `az deployment group what-if --no-pretty-print`. It never signs in.
#
# Output (stdout): the change counts, one line per created, modified, deleted or unsupported
# resource, warnings for deletions and for changes to WORM storage or locks, and last
#   template: sha256:<64 hex>      (the compiled template, as the digest counts it)
#   parameters: sha256:<64 hex>    (the compiled parameters, likewise)
#   digest: sha256:<64 hex>
# The digest is SHA-256 over the compiled template, the compiled parameters and the changes
# (sorted; fields Azure fills in, such as etag and timestamps, left out). The same inputs give the
# same digest, so it can be approved on one run and checked on the next; the parameters' values
# count, so the environment a .bicepparam reads (run id, expiry) must be the same for both.
#
# Exit: 0 done; 1 what-if or the deployment failed; 2 usage; 3 --deploy: the digest differs from
# the approved one (nothing deployed).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
rg="" params="" template="$here/main.bicep" name="omnifile" from_file="" json_out="" deploy=0 approve=""

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    -g|--resource-group) [ $# -ge 2 ] || usage; rg="$2"; shift 2 ;;
    -p|--parameters) [ $# -ge 2 ] || usage; params="$2"; shift 2 ;;
    -f|--template) [ $# -ge 2 ] || usage; template="$2"; shift 2 ;;
    -n|--name) [ $# -ge 2 ] || usage; name="$2"; shift 2 ;;
    --from-file) [ $# -ge 2 ] || usage; from_file="$2"; shift 2 ;;
    --json-out) [ $# -ge 2 ] || usage; json_out="$2"; shift 2 ;;
    --deploy) deploy=1; shift ;;
    --approve) [ $# -ge 2 ] || usage; approve="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "whatif.sh: unknown argument $1" >&2; usage ;;
  esac
done
[ -n "$rg" ] && [ -n "$params" ] || usage
[[ "$name" =~ ^[-A-Za-z0-9_.()]{1,64}$ ]] || { echo "whatif.sh: bad deployment name $name" >&2; exit 2; }
if [ "$deploy" = 1 ]; then
  [[ "$approve" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "whatif.sh: --deploy needs --approve sha256:<64 hex>" >&2; exit 2; }
  [ -z "$from_file" ] || { echo "whatif.sh: --deploy runs what-if itself; --from-file can't be used" >&2; exit 2; }
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
if command -v bicep >/dev/null 2>&1; then
  bicep build "$template" --outfile "$work/template.json" >"$work/bicep.log" 2>&1 || { cat "$work/bicep.log" >&2; exit 1; }
  bicep build-params "$params" --outfile "$work/parameters.json" >"$work/bicep.log" 2>&1 || { cat "$work/bicep.log" >&2; exit 1; }
else
  az bicep build --file "$template" --outfile "$work/template.json" >/dev/null
  az bicep build-params --file "$params" --outfile "$work/parameters.json" >/dev/null
fi

if [ -n "$from_file" ]; then
  cp "$from_file" "$work/whatif.json"
else
  az deployment group what-if --resource-group "$rg" --name "$name" \
    --template-file "$work/template.json" --parameters "@$work/parameters.json" \
    --no-pretty-print --output json >"$work/whatif.json" || { echo "whatif.sh: what-if failed" >&2; exit 1; }
fi

python3 - "$work/whatif.json" "$work/template.json" "$work/parameters.json" "$rg" "$name" \
  "${json_out:-}" >"$work/summary" <<'PY' || { cat "$work/summary"; exit 1; }
import hashlib
import json
import sys

whatif_path, template_path, params_path, rg, name, json_out = sys.argv[1:7]
doc = json.load(open(whatif_path))
# The REST shape keeps the changes under properties; az flattens them to the top level.
props = doc.get("properties") or {}
changes = doc.get("changes", props.get("changes")) or []
status = doc.get("status", "")
error = doc.get("error") or props.get("error")
if error or status not in ("Succeeded", ""):
    print(f"what-if {status or 'failed'}: {json.dumps(error)[:2000]}")
    sys.exit(1)

#: Fields Azure fills in or changes on its own, never chosen by the template.
VOLATILE = {"etag", "systemData", "provisioningState", "createdTime", "changedTime",
            "creationTime", "lastModifiedTime", "lastModifiedAt", "createdAt", "lastModified"}


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if k not in VOLATILE}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def canon(c):
    t = c.get("changeType", "")
    item = {"changeType": t, "resourceId": c.get("resourceId") or json.dumps(c.get("identifiers"))}
    if t in ("Create", "Deploy"):
        item["after"] = clean(c.get("after"))
    elif t == "Modify":
        item["delta"] = clean(c.get("delta"))
    elif t == "Unsupported":
        item["unsupportedReason"] = c.get("unsupportedReason")
    return item


def file_sha(path):
    return hashlib.sha256(json.dumps(json.load(open(path)), sort_keys=True).encode()).hexdigest()


items = sorted((canon(c) for c in changes), key=lambda i: (i["resourceId"], i["changeType"]))
material = {"v": 1, "template": file_sha(template_path), "parameters": file_sha(params_path),
            "changes": items}
digest = "sha256:" + hashlib.sha256(
    json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

order = ["Create", "Modify", "Delete", "Deploy", "NoChange", "Ignore", "Unsupported"]
counts = {t: sum(1 for c in changes if c.get("changeType") == t) for t in order}
mark = {"Create": "+", "Modify": "~", "Delete": "-", "Deploy": "=", "Unsupported": "?"}


def kind_name(rid):
    parts = rid.split("/providers/")[-1].split("/")
    return "/".join(parts[0:2] + parts[3:-1:2]), parts[-1]


print(f"what-if: resource group {rg}, deployment {name}")
print("changes: " + ", ".join(f"{t} {counts[t]}" for t in order))
warnings = []
for c in sorted(changes, key=lambda c: (order.index(c.get("changeType", "Unsupported"))
                                        if c.get("changeType") in order else 9, c.get("resourceId", ""))):
    t = c.get("changeType")
    if t not in mark:
        continue
    rid = c.get("resourceId", "")
    kind, res = kind_name(rid)
    line = f"  {mark[t]} {t:<11} {kind} {res}"
    if t == "Modify":
        paths = [d.get("path", "") for d in c.get("delta") or []]
        line += f"  ({', '.join(paths[:6])}{', ...' if len(paths) > 6 else ''})"
    if t == "Unsupported":
        line += f"  ({c.get('unsupportedReason', '')})"
    print(line)
    body = json.dumps(c.get("after") or c.get("before") or {})
    if t == "Delete":
        warnings.append(f"DELETE {kind} {res}")
    if t in ("Modify", "Delete") and (
        "immutableStorageWithVersioning" in body or "immutabilityPolic" in rid
        or kind == "Microsoft.Authorization/locks"
    ):
        warnings.append(f"WORM or lock change: {t} {kind} {res}")
for w in warnings:
    print(f"!! {w}")
if json_out:
    with open(json_out, "w") as fh:
        json.dump({"digest": digest, "status": status or "Succeeded", "counts": counts,
                   "changes": items, "warnings": warnings}, fh, indent=2)
# What the digest covers, by file: the real-cloud harness's az guard binds a deploy to them.
print(f"template: sha256:{material['template']}")
print(f"parameters: sha256:{material['parameters']}")
print(f"digest: {digest}")
PY
cat "$work/summary"
got="$(sed -n 's/^digest: //p' "$work/summary" | tail -1)"

if [ "$deploy" = 1 ]; then
  if [ "$got" != "$approve" ]; then
    echo "whatif.sh: the what-if digest is $got, not the approved $approve: nothing deployed" >&2
    exit 3
  fi
  echo "deploying $name to $rg (digest approved)"
  # OMNIFILE_DEPLOY_APPROVED_DIGEST: the digest just checked, for the real-cloud harness's az
  # guard (tests/real/harness/azguard.py), which lets a deploy through only with it. Unused
  # anywhere else.
  OMNIFILE_DEPLOY_APPROVED_DIGEST="$got" az deployment group create --resource-group "$rg" --name "$name" \
    --template-file "$work/template.json" --parameters "@$work/parameters.json" \
    --output json >"$work/deployment.json" || { echo "whatif.sh: the deployment failed" >&2; exit 1; }
  python3 - "$work/deployment.json" <<'PY'
import json
import sys

d = json.load(open(sys.argv[1]))
p = d.get("properties", d)
print("deployment:", p.get("provisioningState"))
for k, v in sorted((p.get("outputs") or {}).items()):
    print(f"  {k} = {json.dumps(v.get('value'))[:200]}")
PY
fi
