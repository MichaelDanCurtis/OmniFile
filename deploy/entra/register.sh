#!/usr/bin/env bash
# Register OmniFile with Entra ID: the portal's app registration, the worker's Graph permissions
# and their consent, and (optionally) the Teams app in the organisation's catalog.
#
#   deploy/entra/register.sh --tenant TENANT_ID --prefix NAME_PREFIX --web-origin https://HOST
#                            --web-principal OBJECT_ID --worker-principal OBJECT_ID
#                            [--teams-app-zip ZIP] [--mail-sender UPN --worker-client-id ID]
#                            [--out FILE] [--dry-run]
#
# Arguments (the ids are deployment outputs of deploy/azure/main.bicep, identityPrincipalIds):
#   --tenant ID            the Entra tenant; the signed-in az account must be in it
#   --prefix P             the deployment's namePrefix: names the app "OmniFile <P> portal"
#   --web-origin URL       the portal's origin (output webOrigin); sign-in returns to
#                          <origin>/auth/callback
#   --web-principal OID    web's managed identity: web signs in to Entra with it, through a
#                          federated credential on the app registration (no secret)
#   --worker-principal OID the worker's managed identity, which calls Graph (teams.md 7) and is
#                          the Teams bot: it gets Graph application roles User.Read.All,
#                          GroupMember.Read.All and TeamsAppInstallation.ReadWriteSelfForUser.All
#   --teams-app-zip ZIP    deploy/teams/out/omnifile-teams-app.zip: publish (or update) it in the
#                          organisation's app catalog and print its catalog id. VERIFY that the
#                          admin's az sign-in may call Graph appCatalogs (AppCatalog.ReadWrite.All);
#                          else upload it in the Teams admin centre
#   --mail-sender UPN      print the Exchange Online steps that let the worker send fallback email
#                          from that one mailbox (RBAC for Applications). Entra's tenant-wide
#                          Mail.Send is never granted. Needs --worker-client-id
#   --out FILE             default deploy/entra/out/entra.json
#   --dry-run              print the az commands that would run, run none
#
# What it changes, each step only if it isn't so already (a rerun changes nothing):
#   1. app registration "OmniFile <P> portal": single tenant; web redirects <origin>/auth/callback
#      and <origin>/auth/azure/callback (Connect Azure, docs/design/cloud-connect.md 1.2);
#      user app roles ArchiveAdmin, DataSteward, User and application roles Policy.Propose,
#      Recall.Request, Archive.Read, Archive.Adopt (portal-api.md 2.2); identifier URI
#      api://<appId>; security-group claims and the onprem_sid optional claim; its service
#      principal; delegated openid and profile, admin-consented; delegated Azure Service
#      Management user_impersonation, NOT admin-consented (each ArchiveAdmin consents at
#      Connect Azure; VERIFY RA-43)
#   2. a federated credential on it for web's managed identity
#   3. Graph application roles for the worker's managed identity: assigning an app role to a
#      managed identity's service principal is the admin consent
#   4. with --teams-app-zip, the Teams app in the catalog
#
# Output: FILE (JSON, keys are the Bicep parameters they feed):
#   {"tenantId", "webClientId", "webApiAudiences": ["api://<appId>", "<appId>"],
#    "teamsAppCatalogId" (with --teams-app-zip), "graphAppRoles": [...]}
# and the same as key=value lines on stdout, then the steps it can't do (assign the app roles to
# AD groups; the groups claim as on-premises SIDs, VERIFY; Exchange scoping for --mail-sender).
#
# The signed-in admin needs Application Administrator (step 1, 2) and Privileged Role
# Administrator or Global Administrator (steps 1's consent and 3). It never signs in itself.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/out/entra.json"
tenant="" prefix="" origin="" web_oid="" worker_oid="" teams_zip="" sender="" worker_client="" dry=0

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --tenant) tenant="${2:?}"; shift 2 ;;
    --prefix) prefix="${2:?}"; shift 2 ;;
    --web-origin) origin="${2:?}"; shift 2 ;;
    --web-principal) web_oid="${2:?}"; shift 2 ;;
    --worker-principal) worker_oid="${2:?}"; shift 2 ;;
    --teams-app-zip) teams_zip="${2:?}"; shift 2 ;;
    --mail-sender) sender="${2:?}"; shift 2 ;;
    --worker-client-id) worker_client="${2:?}"; shift 2 ;;
    --out) out="${2:?}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) usage ;;
    *) echo "register.sh: unknown argument $1" >&2; usage ;;
  esac
