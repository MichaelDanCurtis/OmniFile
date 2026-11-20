#!/usr/bin/env bash
# Give the portal's roles, and API clients their scopes (docs/guide/README.md, sections 8.2 and 18).
#
#   docs/guide/tools/assign-app-roles.sh --app-id CLIENT_ID ROLE=WHO [ROLE=WHO ...]
#   docs/guide/tools/assign-app-roles.sh --app-id CLIENT_ID --portal-roles
#
# ROLE is one of the app's roles: the portal's (ArchiveAdmin, DataSteward, User), or the API's application
# roles (Policy.Propose, Recall.Request, Archive.Read, Archive.Adopt; section 18). WHO is a group's display
# name in Entra, user:<sign-in name> for one person (their UPN), or app:<application id> for an API client's
# application. Each is assigned its role on the enterprise application, through Microsoft Graph (`az rest`),
# unless it already has it.
#
# --portal-roles reads omnifile.env's choice (section 1.5, decision 89) from the environment:
#   OMNI_ROLE_HOLDERS=groups  ArchiveAdmin, DataSteward and User go to OMNI_GROUP_ARCHIVEADMIN,
#                             OMNI_GROUP_DATASTEWARD and OMNI_GROUP_USER;
#   OMNI_ROLE_HOLDERS=people  ArchiveAdmin and DataSteward go to each person named in
#                             OMNI_PEOPLE_ARCHIVEADMIN and OMNI_PEOPLE_DATASTEWARD (sign-in names,
#                             separated by spaces). User goes to nobody: the service gives it to every
#                             AD user in the tenant (OMNIFILE_WEB_ROLE_HOLDERS=people). That needs the
#                             app's "Assignment required" off (Entra's default, and register.sh's), or
#                             Entra refuses everyone unnamed at sign-in: the script stops if it is on.
#   Empty or unset means groups. Switching later doesn't remove the other mode's holders: remove them in
#   Entra (Enterprise applications, the portal's app, Users and groups).
#
# A group can hold a role only on Entra ID P1 or above. Before the first group, the script reads the tenant's
# licences; without P1 it stops and says to choose OMNI_ROLE_HOLDERS=people. Runs as you: you need Application
# Administrator (or Cloud Application Administrator).
set -euo pipefail

GRAPH=https://graph.microsoft.com/v1.0
APP='' portal=0
pairs=()
usage() { echo "usage: assign-app-roles.sh --app-id CLIENT_ID (ROLE=WHO ... | --portal-roles)" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --app-id) [ $# -ge 2 ] || usage; APP=$2; shift ;;
    --portal-roles) portal=1 ;;
    *=*) pairs+=("$1") ;;
    *) usage ;;
  esac
  shift
done
[ -n "$APP" ] || { echo "assign-app-roles.sh: --app-id is required" >&2; exit 2; }

