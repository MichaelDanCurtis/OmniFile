#!/usr/bin/env bash
# Offline tests of relay-ranges.sh (decision 49). No Azure call: the service tag list is the fixture in
# testdata/service-tags.json, which has the shape of `az network list-service-tags` output (the REST API's
# ServiceTagsListResult) with documentation addresses, not Microsoft's ranges; the live path runs against a
# fake `az` on PATH that prints it.
#
#   deploy/azure/test-relay-ranges.sh      (check.sh runs it too)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
script="$here/relay-ranges.sh"
fixture="$here/testdata/service-tags.json"
t="$(mktemp -d)"
trap 'rm -rf "$t"' EXIT

pass=0 fail=0
ok() { echo "ok   $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1"; fail=$((fail + 1)); }

# A fake az that records its arguments and prints the fixture; an `az` that must never run fails loudly.
mkdir -p "$t/bin" "$t/nobin"
cat >"$t/bin/az" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$t/az.calls"
[ "\$*" = "network list-service-tags --location westeurope --output json" ] || { echo "unexpected az \$*" >&2; exit 9; }
cat "$fixture"
EOF
cat >"$t/nobin/az" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$t/az.forbidden"
exit 9
EOF
chmod +x "$t/bin/az" "$t/nobin/az"

# variant <name> <python expression on d, the parsed fixture>: writes $t/<name>.json
variant() {
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
exec(sys.argv[2])
json.dump(d, open(sys.argv[3], "w"))
' "$fixture" "$2" "$t/$1.json"
}
bot='[v for v in d["values"] if v["name"] == "AzureBotService"][0]["properties"]'

prefixes() { python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["prefixes"]))' "$1"; }

# 1. The global tag: IPv4 only, de-duplicated, sorted; az never runs with --from-file.
if PATH="$t/nobin:$PATH" "$script" --location westeurope --from-file "$fixture" --out "$t/g.json" 2>"$t/err" \
  && [ "$(prefixes "$t/g.json")" = "192.0.2.0/26 198.51.100.0/24 203.0.113.0/25 203.0.113.128/26" ] \
  && [ ! -e "$t/az.forbidden" ] \
  && grep -q '"tag": "AzureBotService"' "$t/g.json" && grep -q '"ipv6LeftOut": 2' "$t/g.json"; then
  ok "global AzureBotService tag: IPv4 prefixes, de-duplicated and sorted, IPv6 left out, no az call"
else bad "global tag"; cat "$t/err"; fi

# 2. The live path, through the fake az: same list, and exactly one read-only call.
if PATH="$t/bin:$PATH" "$script" --location westeurope --out "$t/live.json" 2>"$t/err" \
  && [ "$(prefixes "$t/live.json")" = "$(prefixes "$t/g.json")" ] \
  && [ "$(cat "$t/az.calls")" = "network list-service-tags --location westeurope --output json" ] \
  && grep -q '"source": "az network list-service-tags --location westeurope"' "$t/live.json"; then
  ok "live path: one az network list-service-tags call, same prefixes"
else bad "live path"; cat "$t/err"; fi

# 3. A failing az fails the script and writes nothing.
if PATH="$t/nobin:$PATH" "$script" --location westeurope --out "$t/none.json" 2>"$t/err"; then
  bad "a failing az was ignored"
elif [ -e "$t/none.json" ]; then bad "a failing az still wrote the file"
else ok "a failing az call fails the script and writes nothing"; fi

# 4. --regional takes only the region's tag.
if "$script" --location westeurope --regional --from-file "$fixture" --out "$t/r.json" 2>"$t/err" \
  && [ "$(prefixes "$t/r.json")" = "198.51.100.0/24" ] && grep -q '"region": "westeurope"' "$t/r.json"; then
  ok "--regional: AzureBotService.WestEurope only"
else bad "--regional"; cat "$t/err"; fi