done
guid='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
for pair in "tenant:$tenant" "web-principal:$web_oid" "worker-principal:$worker_oid"; do
  [[ "${pair#*:}" =~ $guid ]] || { echo "register.sh: --${pair%%:*} must be a GUID" >&2; exit 2; }
done
[[ "$prefix" =~ ^[a-z0-9]{2,8}$ ]] || { echo "register.sh: --prefix is the deployment's namePrefix (2-8 lowercase)" >&2; exit 2; }
[[ "$origin" =~ ^https://[^/]+$ ]] || { echo "register.sh: --web-origin must be https://host, no path" >&2; exit 2; }
[ -z "$teams_zip" ] || [ -f "$teams_zip" ] || { echo "register.sh: no $teams_zip" >&2; exit 2; }
if [ -n "$sender" ] && ! [[ "$worker_client" =~ $guid ]]; then
  echo "register.sh: --mail-sender needs --worker-client-id (the worker identity's client id)" >&2; exit 2
fi
# Every id that goes into a Graph URL or an OData $filter must be a GUID, wherever it came from
# (the zip, or an az answer), so none can change the request (review of #34).
need_guid() {  # what, value
  [[ "$2" =~ $guid ]] || { echo "register.sh: $1 is not a GUID: ${2:0:80}" >&2; exit 1; }
}
manifest_id=""
if [ -n "$teams_zip" ]; then
  manifest_id="$(python3 -c 'import json,sys,zipfile; print(json.load(zipfile.ZipFile(sys.argv[1]).open("manifest.json"))["id"])' "$teams_zip" 2>/dev/null || true)"
  [[ "$manifest_id" =~ $guid ]] || { echo "register.sh: $teams_zip has no manifest id that is a GUID" >&2; exit 2; }
fi

GRAPH=00000003-0000-0000-c000-000000000000
# Azure Service Management (ARM): Connect Azure's delegated sign-in.
ARM_API=797f4846-ba00-4fd7-ba43-dac1f8f63013
GRAPH_URL=https://graph.microsoft.com/v1.0
display="OmniFile $prefix portal"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

az_() {  # run az, or print it (--dry-run)
  if [ "$dry" = 1 ]; then { printf 'az'; printf ' %q' "$@"; printf '\n'; } >&2; return 0; fi
  az "$@"
}
json() { python3 -c "$1" "${@:2}"; }

# Stable ids: a rerun keeps the same role ids, so assignments made to them survive.
role_id() { python3 -c 'import sys,uuid; print(uuid.uuid5(uuid.UUID("2f1c6f0e-3d0a-4b8e-9a51-0f1e5d7c9b3a"), sys.argv[1]))' "$1"; }

if [ "$dry" = 0 ]; then
  signed_in="$(az account show --query tenantId -o tsv)"
  [ "$signed_in" = "$tenant" ] || { echo "register.sh: az is signed in to tenant $signed_in, not $tenant" >&2; exit 1; }
fi

# --- 1. the portal's app registration -------------------------------------------------
json 'import json,sys
roles = [(v, d, ["User"]) for v, d in (
    ("ArchiveAdmin", "Policies, approvals, share settings, WORM, legal holds, audit"),
    ("DataSteward", "Tier pins and keep-online holds on what they can read"),
    ("User", "Browse, recall, rewind and download what their AD groups can read"))]
roles += [(v, d, ["Application"]) for v, d in (
    ("Policy.Propose", "Partner: propose policies"), ("Recall.Request", "Partner: request recalls"),
    ("Archive.Read", "Partner: read the catalog"), ("Archive.Adopt", "Partner: adopt migrations"))]
ids = dict(a.split("=", 1) for a in sys.argv[1:])
print(json.dumps([{"allowedMemberTypes": t, "description": d, "displayName": v, "isEnabled": True,
                   "id": ids[v], "value": v} for v, d, t in roles], indent=1))' \
  $(for r in ArchiveAdmin DataSteward User Policy.Propose Recall.Request Archive.Read Archive.Adopt; do echo "$r=$(role_id "$r")"; done) \
  >"$work/app-roles.json"
cat >"$work/optional-claims.json" <<'EOF'
{"idToken": [{"name": "onprem_sid", "essential": false}, {"name": "groups", "essential": false, "additionalProperties": []}],
 "accessToken": [{"name": "groups", "essential": false, "additionalProperties": []}]}
EOF

app_id="$(az_ ad app list --display-name "$display" --query "[].appId" -o tsv | head -1)"
if [ -z "$app_id" ] || [ "$dry" = 1 ]; then
  created="$(az_ ad app create --display-name "$display" --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "$origin/auth/callback" "$origin/auth/azure/callback" --app-roles "@$work/app-roles.json" \
    --optional-claims "@$work/optional-claims.json" -o json)"
  [ "$dry" = 1 ] && app_id="<appId>" || app_id="$(json 'import json,sys; print(json.load(sys.stdin)["appId"])' <<<"$created")"
  [ "$dry" = 1 ] || need_guid "the new app registration's appId" "$app_id"
  echo "created app registration $display ($app_id)" >&2
else
  need_guid "the app registration's appId" "$app_id"
  echo "app registration $display exists ($app_id)" >&2
  az_ ad app update --id "$app_id" --web-redirect-uris "$origin/auth/callback" "$origin/auth/azure/callback" \
    --app-roles "@$work/app-roles.json" --optional-claims "@$work/optional-claims.json" >/dev/null
fi
az_ ad app update --id "$app_id" --identifier-uris "api://$app_id" --set groupMembershipClaims=SecurityGroup >/dev/null

if [ "$dry" = 1 ] || ! az_ ad sp show --id "$app_id" -o none 2>/dev/null; then
  az_ ad sp create --id "$app_id" -o none
fi

# Graph's permission ids, looked up by name (--dry-run shows a placeholder).
graph_scope() { az_ ad sp show --id "$GRAPH" --query "oauth2PermissionScopes[?value=='$1'].id | [0]" -o tsv; [ "$dry" = 0 ] || echo "<$1>"; }
graph_role() { az_ ad sp show --id "$GRAPH" --query "appRoles[?value=='$1'].id | [0]" -o tsv; [ "$dry" = 0 ] || echo "<$1>"; }
openid="$(graph_scope openid)"; profile="$(graph_scope profile)"
az_ ad app permission add --id "$app_id" --api "$GRAPH" --api-permissions "$openid=Scope" "$profile=Scope" -o none
az_ ad app permission admin-consent --id "$app_id"
# Connect Azure (docs/design/cloud-connect.md 1.2): ARM's user_impersonation, added after the
# tenant-wide consent above, so each ArchiveAdmin consents for themselves when they connect.
arm_scope="$(az_ ad sp show --id "$ARM_API" --query "oauth2PermissionScopes[?value=='user_impersonation'].id | [0]" -o tsv)"
[ "$dry" = 0 ] || arm_scope="<user_impersonation>"
az_ ad app permission add --id "$app_id" --api "$ARM_API" --api-permissions "$arm_scope=Scope" -o none

# --- 2. web's managed identity signs in as the app ----------------------------------------
fic_name="omnifile-web-managed-identity"
existing_fic="$(az_ ad app federated-credential list --id "$app_id" --query "[?name=='$fic_name'].subject | [0]" -o tsv)"
if [ "$existing_fic" != "$web_oid" ] || [ "$dry" = 1 ]; then
  [ -z "$existing_fic" ] || [ "$dry" = 1 ] || az_ ad app federated-credential delete --id "$app_id" --federated-credential-id "$fic_name"
  json 'import json,sys; print(json.dumps({"name": sys.argv[1], "issuer": f"https://login.microsoftonline.com/{sys.argv[2]}/v2.0",
       "subject": sys.argv[3], "audiences": ["api://AzureADTokenExchange"],
       "description": "web (OmniFile portal) signs in with its managed identity"}))' \
    "$fic_name" "$tenant" "$web_oid" >"$work/fic.json"
  az_ ad app federated-credential create --id "$app_id" --parameters "@$work/fic.json" -o none
