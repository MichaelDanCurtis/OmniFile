#!/usr/bin/env bash
# Give AD groups (synced to Entra) the portal's roles (docs/guide/README.md, section 8.2).
#
#   docs/guide/tools/assign-app-roles.sh --app-id CLIENT_ID ROLE=GROUP [ROLE=GROUP ...]
#
# ROLE is one of the app's roles: the portal's (ArchiveAdmin, DataSteward, User), or the API's application
# roles (Policy.Propose, Recall.Request, Archive.Read, Archive.Adopt; section 18). GROUP is a group's display
# name in Entra, or app:<application id> for an API client's application. Each is assigned its role on the
# enterprise application, through Microsoft Graph (`az rest`), unless it already has it. A group can hold a
# role only on Entra ID P1 or above. Runs as you: you need Application Administrator (or Cloud Application
# Administrator).
set -euo pipefail

APP=''
pairs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --app-id) APP=$2; shift ;;
    *=*) pairs+=("$1") ;;
    *) echo "usage: assign-app-roles.sh --app-id CLIENT_ID ROLE=GROUP [ROLE=GROUP ...]" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$APP" ] && [ ${#pairs[@]} -gt 0 ] || { echo "assign-app-roles.sh: --app-id and at least one ROLE=GROUP" >&2; exit 2; }

sp=$(az ad sp show --id "$APP" --query id --output tsv)
for pair in "${pairs[@]}"; do
  role=${pair%%=*} group=${pair#*=}
  role_id=$(az ad sp show --id "$APP" --query "appRoles[?value=='$role'].id | [0]" --output tsv)
  [ -n "$role_id" ] || { echo "assign-app-roles.sh: the app has no role $role" >&2; exit 1; }
  if [[ $group == app:* ]]; then
    group_id=$(az ad sp show --id "${group#app:}" --query id --output tsv)
  else
    group_id=$(az ad group show --group "$group" --query id --output tsv)
  fi
  [ -n "$group_id" ] || { echo "assign-app-roles.sh: no $group in Entra" >&2; exit 1; }
  have=$(az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$sp/appRoleAssignedTo" \
    --query "value[?principalId=='$group_id' && appRoleId=='$role_id'] | length(@)" --output tsv)
  if [ "$have" != 0 ]; then
    echo "  $group already has $role"
    continue
  fi
  az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$sp/appRoleAssignedTo" \
    --headers 'Content-Type=application/json' \
    --body "{\"principalId\":\"$group_id\",\"resourceId\":\"$sp\",\"appRoleId\":\"$role_id\"}" --output none
  echo "  $group now has $role"
done
