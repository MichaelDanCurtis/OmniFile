#!/usr/bin/env bash
# The relay's ingress allow-list, from Azure's published AzureBotService service tag, taken at deploy time
# (decision 49; threat model T1). Nobody types the ranges.
#
#   deploy/azure/relay-ranges.sh --location westeurope --out deploy/azure/relay-allowed-cidrs.json
#   az deployment group what-if -g <rg> -f deploy/azure/main.bicep -p deploy/azure/main.bicepparam
#
# The parameters file reads the list with
#   param relayAllowedCidrs = loadJsonContent('relay-allowed-cidrs.json', '$.prefixes')
# (main.example.bicepparam does), so relayAllowedCidrs is exactly what this script wrote.
#
# It runs `az network list-service-tags --location <region>`, a read-only call that needs an az session the
# operator has already signed in; this script never signs in. The list covers every region of the cloud
# (--location picks the cloud and the API version, not a filter). It takes the IPv4 prefixes of:
#   - by default, the global `AzureBotService` tag: Bot Connector's calls are not tied to the relay's region;
#   - with --regional, only the tag of this region (`AzureBotService.<Region>`). Use it only after the
#     real-tenant check shows every Teams call comes from that region's ranges (real-tenant-checks.md).
# IPv6 prefixes are left out: Container Apps ingress IP restrictions take IPv4 only.
#
# It fails, writing nothing, when the tag is missing, has no IPv4 prefix, holds a malformed prefix, or (with
# --max-rules N) has more prefixes than N. Run it again before each deployment: when Microsoft changes the tag,
# it prints the prefixes added and removed since the file it replaces.
#
# Offline (the tests, check.sh): --from-file <recorded list-service-tags output> instead of calling az.
set -euo pipefail

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' >&2
  echo "usage: relay-ranges.sh --location <region> [--regional] [--out FILE] [--from-file FILE] [--max-rules N]" >&2
  exit 2
}

location="" out="" from_file="" regional=0 max_rules=0
need() { [ $# -ge 2 ] || { echo "relay-ranges.sh: $1 needs a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --location|--out|--from-file|--max-rules) need "$@" ;;
  esac
  case "$1" in
    --location) location="${2:-}"; shift 2 ;;
    --out) out="${2:-}"; shift 2 ;;
    --from-file) from_file="${2:-}"; shift 2 ;;
    --regional) regional=1; shift ;;
    --max-rules) max_rules="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "relay-ranges.sh: unknown argument: $1" >&2; usage ;;
  esac
done
[ -n "$location" ] || { echo "relay-ranges.sh: --location is required" >&2; usage; }
[[ "$location" =~ ^[a-z0-9]+$ ]] || { echo "relay-ranges.sh: --location must be an Azure region name like westeurope" >&2; exit 2; }
[[ "$max_rules" =~ ^[0-9]+$ ]] || { echo "relay-ranges.sh: --max-rules must be a number" >&2; exit 2; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ -n "$from_file" ]; then
  [ -r "$from_file" ] || { echo "relay-ranges.sh: cannot read $from_file" >&2; exit 1; }
  tags="$from_file"
  source_desc="recorded: $(basename "$from_file")"
else
  command -v az >/dev/null 2>&1 || { echo "relay-ranges.sh: needs the Azure CLI (az), signed in" >&2; exit 1; }
  tags="$tmp/service-tags.json"
  if ! az network list-service-tags --location "$location" --output json >"$tags" 2>"$tmp/az.err"; then
    echo "relay-ranges.sh: az network list-service-tags failed:" >&2
    sed 's/^/    /' "$tmp/az.err" >&2
    exit 1
  fi
  source_desc="az network list-service-tags --location $location"
fi

python3 - "$tags" "$location" "$regional" "$max_rules" "$source_desc" "$out" "$tmp/result.json" <<'PY'
import ipaddress
import json
import sys
from datetime import datetime, timezone