if [ "$portal" = 1 ]; then
  case "${OMNI_ROLE_HOLDERS:-groups}" in
    groups)
      for v in OMNI_GROUP_ARCHIVEADMIN OMNI_GROUP_DATASTEWARD OMNI_GROUP_USER; do
        [ -n "${!v:-}" ] || { echo "assign-app-roles.sh: OMNI_ROLE_HOLDERS=groups needs $v" >&2; exit 2; }
      done
      pairs+=("ArchiveAdmin=$OMNI_GROUP_ARCHIVEADMIN" "DataSteward=$OMNI_GROUP_DATASTEWARD" "User=$OMNI_GROUP_USER")
      ;;
    people)
      read -ra admins <<<"${OMNI_PEOPLE_ARCHIVEADMIN:-}"
      read -ra stewards <<<"${OMNI_PEOPLE_DATASTEWARD:-}"
      [ ${#admins[@]} -gt 0 ] || { echo "assign-app-roles.sh: OMNI_ROLE_HOLDERS=people needs at least one OMNI_PEOPLE_ARCHIVEADMIN" >&2; exit 2; }
      for u in "${admins[@]}"; do pairs+=("ArchiveAdmin=user:$u"); done
      for u in ${stewards[@]+"${stewards[@]}"}; do pairs+=("DataSteward=user:$u"); done
      echo "  User: every AD user in the tenant has it without an assignment (OMNI_ROLE_HOLDERS=people)"
      ;;
    *) echo "assign-app-roles.sh: OMNI_ROLE_HOLDERS must be groups or people (got '${OMNI_ROLE_HOLDERS:-}')" >&2; exit 2 ;;
  esac
fi
[ ${#pairs[@]} -gt 0 ] || { echo "assign-app-roles.sh: give at least one ROLE=WHO, or --portal-roles" >&2; exit 2; }

# Entra ID P1 (or P2, which includes it), read once, before the first group.
p1_checked=0
need_p1() {
  [ "$p1_checked" = 1 ] && return 0
  p1_checked=1
  local plans
  if ! plans=$(az rest --method GET --uri "$GRAPH/subscribedSkus" \
      --query "value[?capabilityStatus=='Enabled'].servicePlans[].servicePlanName" --output tsv 2>/dev/null); then
    echo "  note: couldn't read the tenant's licences; assigning to groups anyway (it needs Entra ID P1)"
    return 0
  fi
  if ! grep -qxE 'AAD_PREMIUM(_P2)?' <<<"$plans"; then
    echo "assign-app-roles.sh: giving a role to a group needs Entra ID P1, and this tenant has none." >&2
    echo "  Set OMNI_ROLE_HOLDERS=people in omnifile.env and name the ArchiveAdmins and DataStewards" >&2
    echo "  (section 1.5), or add Entra ID P1 to the tenant, then run this step again." >&2
    exit 1
  fi
}

sp=$(az ad sp show --id "$APP" --query id --output tsv)
if [ "$portal" = 1 ] && [ "${OMNI_ROLE_HOLDERS:-groups}" = people ]; then
  required=$(az ad sp show --id "$APP" --query appRoleAssignmentRequired --output tsv)
  if [ "$required" = true ]; then
    echo "assign-app-roles.sh: the portal's app has \"Assignment required\" on, so Entra refuses everyone" >&2
    echo "  not named at sign-in, and OMNI_ROLE_HOLDERS=people can't make every AD user a User. Turn it" >&2
    echo "  off (Enterprise applications, the portal's app, Properties), then run this step again." >&2
    exit 1
  fi
fi
for pair in "${pairs[@]}"; do
  role=${pair%%=*} who=${pair#*=}
  role_id=$(az ad sp show --id "$APP" --query "appRoles[?value=='$role'].id | [0]" --output tsv)
  [ -n "$role_id" ] || { echo "assign-app-roles.sh: the app has no role $role" >&2; exit 1; }
  case "$who" in
    app:*) id=$(az ad sp show --id "${who#app:}" --query id --output tsv) ;;
    # A person by their sign-in name (UPN): a guest's is its #EXT# name, not its mail address.
    user:*) id=$(az ad user show --id "${who#user:}" --query id --output tsv) ;;
    *) need_p1
      id=$(az ad group show --group "$who" --query id --output tsv) ;;
  esac
  [ -n "$id" ] || { echo "assign-app-roles.sh: no $who in Entra" >&2; exit 1; }
  # Assign, and take Graph's "already exists" as done: no listing to page through, and a rerun
  # changes nothing.
  if out=$(az rest --method POST --uri "$GRAPH/servicePrincipals/$sp/appRoleAssignedTo" \
      --headers 'Content-Type=application/json' \
      --body "{\"principalId\":\"$id\",\"resourceId\":\"$sp\",\"appRoleId\":\"$role_id\"}" --output none 2>&1); then
    echo "  $who now has $role"
  elif grep -q "already exists" <<<"$out"; then
    echo "  $who already has $role"
  else
    echo "$out" >&2
    echo "assign-app-roles.sh: couldn't give $who the role $role" >&2
    exit 1
  fi
done
