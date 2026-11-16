#!/usr/bin/env bash
# Compile-only checks for the Azure deployment. Never signs in to Azure, never creates anything.
#
#   deploy/azure/check.sh
#
# Builds main.bicep and every bicepparam file with the Bicep CLI (`bicep`, or `az bicep` when only the Azure CLI
# is there), lints main.bicep and every module, and fails on any error or warning. It also checks that a
# parameters file without the three ingress allow-lists fails validation (closed by default, threat model T1),
# and runs the offline tests of relay-ranges.sh (decision 49). The parameters files are built in a copy of this
# folder, with relay-allowed-cidrs.json made by relay-ranges.sh from the recorded fixture (testdata/), so the
# relay's list in the built parameters is the one the script wrote, and a real list here is never touched.
# test.bicepparam is built with stand-in values for the environment it reads and the real image lock
# (deploy/images/images.lock.json), and its result checked. It also runs the offline tests of whatif.sh
# (test-whatif.sh, against a fake az), and checks the two deployment sizes (test-sizes.py): standard is today's
# sizing exactly, small keeps every constraint, and small.bicepparam and standard.bicepparam are what they say.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

if command -v bicep >/dev/null 2>&1; then
  build() { bicep build "$1" --outfile "$2"; }
  build_params() { bicep build-params "$1" --outfile "$2"; }
  lint() { bicep lint "$1"; }
elif command -v az >/dev/null 2>&1; then
  build() { az bicep build --file "$1" --outfile "$2"; }
  build_params() { az bicep build-params --file "$1" --outfile "$2"; }
  lint() { az bicep lint --file "$1"; }
else
  echo "check.sh: needs the Bicep CLI (brew install azure/bicep/bicep) or the Azure CLI" >&2
  exit 2
fi

fail=0
run() {
  # $1 = label; rest = command. Any line with "Warning" or "Error" fails the check.
  local label="$1"; shift
  local log="$out/log"
  if ! "$@" >"$log" 2>&1; then
    echo "FAIL $label"; sed 's/^/    /' "$log"; fail=1; return
  fi
  if grep -Eq '(Warning|Error) ' "$log"; then
    echo "FAIL $label (warnings)"; sed 's/^/    /' "$log"; fail=1; return
  fi
  echo "ok   $label"
}

run "build main.bicep" build "$here/main.bicep" "$out/main.json"

# The two sizes (deploymentSize): standard is today's sizing exactly, small keeps every constraint (test-sizes.py).
# With uv and the service there, it also evaluates the template with the service's ARM evaluator: what each module
# is given at each size, a parameter overriding its size, and the two refusals.
run "sizes: the compiled table" python3 -I "$here/test-sizes.py" "$out/main.json"
if command -v uv >/dev/null 2>&1 && [ -f "$here/../../service/tests/test_deploy_env.py" ]; then
  run "sizes: evaluated" uv run --quiet --project "$here/../../service" python "$here/test-sizes.py" --evaluate "$out/main.json"
else
  echo "skip sizes: evaluated (needs uv and service/)"
fi

