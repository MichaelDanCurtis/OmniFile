#!/usr/bin/env bash
# Fill your values into the AWS policy templates (docs/guide/README.md, section 17).
#
#   docs/guide/aws/render.sh --out DIR [--edge-ids 1,2]
#
# Reads the values from the environment (omnifile.env and omnifile.state.env, loaded by the guide step)
# and writes, into DIR:
#   service-role-trust.json, service-role-policy.json   the role the service assumes by web identity
#   batch-role-trust.json, batch-role-policy.json       the role S3 Batch Operations runs as
# and, once the trust anchor is recorded (OMNI_AWS_TRUST_ANCHOR_ARN), from deploy/aws/iam/:
#   service-crl-policy.json                             the CRL upload, for this trust anchor only
#   edge-role-policy.json                               the filers' reads, through the S3 endpoint only
#   edge-role-trust.json                                the filers by name (needs --edge-ids)
# A placeholder naming a source bucket becomes one entry per bucket; one naming a filer, one per filer.
# NAME_SUFFIX becomes -OMNI_AWS_NAME_SUFFIX (test runs only), or nothing: a client's files are unchanged.
# It refuses to write a file that still holds a placeholder. It calls nothing.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
OUT='' EDGES=''
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift ;;
    --edge-ids) EDGES=$2; shift ;;
    *) echo "usage: render.sh --out DIR [--edge-ids 1,2]" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$OUT" ] || { echo "render.sh: --out is required" >&2; exit 2; }
if [ -n "${OMNI_AWS_NAME_SUFFIX:-}" ] && ! [[ $OMNI_AWS_NAME_SUFFIX =~ ^[a-z0-9][a-z0-9-]{3,39}$ ]]; then
  echo "render.sh: OMNI_AWS_NAME_SUFFIX must be empty or a run id (4 to 40 lowercase letters, digits or hyphens)" >&2
  exit 2
fi
mkdir -p "$OUT"

templates=("$HERE/service-role-trust.json" "$HERE/service-role-policy.json" "$HERE/batch-role-trust.json"
  "$HERE/batch-role-policy.json")
if [ -n "${OMNI_AWS_TRUST_ANCHOR_ARN:-}" ]; then
  templates+=("$REPO/deploy/aws/iam/service-crl-policy.json" "$REPO/deploy/aws/iam/edge-role-policy.json")
  [ -n "$EDGES" ] && templates+=("$REPO/deploy/aws/iam/edge-role-trust.json")
fi

python3 -I - "$OUT" "$EDGES" "${templates[@]}" <<'PY'
import json, os, re, sys

out, edges, templates = sys.argv[1], sys.argv[2], sys.argv[3:]
env = os.environ
ta = env.get("OMNI_AWS_TRUST_ANCHOR_ARN", "")
suffix = "-" + env["OMNI_AWS_NAME_SUFFIX"] if env.get("OMNI_AWS_NAME_SUFFIX") else ""
scalar = {
    # longest first, so no name is replaced inside another
    "S3_INTERFACE_ENDPOINT_ID": env.get("OMNI_AWS_VPCE_S3", ""),
    "WORKER_PRINCIPAL_ID": env.get("OMNI_WORKER_PRINCIPAL_ID", ""),
    "API_PRINCIPAL_ID": env.get("OMNI_API_PRINCIPAL_ID", ""),
    "TRUST_ANCHOR_ID": ta.rsplit("/", 1)[-1] if ta else "",
    "ACCOUNT_ID": env.get("OMNI_AWS_ACCOUNT_ID", ""),
    "OPS_BUCKET": env.get("OMNI_AWS_OPS_BUCKET", ""),
    "TENANT_ID": env.get("OMNI_TENANT_ID", ""),
    "AUDIENCE": env.get("OMNI_AWS_AUDIENCE", ""),
    "REGION": env.get("OMNI_AWS_REGION", ""),
}
buckets = [b for b in env.get("OMNI_AWS_SOURCE_BUCKETS", "").split(",") if b]
edge_ids = [e for e in edges.split(",") if e]
missing = [k for k, v in scalar.items() if not v]
if not buckets:
    missing.append("SOURCE_BUCKET (OMNI_AWS_SOURCE_BUCKETS)")
LEFT = re.compile("|".join(list(scalar) + ["SOURCE_BUCKET", "SOURCE_PREFIX", "EDGE_ID"]))


def fill(s):
    s = s.replace("NAME_SUFFIX", suffix)  # empty for a client
    for k, v in scalar.items():
        if v:  # an unset value keeps its placeholder, which the check below refuses
            s = s.replace(k, v)
    return s


def expand(s):
    """One string becomes the list of strings it stands for."""
    if "SOURCE_BUCKET" in s:
        return [fill(s.replace("SOURCE_BUCKET", b).replace("SOURCE_PREFIX", "")) for b in buckets]
    if "EDGE_ID" in s:
        return [fill(s.replace("EDGE_ID", e)) for e in edge_ids]
    return [fill(s)]


def walk(node):
    if isinstance(node, dict):
        return {fill(k): walk(v) for k, v in node.items()}
    if isinstance(node, list):
        items = []
        for v in node:
            items.extend(expand(v) if isinstance(v, str) else [walk(v)])
        return items
    if isinstance(node, str):
        many = expand(node)
        return many[0] if len(many) == 1 else many
    return node


bad = False
for path in templates:
    name = os.path.basename(path)
    with open(path, encoding="utf-8") as fh:
        doc = walk(json.load(fh))
    text = json.dumps(doc, indent=2) + "\n"
    left = sorted(set(LEFT.findall(text)))
    if left:
        print(f"render.sh: {name} still names {', '.join(left)}: set them first ({', '.join(missing)})",
              file=sys.stderr)
        bad = True
        continue
    with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"  {out}/{name}")
sys.exit(1 if bad else 0)
PY