fi

# --- 3. Graph application roles for the worker --------------------------------------------
graph_sp="$(az_ ad sp show --id "$GRAPH" --query id -o tsv)"
[ "$dry" = 1 ] || need_guid "Graph's service principal id" "$graph_sp"
assigned="$(az_ rest --method GET --uri "$GRAPH_URL/servicePrincipals/$worker_oid/appRoleAssignments" \
  --query "value[?resourceId=='$graph_sp'].appRoleId" -o tsv)"
granted=()
for role in User.Read.All GroupMember.Read.All TeamsAppInstallation.ReadWriteSelfForUser.All; do
  rid="$(graph_role "$role")"
  [ -n "$rid" ] || { echo "register.sh: Graph has no application role $role" >&2; exit 1; }
  [ "$dry" = 1 ] || need_guid "Graph's $role role id" "$rid"
  if [ "$dry" = 1 ] || ! grep -qx "$rid" <<<"$assigned"; then
    json 'import json,sys; print(json.dumps({"principalId": sys.argv[1], "resourceId": sys.argv[2], "appRoleId": sys.argv[3]}))' \
      "$worker_oid" "$graph_sp" "$rid" >"$work/assign.json"
    az_ rest --method POST --uri "$GRAPH_URL/servicePrincipals/$worker_oid/appRoleAssignments" \
      --headers Content-Type=application/json --body "@$work/assign.json" -o none
  fi
  granted+=("$role")
