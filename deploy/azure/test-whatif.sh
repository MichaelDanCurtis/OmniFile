#!/usr/bin/env bash
# Offline tests of whatif.sh: the real Bicep CLI compiles main.bicep and test.bicepparam; a fake
# `az` first on PATH answers what-if with testdata/whatif.json (the REST API's documented response
# shape, with documentation values) or a variant made from it, and records its calls. The real az
# is never run. Prints one line per check; exit 1 on a failure.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
unset AZURE_CONFIG_DIR AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID || true
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

# The template and parameters, as check.sh builds them: a copy with the relay's list from the
# fixture and the image lock beside it.
src="$work/src"
mkdir -p "$src" "$work/images"
cp -R "$here/main.bicep" "$here/bicepconfig.json" "$here/modules" "$here/test.bicepparam" "$src/"
cp "$here/../images/images.lock.json" "$work/images/"
"$here/relay-ranges.sh" --location westeurope --from-file "$here/testdata/service-tags.json" \
  --out "$src/relay-allowed-cidrs.json" >/dev/null 2>&1
export OMNIFILE_RUN_ID=check OMNIFILE_RUN_EXPIRES=2026-11-05T00:00:00Z \
  OMNIFILE_TEST_PG_ADMIN_OID=00000000-0000-0000-0000-0000000000a1 OMNIFILE_TEST_PG_ADMIN_NAME=OmniFile-Test-DB \
  OMNIFILE_TEST_REGISTRY=registry.example.test OMNIFILE_TEST_ENROL_CIDRS=10.20.0.0/16 \
  OMNIFILE_TEST_WEB_CIDRS=10.30.0.0/16

mkdir -p "$work/bin"
cat >"$work/bin/az" <<'FAKE'
#!/usr/bin/env bash
# The fake az: what-if prints $FAKE_WHATIF, create prints a deployment; nothing else is allowed.
printf '%s\n' "$*" | sed -E 's#/[^ ]*/(template|parameters)\.json#<tmp>/\1.json#g' >>"$FAKE_AZ_LOG"
case "$1 $2 $3" in
  "deployment group what-if") cat "$FAKE_WHATIF" ;;
  "deployment group create")
    echo '{"properties": {"provisioningState": "Succeeded", "outputs": {"apiUrl": {"type": "String", "value": "https://oftest-api.example:8443"}}}}' ;;
  *) echo "fake az: unexpected call: $*" >&2; exit 99 ;;
esac
FAKE
chmod +x "$work/bin/az"
export PATH="$work/bin:$PATH" FAKE_AZ_LOG="$work/az.log" FAKE_WHATIF="$here/testdata/whatif.json"
[ "$(command -v az)" = "$work/bin/az" ] || { echo "FAIL the fake az is not first on PATH"; exit 1; }

plan() { "$here/whatif.sh" -g rg-omnifile-test -f "$src/main.bicep" -p "$src/test.bicepparam" "$@"; }
digest_of() { sed -n 's/^digest: //p' "$1" | tail -1; }

: >"$FAKE_AZ_LOG"
plan >"$work/one"
d1="$(digest_of "$work/one")"
[[ "$d1" =~ ^sha256:[0-9a-f]{64}$ ]] && [ "$(tail -1 "$work/one")" = "digest: $d1" ] \
  && ok "the last line is the digest ($d1)" || bad "no digest line: $(tail -3 "$work/one")"
grep -q "^changes: Create 2, Modify 1, Delete 0, Deploy 0, NoChange 1, Ignore 0, Unsupported 0$" "$work/one" \
  && ok "the change counts" || bad "counts: $(grep changes "$work/one")"
grep -q "~ Modify      Microsoft.ManagedIdentity/userAssignedIdentities id-oftest-api  (tags.omnifile-run)" "$work/one" \
  && ok "a modified resource names the paths that change" || bad "$(grep Modify "$work/one")"
want="deployment group what-if --resource-group rg-omnifile-test --name omnifile --template-file <tmp>/template.json --parameters @<tmp>/parameters.json --no-pretty-print --output json"
[ "$(cat "$FAKE_AZ_LOG")" = "$want" ] && ok "one az call, read-only what-if on the compiled files" \
  || bad "az calls: $(cat "$FAKE_AZ_LOG")"

