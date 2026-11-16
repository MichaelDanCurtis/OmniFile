#!/usr/bin/env bash
# Offline tests of deploy/entra/register.sh, against a fake `az` (testdata/fake-az) first on
# PATH, which records every call. The calls of a first run and of a rerun are compared with
# golden transcripts (testdata/register-*.transcript); UPDATE_GOLDEN=1 rewrites them.
# Never runs the real az. Prints one line per check; exit 1 on a failure.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
unset AZURE_CONFIG_DIR AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID || true
mkdir -p "$work/bin"
ln -s "$here/testdata/fake-az" "$work/bin/az"
export PATH="$work/bin:$PATH"
export FAKE_AZ_LOG="$work/az.log" FAKE_AZ_STATE="$work/tenant.json"
[ "$(command -v az)" = "$work/bin/az" ] || { echo "FAIL the fake az is not first on PATH"; exit 1; }
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

TENANT=72f988bf-0000-0000-0000-000000000000
WEB=11111111-0000-0000-0000-00000000000a
WORKER=22222222-0000-0000-0000-00000000000b
WORKER_CLIENT=33333333-0000-0000-0000-00000000000c
"$repo/deploy/teams/build.sh" --bot-app-id "$WORKER_CLIENT" --portal-url https://omni-web.example.test \
  --out-dir "$work/teams" >/dev/null
register() {
  "$here/register.sh" --tenant "$TENANT" --prefix omni --web-origin https://omni-web.example.test \
    --web-principal "$WEB" --worker-principal "$WORKER" --out "$work/entra.json" "$@"
}

golden() {  # name, log
  local want="$here/testdata/register-$1.transcript"
  if [ "${UPDATE_GOLDEN:-}" = 1 ]; then cp "$2" "$want"; echo "     wrote $want"; fi
  if diff -u "$want" "$2" >"$work/diff"; then ok "the $1 run's az calls match testdata/register-$1.transcript"
  else bad "the $1 run's az calls differ from the transcript"; sed 's/^/     /' "$work/diff"; fi
}

register --teams-app-zip "$work/teams/omnifile-teams-app.zip" \
  --mail-sender archive-notices@example.test --worker-client-id "$WORKER_CLIENT" >"$work/out1"
golden first "$FAKE_AZ_LOG"
python3 - "$work/entra.json" <<'PY' && ok "entra.json names the Bicep parameters it feeds" || bad "entra.json"
import json, sys
d = json.load(open(sys.argv[1]))
app = d["webClientId"]
assert app.startswith("aaaaaaaa-"), d
assert d["webApiAudiences"] == [f"api://{app}", app], d
assert d["teamsAppCatalogId"] == "cccccccc-0000-0000-0000-000000000001", d
assert d["graphAppRoles"] == ["User.Read.All", "GroupMember.Read.All",
                              "TeamsAppInstallation.ReadWriteSelfForUser.All"], d
PY
grep -q "^webClientId=aaaaaaaa-" "$work/out1" && ok "key=value lines on stdout" || bad "no key=value lines"
grep -q 'Application Mail.Send' "$work/out1" && ok "the Exchange scoping steps are printed for --mail-sender" || bad "no Exchange steps"
grep -q "Mail.Send" "$FAKE_AZ_LOG" && bad "Mail.Send was granted in Entra" || ok "Entra's tenant-wide Mail.Send is never granted"
python3 - "$FAKE_AZ_STATE" "$WEB" <<'PY' && ok "web's managed identity is the federated credential's subject" || bad "federated credential"
import json, sys
s = json.load(open(sys.argv[1]))
(fic,) = s["fics"].values()
assert fic["subject"] == sys.argv[2] and fic["audiences"] == ["api://AzureADTokenExchange"], fic
assert fic["issuer"] == "https://login.microsoftonline.com/72f988bf-0000-0000-0000-000000000000/v2.0", fic
PY

: >"$FAKE_AZ_LOG"
register --teams-app-zip "$work/teams/omnifile-teams-app.zip" >/dev/null
golden rerun "$FAKE_AZ_LOG"
if grep -Eq " create |--method POST --uri \S+/appRoleAssignments" "$FAKE_AZ_LOG"; then
  bad "a rerun created something"
else
  ok "a rerun creates nothing (no app, principal, credential or role assignment)"
fi

: >"$FAKE_AZ_LOG"
if FAKE_AZ_TENANT=00000000-0000-0000-0000-0000000000ff register >/dev/null 2>"$work/err"; then
  bad "ran in another tenant"
else
  [ "$(wc -l <"$FAKE_AZ_LOG")" -eq 1 ] && grep -q "another\|not $TENANT\|tenant" "$work/err" \
    && ok "signed in to another tenant: refuses after one read-only call" || bad "$(cat "$work/err")"
fi

for args in "--web-origin http://plain.example" "--web-origin https://x.example/path" "--prefix TOO-LONG-PREFIX"; do
  : >"$FAKE_AZ_LOG"
  # shellcheck disable=SC2086
  if register $args >/dev/null 2>&1; then bad "accepted $args"; else
    [ ! -s "$FAKE_AZ_LOG" ] && ok "refuses $args before calling az" || bad "$args called az"
  fi
done
if register --mail-sender a@example.test >/dev/null 2>&1; then bad "--mail-sender without the client id"; else ok "--mail-sender needs --worker-client-id"; fi

# Ids from a zip or an az answer go into Graph URLs and $filter: anything but a GUID is refused.
python3 - "$work/teams/omnifile-teams-app.zip" "$work/evil.zip" <<'PY'
import json, sys, zipfile
src = zipfile.ZipFile(sys.argv[1])
with zipfile.ZipFile(sys.argv[2], "w") as out:
    for name in src.namelist():
        data = src.read(name)
        if name == "manifest.json":
            doc = json.loads(data)
            doc["id"] = "x' or displayName ne 'y"
            data = json.dumps(doc).encode()
        out.writestr(name, data)
PY
: >"$FAKE_AZ_LOG"
if register --teams-app-zip "$work/evil.zip" >/dev/null 2>"$work/err"; then
  bad "a manifest id that isn't a GUID went into a \$filter"
else
  [ ! -s "$FAKE_AZ_LOG" ] && grep -q "GUID" "$work/err" \
    && ok "a manifest id that isn't a GUID is refused before any az call" || bad "$(cat "$work/err")"
fi
: >"$FAKE_AZ_LOG"
if FAKE_AZ_CATALOG_ID="../../servicePrincipals" register --teams-app-zip "$work/teams/omnifile-teams-app.zip" >/dev/null 2>"$work/err"; then
  bad "a catalog id that isn't a GUID went into a path"
else
  ! grep -q "appDefinitions" "$FAKE_AZ_LOG" && grep -q "catalog id" "$work/err" \
    && ok "a catalog id from Graph that isn't a GUID is refused, never put in a path" || bad "$(cat "$work/err")"
fi

: >"$FAKE_AZ_LOG"
register --dry-run --teams-app-zip "$work/teams/omnifile-teams-app.zip" >/dev/null 2>"$work/dry"
[ ! -s "$FAKE_AZ_LOG" ] && ok "--dry-run calls no az" || bad "--dry-run called az"
grep -q "^az ad app create" "$work/dry" && grep -q "appRoleAssignments" "$work/dry" \
  && ok "--dry-run prints the commands" || bad "--dry-run printed $(head -3 "$work/dry")"
exit "$fail"