# expect_fail <label> <stderr pattern> <args...>: non-zero exit, the message, and no output file.
expect_fail() {
  local label="$1" pattern="$2"; shift 2
  rm -f "$t/f.json"
  if "$script" "$@" --out "$t/f.json" 2>"$t/err"; then bad "$label: exited 0"; return; fi
  if [ -e "$t/f.json" ]; then bad "$label: wrote a file"; return; fi
  if ! grep -q "$pattern" "$t/err"; then bad "$label: message"; cat "$t/err"; return; fi
  ok "$label"
}

expect_fail "--regional for a region with no tag fails" "AzureBotService.<northeurope> is missing" \
  --location northeurope --regional --from-file "$fixture"

variant no-tag 'd["values"] = [v for v in d["values"] if not v["name"].startswith("AzureBotService")]'
expect_fail "a list without AzureBotService fails" "AzureBotService is missing" \
  --location westeurope --from-file "$t/no-tag.json"

variant twice 'd["values"].append([v for v in d["values"] if v["name"] == "AzureBotService"][0])'
expect_fail "a tag listed twice fails" "appears more than once" --location westeurope --from-file "$t/twice.json"

variant empty "$bot"'["addressPrefixes"] = []'
expect_fail "an empty tag fails" "has no IPv4 prefix" --location westeurope --from-file "$t/empty.json"

variant v6only "$bot"'["addressPrefixes"] = ["2001:db8:1::/48"]'
expect_fail "an IPv6-only tag fails" "has no IPv4 prefix (1 IPv6)" --location westeurope --from-file "$t/v6only.json"

variant noprefixes "del $bot"'["addressPrefixes"]'
expect_fail "a tag without addressPrefixes fails" "has no addressPrefixes" --location westeurope --from-file "$t/noprefixes.json"

variant hostbits "$bot"'.setdefault("addressPrefixes", []).append("198.51.100.7/24")'
expect_fail "a prefix with host bits fails" "malformed prefix '198.51.100.7/24'" --location westeurope --from-file "$t/hostbits.json"

variant junk "$bot"'["addressPrefixes"].append("not-a-range")'
expect_fail "a malformed prefix fails" "malformed prefix 'not-a-range'" --location westeurope --from-file "$t/junk.json"

variant novalues 'd["values"] = []'
expect_fail "an empty list fails" "has no values" --location westeurope --from-file "$t/novalues.json"

printf 'not json' >"$t/notjson.json"
expect_fail "a file that is not JSON fails" "not a service tag list" --location westeurope --from-file "$t/notjson.json"

expect_fail "more prefixes than --max-rules fails" "4 prefixes are more than --max-rules 3" \
  --location westeurope --from-file "$fixture" --max-rules 3
if "$script" --location westeurope --from-file "$fixture" --max-rules 4 >/dev/null 2>&1; then
  ok "--max-rules equal to the count passes"
else bad "--max-rules 4"; fi

if "$script" --location 'West Europe' --from-file "$fixture" >/dev/null 2>&1; then
  bad "a display name was taken as a region"
else ok "a region must be its name (westeurope), not a display name"; fi

# A rerun after Microsoft changes the tag reports the change and replaces the file; a failed rerun keeps it.
cp "$t/g.json" "$t/run.json"
variant changed "$bot"'["addressPrefixes"] = ["192.0.2.0/26", "198.51.100.0/24", "203.0.113.0/25", "192.0.2.192/26"]'
if "$script" --location westeurope --from-file "$t/changed.json" --out "$t/run.json" 2>"$t/err" \
  && grep -q "changed since the last run: +1 -1" "$t/err" && grep -q "+ 192.0.2.192/26" "$t/err" \
  && grep -q -- "- 203.0.113.128/26" "$t/err" && grep -q '"192.0.2.192/26"' "$t/run.json"; then
  ok "a rerun reports the prefixes added and removed, and replaces the file"
else bad "rerun diff"; cat "$t/err"; fi
before="$(cat "$t/run.json")"
if ! "$script" --location westeurope --from-file "$t/empty.json" --out "$t/run.json" 2>/dev/null \
  && [ "$(cat "$t/run.json")" = "$before" ]; then
  ok "a failed rerun leaves the previous file as it was"
else bad "failed rerun changed the file"; fi

echo "relay-ranges: $pass passed, $fail failed"
[ "$fail" = 0 ]