plan >"$work/two"
[ "$(digest_of "$work/two")" = "$d1" ] && ok "the same inputs give the same digest" || bad "digest not stable"

: >"$FAKE_AZ_LOG"
plan --from-file "$here/testdata/whatif.json" >"$work/file"
[ "$(digest_of "$work/file")" = "$d1" ] && [ ! -s "$FAKE_AZ_LOG" ] \
  && ok "--from-file: the same digest, and no az call" || bad "--from-file"

variant() {  # name, python expression over `d` (the fixture)
  python3 - "$here/testdata/whatif.json" "$work/$1.json" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
exec(sys.argv[3])
json.dump(d, open(sys.argv[2], "w"))
PY
  echo "$work/$1.json"
}
cli="$(variant cli 'd = {"changes": d["properties"]["changes"], "status": "Succeeded", "error": None}')"
[ "$(digest_of <(plan --from-file "$cli"))" = "$d1" ] && ok "az's flattened shape gives the same digest" || bad "flattened shape"
noise="$(variant noise 'd["properties"]["changes"][2]["after"]["etag"] = "\"other\""; d["properties"]["changes"][2]["before"]["systemData"]["createdAt"] = "2026-11-04T00:00:00Z"')"
[ "$(digest_of <(plan --from-file "$noise"))" = "$d1" ] && ok "fields Azure fills in (etag, systemData) don't change it" || bad "volatile fields"
other="$(variant other 'd["properties"]["changes"][0]["after"]["properties"]["minimumTlsVersion"] = "TLS1_0"')"
[ "$(digest_of <(plan --from-file "$other"))" != "$d1" ] && ok "a changed property gives another digest" || bad "property change not seen"
[ "$(OMNIFILE_RUN_ID=other digest_of <(OMNIFILE_RUN_ID=other plan --from-file "$here/testdata/whatif.json"))" != "$d1" ] \
  && ok "other parameter values give another digest" || bad "parameters not covered"

dele="$(variant dele 'c = dict(d["properties"]["changes"][0]); c["changeType"] = "Delete"; c["before"] = c.pop("after"); d["properties"]["changes"][0] = c')"
plan --from-file "$dele" >"$work/dele"
grep -q "^!! DELETE Microsoft.Storage/storageAccounts oftestwtstuniq012345$" "$work/dele" \
  && grep -q "^!! WORM or lock change: Delete" "$work/dele" && ok "a deletion, and a WORM account's, are flagged" \
  || bad "no deletion warning: $(grep '!!' "$work/dele")"

failed="$(variant failed 'd = {"status": "Failed", "error": {"code": "InvalidTemplate", "message": "bad"}}')"
if plan --from-file "$failed" >"$work/failed"; then bad "a failed what-if passed"; else
  grep -q "^digest:" "$work/failed" && bad "a failed what-if printed a digest" || ok "a failed what-if exits 1 with no digest"
fi

plan --from-file "$here/testdata/whatif.json" --json-out "$work/summary.json" >/dev/null
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["digest"]==sys.argv[2] and d["counts"]["Create"]==2' \
  "$work/summary.json" "$d1" && ok "--json-out has the same digest and counts" || bad "--json-out"

: >"$FAKE_AZ_LOG"
plan --deploy --approve "$d1" >"$work/deploy"
grep -q "deployment group create --resource-group rg-omnifile-test --name omnifile --template-file <tmp>/template.json --parameters @<tmp>/parameters.json --output json" "$FAKE_AZ_LOG" \
  && grep -q "apiUrl = \"https://oftest-api.example:8443\"" "$work/deploy" \
  && ok "--deploy with the approved digest: what-if again, then create, outputs printed" || bad "deploy: $(cat "$FAKE_AZ_LOG")"
: >"$FAKE_AZ_LOG"
set +e
plan --deploy --approve "sha256:$(printf '0%.0s' {1..64})" >/dev/null 2>"$work/err"; code=$?
set -e
[ "$code" = 3 ] && ! grep -q "group create" "$FAKE_AZ_LOG" && ok "another digest: exit 3, nothing deployed" \
  || bad "wrong digest: exit $code, calls $(cat "$FAKE_AZ_LOG")"
set +e
plan --deploy >/dev/null 2>&1; code=$?
set -e
[ "$code" = 2 ] && ok "--deploy without --approve is a usage error" || bad "--deploy alone: exit $code"
exit "$fail"