# The parameters files read the relay's list from relay-allowed-cidrs.json (relay-ranges.sh writes it at deploy
# time). Build them in a copy, with that file made from the fixture.
src="$out/src"
mkdir -p "$src" "$out/images"
cp -R "$here/main.bicep" "$here/bicepconfig.json" "$here/modules" "$src/"
cp "$here"/*.bicepparam "$src/"
# test.bicepparam reads ../images/images.lock.json and these (stand-ins; tests/real/run.sh sets the real ones).
cp "$here/../images/images.lock.json" "$out/images/"
export OMNIFILE_RUN_ID=check OMNIFILE_RUN_EXPIRES=2026-11-05T00:00:00Z \
  OMNIFILE_TEST_PG_ADMIN_OID=00000000-0000-0000-0000-0000000000a1 OMNIFILE_TEST_PG_ADMIN_NAME=OmniFile-Test-DB \
  OMNIFILE_TEST_REGISTRY=registry.example.test OMNIFILE_TEST_ENROL_CIDRS=10.20.0.0/16,10.21.0.0/16 \
  OMNIFILE_TEST_WEB_CIDRS=10.30.0.0/16
run "relay-ranges.sh offline tests" "$here/test-relay-ranges.sh"
run "relay-ranges.sh from the fixture" "$here/relay-ranges.sh" --location westeurope \
  --from-file "$here/testdata/service-tags.json" --out "$src/relay-allowed-cidrs.json"
for p in "$src"/*.bicepparam; do
  run "build-params $(basename "$p")" build_params "$p" "$out/$(basename "$p").json"
done
# The built relayAllowedCidrs is exactly the script's list.
want="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["prefixes"])' "$src/relay-allowed-cidrs.json" 2>/dev/null || true)"
got="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["parameters"]["relayAllowedCidrs"]["value"])' "$out/main.example.bicepparam.json" 2>/dev/null || true)"
if [ -n "$want" ] && [ "$want" = "$got" ]; then
  echo "ok   relayAllowedCidrs in the built parameters is relay-ranges.sh's list"
else
  echo "FAIL relayAllowedCidrs in the built parameters ($got) is not relay-ranges.sh's list ($want)"; fail=1
fi
# The test deployment: small, removable, tagged, and running the locked images by digest.
if python3 - "$out/test.bicepparam.json" "$out/images/images.lock.json" <<'PY'
import json, sys
p = {k: v["value"] for k, v in json.load(open(sys.argv[1]))["parameters"].items()}
lock = json.load(open(sys.argv[2]))
want = {"deploymentSize": "small", "lockWormAccounts": False, "keyVaultPurgeProtection": False,
        "apiIngress": "tcp", "createPrivateEndpoints": True, "keyVaultSoftDeleteDays": 7}
bad = {k: p.get(k) for k, v in want.items() if p.get(k) != v}
assert not bad, f"test.bicepparam: {bad}"
# The size decides these; a value here would override it.
sized = {"postgresSkuName", "postgresSkuTier", "postgresStorageSizeGB", "apiMinReplicas", "apiMaxReplicas",
         "workerMaxReplicas", "logRetentionDays", "logDailyCapGb"}
assert not sized & set(p), f"test.bicepparam overrides its size: {sorted(sized & set(p))}"
assert p["wormShares"] == [{"name": "tst", "redundancy": "Standard_LRS", "retentionDays": 1}], p["wormShares"]
assert p["tags"]["omnifile-run"] == "check" and p["tags"]["omnifile-expires"] == "2026-11-05T00:00:00Z", p["tags"]
assert p["enrolAllowedCidrs"] == ["10.20.0.0/16", "10.21.0.0/16"], p["enrolAllowedCidrs"]
for app, image in lock["apps"].items():
    i = lock["images"][image]
    ref = f"registry.example.test/{i['repository']}@{i['digest']}"
    assert p[f"{app}Image"] == ref, (app, p[f"{app}Image"], ref)
PY
then
  echo "ok   test.bicepparam: small, unlocked 1-day WORM, purgeable vault, run tags, the locked images"
else
  echo "FAIL test.bicepparam's values"; fail=1
fi
# The two size files: each its size, small in West US 3, both with the constraints no size changes.
if python3 - "$out/small.bicepparam.json" "$out/standard.bicepparam.json" "$out/main.example.bicepparam.json" <<'PY'
import json, sys
def params(f):
    return {k: v["value"] for k, v in json.load(open(f))["parameters"].items()}
small, standard, example = (params(f) for f in sys.argv[1:4])
assert small["deploymentSize"] == "small" and small["location"] == "westus3", small
assert standard["deploymentSize"] == "standard" and example["deploymentSize"] == "standard"
sized = {"postgresSkuName", "postgresSkuTier", "postgresStorageSizeGB", "apiMinReplicas", "apiMaxReplicas",
         "workerMaxReplicas", "logRetentionDays", "logDailyCapGb"}
for name, p in (("small", small), ("standard", standard)):
    assert not sized & set(p), f"{name}.bicepparam overrides its size: {sorted(sized & set(p))}"
    assert p["apiIngress"] == "tcp", name
    for k in ("relayAllowedCidrs", "enrolAllowedCidrs", "webAllowedCidrs"):
        assert p[k], (name, k)
    assert p.get("storagePublicNetworkAccess", "Disabled") == "Disabled", name
    assert p.get("keyVaultPublicNetworkAccess", "Disabled") == "Disabled", name
    assert p.get("keyVaultSku", "premium") == "premium", name
PY
then
  echo "ok   small.bicepparam and standard.bicepparam: their sizes, small in westus3, private and closed"
else
  echo "FAIL small.bicepparam or standard.bicepparam"; fail=1
fi
run "whatif.sh offline tests" "$here/test-whatif.sh"
# Without the file, the example does not build: there is no fallback list.
rm "$src/relay-allowed-cidrs.json"
if build_params "$src/main.example.bicepparam" "$out/nofile.json" >"$out/nofile.log" 2>&1 \
  && ! grep -q 'Error ' "$out/nofile.log"; then
  echo "FAIL the example built without relay-allowed-cidrs.json"; fail=1
elif grep -q 'relay-allowed-cidrs.json' "$out/nofile.log"; then
  echo "ok   without relay-allowed-cidrs.json the example does not build"
else
  echo "FAIL the example failed to build without relay-allowed-cidrs.json, but not because of it"
  sed 's/^/    /' "$out/nofile.log"; fail=1
fi
"$here/relay-ranges.sh" --location westeurope --from-file "$here/testdata/service-tags.json" \
  --out "$src/relay-allowed-cidrs.json" 2>/dev/null

run "lint main.bicep" lint "$here/main.bicep"
for m in "$here"/modules/*.bicep; do
  run "lint modules/$(basename "$m")" lint "$m"
done

# Closed by default: a parameters file that leaves out the allow-lists must not compile.
neg="$src/.check-closed-default.bicepparam"
# Drop each `param <list> = [ ... ]` statement, on one line or several, from the example.
awk '
  /^param (relayAllowedCidrs|enrolAllowedCidrs|webAllowedCidrs)[ =]/ { skip = 1; depth = 0 }
  skip { o = gsub(/\[/, "["); c = gsub(/\]/, "]"); depth += o - c; if (depth <= 0) skip = 0; next }
  { print }
' "$src/main.example.bicepparam" >"$neg"
if build_params "$neg" "$out/neg.json" >"$out/neg.log" 2>&1; then
  echo "FAIL closed by default: a parameters file without the allow-lists compiled"; fail=1
else
  missing=0
  for name in relayAllowedCidrs enrolAllowedCidrs webAllowedCidrs; do
    grep -q "$name" "$out/neg.log" || missing=1
  done
  if [ "$missing" = 0 ]; then
    echo "ok   closed by default: the allow-lists are required"
  else
    echo "FAIL closed by default: the error does not name all three allow-lists"; sed 's/^/    /' "$out/neg.log"; fail=1
  fi
fi

# And an empty list is refused too (@minLength(1)), so "[]" can't quietly mean "open".
awk '
  /^param (relayAllowedCidrs|enrolAllowedCidrs|webAllowedCidrs)[ =]/ { print $1 " " $2 " = []"; skip = 1; depth = 0 }
  skip { o = gsub(/\[/, "["); c = gsub(/\]/, "]"); depth += o - c; if (depth <= 0) skip = 0; next }
  { print }
' "$src/main.example.bicepparam" >"$neg"
if build_params "$neg" "$out/neg.json" >"$out/neg.log" 2>&1; then
  echo "FAIL closed by default: empty allow-lists compiled"; fail=1
elif [ "$(grep -c 'BCP333' "$out/neg.log")" = 3 ]; then
  echo "ok   closed by default: empty allow-lists are refused"
else
  echo "FAIL closed by default: expected three minimum-length errors"; sed 's/^/    /' "$out/neg.log"; fail=1
fi

exit "$fail"