done

# --- 4. the Teams app ---------------------------------------------------------------------
catalog_id=""
if [ -n "$teams_zip" ]; then
  catalog_id="$(az_ rest --method GET --uri "$GRAPH_URL/appCatalogs/teamsApps?\$filter=externalId eq '$manifest_id'" \
    --query "value[0].id" -o tsv)"
  if [ -n "$catalog_id" ] && [ "$dry" = 0 ]; then
    need_guid "the Teams catalog id Graph answered" "$catalog_id"
    az_ rest --method POST --uri "$GRAPH_URL/appCatalogs/teamsApps/$catalog_id/appDefinitions" \
      --headers Content-Type=application/zip --body "@$teams_zip" -o none
  else
    catalog_id="$(az_ rest --method POST --uri "$GRAPH_URL/appCatalogs/teamsApps" \
      --headers Content-Type=application/zip --body "@$teams_zip" --query id -o tsv)"
    [ "$dry" = 0 ] || catalog_id="<teamsAppCatalogId>"
    [ "$dry" = 1 ] || need_guid "the Teams catalog id Graph answered" "$catalog_id"
  fi
fi

# --- outputs ------------------------------------------------------------------------------
[ "$dry" = 1 ] && exit 0
mkdir -p "$(dirname "$out")"
python3 - "$out" "$tenant" "$app_id" "$catalog_id" "${granted[@]}" <<'PY'
import json, sys
out, tenant, app_id, catalog = sys.argv[1:5]
doc = {"tenantId": tenant, "webClientId": app_id, "webApiAudiences": [f"api://{app_id}", app_id],
       "graphAppRoles": sys.argv[5:]}
if catalog:
    doc["teamsAppCatalogId"] = catalog
with open(out, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
for k, v in doc.items():
    print(f"{k}={','.join(v) if isinstance(v, list) else v}")
PY
cat <<EOF
Still to do by hand:
  - Enterprise applications > $display > Users and groups: assign ArchiveAdmin, DataSteward and User
    to the synced AD groups (portal-api.md 2.2).
  - Token configuration: the groups claim as "On Premises Group Security Identifier"
    (VERIFY on a real tenant, real-tenant-checks.md).
EOF
if [ -n "$sender" ]; then
  cat <<EOF
  - Fallback email from $sender only (Exchange Online PowerShell, RBAC for Applications):
      New-ServicePrincipal -AppId $worker_client -ObjectId $worker_oid -DisplayName "OmniFile $prefix worker"
      New-ManagementScope -Name "OmniFile $prefix sender" -RecipientRestrictionFilter "PrimarySmtpAddress -eq '$sender'"
      New-ManagementRoleAssignment -App $worker_client -Role "Application Mail.Send" -CustomResourceScope "OmniFile $prefix sender"
    then redeploy with notifySender = '$sender'.
EOF
fi
echo "wrote $out"