tags_path, location, regional, max_rules, source_desc, previous_path, result_path = sys.argv[1:]
regional, max_rules = regional == "1", int(max_rules)


def die(msg: str) -> None:
    print(f"relay-ranges.sh: {msg}", file=sys.stderr)
    sys.exit(1)


try:
    with open(tags_path) as f:
        data = json.load(f)
except (OSError, ValueError) as e:
    die(f"not a service tag list ({tags_path}): {e}")
values = data.get("values") if isinstance(data, dict) else None
if not isinstance(values, list) or not values:
    die("the service tag list has no values (is this `az network list-service-tags` output?)")


def props(v: dict) -> dict:
    p = v.get("properties")
    return p if isinstance(p, dict) else {}


if regional:
    found = [
        v for v in values
        if isinstance(v, dict)
        and str(v.get("name", "")).startswith("AzureBotService.")
        and props(v).get("region") == location
    ]
    want = f"AzureBotService.<{location}>"
else:
    found = [v for v in values if isinstance(v, dict) and v.get("name") == "AzureBotService"]
    want = "AzureBotService"
if len(found) != 1:
    die(f"service tag {want} {'is missing' if not found else 'appears more than once'} in the list "
        f"(cloud {data.get('cloud', '?')}, change {data.get('changeNumber', '?')}); refusing to guess the relay's ranges")
tag = found[0]
raw = props(tag).get("addressPrefixes")
if not isinstance(raw, list):
    die(f"service tag {tag.get('name')} has no addressPrefixes")

v4, v6 = set(), 0
for p in raw:
    try:
        net = ipaddress.ip_network(str(p), strict=True)
    except ValueError as e:
        die(f"service tag {tag.get('name')} holds a malformed prefix {p!r}: {e}")
    if net.version == 6:
        v6 += 1
    else:
        v4.add(net)
if not v4:
    die(f"service tag {tag.get('name')} has no IPv4 prefix ({v6} IPv6); the relay would admit nobody")
prefixes = [str(n) for n in sorted(v4)]
if max_rules and len(prefixes) > max_rules:
    die(f"{len(prefixes)} prefixes are more than --max-rules {max_rules} (the ingress rule limit)")

result = {
    "comment": "Written by deploy/azure/relay-ranges.sh; do not edit. main.bicepparam reads $.prefixes as relayAllowedCidrs.",
    "source": source_desc,
    "cloud": data.get("cloud", ""),
    "listChangeNumber": data.get("changeNumber", ""),
    "tag": tag.get("name"),
    "tagChangeNumber": props(tag).get("changeNumber", ""),
    "region": props(tag).get("region", ""),
    "taken": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "ipv6LeftOut": v6,
    "prefixes": prefixes,
}
with open(result_path, "w") as f:
    json.dump(result, f, indent=2)
    f.write("\n")

msg = (f"relay-ranges.sh: {tag.get('name')} (change {result['tagChangeNumber']}, cloud {result['cloud']}): "
       f"{len(prefixes)} IPv4 prefixes, {v6} IPv6 left out")
print(msg, file=sys.stderr)
if previous_path:
    try:
        with open(previous_path) as f:
            before = set(json.load(f).get("prefixes", []))
    except (OSError, ValueError, AttributeError):
        before = None
    if before is not None:
        added, removed = sorted(set(prefixes) - before), sorted(before - set(prefixes))
        if added or removed:
            print(f"relay-ranges.sh: changed since the last run: +{len(added)} -{len(removed)}", file=sys.stderr)
            for p in added:
                print(f"    + {p}", file=sys.stderr)
            for p in removed:
                print(f"    - {p}", file=sys.stderr)
        else:
            print("relay-ranges.sh: unchanged since the last run", file=sys.stderr)
PY

if [ -n "$out" ]; then
  # Replace the file only once the new list is complete.
  cp "$tmp/result.json" "$out.tmp.$$"
  mv "$out.tmp.$$" "$out"
  echo "relay-ranges.sh: wrote $out" >&2
else
  cat "$tmp/result.json"
fi
