#!/usr/bin/env bash
# Check omnifile.env before the customer guide uses it (docs/guide/README.md, step 5).
#
#   docs/guide/tools/check-env.sh [FILE]       default ./omnifile.env
#
# Every value the example names must be set (a few may be empty, as the example says), and each must
# have the right shape: ids are GUIDs, networks are CIDRs, names fit Azure's and AD's rules. It prints
# one line per problem and exits 1 if there is any. It reads the file only; it calls nothing.
set -uo pipefail

FILE=${1:-./omnifile.env}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
EXAMPLE=$HERE/../omnifile.env.example
[ -f "$FILE" ] || { echo "check-env.sh: no $FILE (step 4 copies docs/guide/omnifile.env.example)" >&2; exit 2; }

# shellcheck disable=SC1090
. "$FILE"
bad=0
problem() { echo "  $1: $2"; bad=1; }

guid='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
cidr='^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'
# May be empty.
optional=" OMNI_DEPLOYMENT_SIZE OMNI_DNS_ZONE_RG OMNI_ALERT_ACTION_GROUP_ID OMNI_AD_JOIN_OU OMNI_TEST_HUB_NO_GATEWAY OMNI_TEST_RUN_ID OMNI_TEST_RUN_EXPIRES OMNI_TEST_PG_SKU_TIER OMNI_TEST_PG_SKU_NAME OMNI_TEST_PG_STORAGE_GB OMNI_TEST_KV_PURGE_PROTECTION OMNI_TEST_KV_SOFT_DELETE_DAYS OMNI_TEST_API_MIN_REPLICAS OMNI_TEST_API_MAX_REPLICAS OMNI_TEST_WORKER_MAX_REPLICAS OMNI_TEST_WORM_RETENTION_DAYS OMNI_TEST_LOCK_WORM OMNI_TEST_BLOB_SOFT_DELETE_DAYS OMNI_TEST_ALERTS OMNI_AWS_NAME_SUFFIX OMNI_API_CLIENT_APP_ID OMNI_API_CLIENT_NAME OMNI_API_CLIENT_SCOPES OMNI_API_CLIENT_OWNER OMNI_AWS_ACCOUNT_ID OMNI_AWS_REGION \
OMNI_AWS_VPC_ID OMNI_AWS_SUBNET_IDS OMNI_AWS_SG_ID OMNI_AWS_SOURCE_BUCKETS OMNI_AWS_OPS_BUCKET \
OMNI_PEOPLE_ARCHIVEADMIN OMNI_PEOPLE_DATASTEWARD OMNI_ROLE_HOLDERS "
# With the portal's roles held by named people (section 1.5), the role groups aren't used.
if [ "${OMNI_ROLE_HOLDERS:-}" = people ]; then
  optional="$optional OMNI_GROUP_ARCHIVEADMIN OMNI_GROUP_DATASTEWARD OMNI_GROUP_USER "
fi

while IFS= read -r name; do
  if [ -z "${!name+set}" ]; then
    problem "$name" "missing (it is in omnifile.env.example)"
  elif [ -z "${!name}" ] && [[ $optional != *" $name "* ]]; then
    problem "$name" "empty"
  fi
done < <(grep -oE '^export OMNI_[A-Z0-9_]+=' "$EXAMPLE" | sed 's/^export //; s/=$//')

match() { # name regex message
  local v=${!1-}
  [ -z "$v" ] && return 0
  [[ $v =~ $2 ]] || problem "$1" "$3 (got '$v')"
}
cidrs() { # name: comma-separated CIDRs, no spaces
  local v=${!1-} c
  [ -z "$v" ] && return 0
  IFS=, read -ra parts <<<"$v"
  for c in "${parts[@]}"; do
    [[ $c =~ $cidr ]] || problem "$1" "'$c' is not a CIDR such as 10.20.0.0/16 (comma-separated, no spaces)"
  done
}

match OMNI_SUBSCRIPTION_ID "$guid" "a subscription id (a GUID)"
match OMNI_TENANT_ID "$guid" "a tenant id (a GUID)"
match OMNI_LOCATION '^[a-z0-9]+$' "an Azure region name such as westeurope"
match OMNI_DEPLOYMENT_SIZE '^(small|standard)$' "small or standard (empty means standard)"
match OMNI_RG '^[-_.()a-zA-Z0-9]{1,90}$' "a resource group name"
match OMNI_PREFIX '^[a-z0-9]{2,8}$' "2 to 8 lowercase letters or digits"
match OMNI_RELEASE '^[0-9]+\.[0-9]+\.[0-9]+$' "a release such as 1.2.0"
match OMNI_ACR '^[a-zA-Z0-9]{5,50}$' "a registry name without .azurecr.io"
for n in OMNI_EDGE_CIDRS OMNI_USER_CIDRS OMNI_ADMIN_CIDRS OMNI_NFS_CLIENTS; do cidrs "$n"; done
for n in OMNI_VNET_PREFIX OMNI_SUBNET_CAE OMNI_SUBNET_RELAY OMNI_SUBNET_PG OMNI_SUBNET_PE; do
  match "$n" "$cidr" "a CIDR"
done
match OMNI_HUB_VNET_ID '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Network/virtualNetworks/[^/]+$' \
  "a virtual network's resource id"
match OMNI_DNS_RESOLVER_IP '^([0-9]{1,3}\.){3}[0-9]{1,3}$' "an IPv4 address"
match OMNI_PG_ADMIN_GROUP "^[^'\\\\]+$" "a group name without quotes or backslashes (it goes into libpq strings)"
match OMNI_TEST_HUB_NO_GATEWAY '^1$' "empty, or 1 in a test run only"
if [ "${OMNI_TEST_HUB_NO_GATEWAY:-}" = 1 ]; then
  echo "  note: OMNI_TEST_HUB_NO_GATEWAY=1 is for test runs only: a client's hub must have its gateway"
fi
# Test runs only (tests/real): each empty, or in its test shape.
match OMNI_TEST_RUN_ID '^[a-z0-9][a-z0-9-]{3,39}$' "a run id: 4 to 40 lowercase letters, digits or hyphens"
match OMNI_TEST_RUN_EXPIRES '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?Z$' "a UTC time such as 2026-11-04T18:00Z"
match OMNI_TEST_PG_SKU_TIER '^Burstable$' "Burstable (test runs only)"
match OMNI_TEST_PG_SKU_NAME '^Standard_B[0-9]+[a-z]*$' "a Burstable size such as Standard_B2ms"
match OMNI_TEST_PG_STORAGE_GB '^(32|64)$' "32 or 64"
match OMNI_TEST_KV_PURGE_PROTECTION '^false$' "false (test runs only)"
match OMNI_TEST_KV_SOFT_DELETE_DAYS '^([7-9]|[1-8][0-9]|90)$' "7 to 90"
match OMNI_TEST_API_MIN_REPLICAS '^[1-9]$' "1 to 9"
match OMNI_TEST_API_MAX_REPLICAS '^[1-9]$' "1 to 9"
match OMNI_TEST_WORKER_MAX_REPLICAS '^[1-9]$' "1 to 9"
match OMNI_TEST_WORM_RETENTION_DAYS '^[1-7]$' "1 to 7 (an unlocked test WORM share)"
match OMNI_TEST_LOCK_WORM '^false$' "false (test runs only)"
match OMNI_TEST_BLOB_SOFT_DELETE_DAYS '^[1-7]$' "1 to 7"
match OMNI_TEST_ALERTS '^false$' "false (test runs only)"
match OMNI_AWS_NAME_SUFFIX '^[a-z0-9][a-z0-9-]{3,39}$' "empty, or a run id: 4 to 40 lowercase letters, digits or hyphens (test runs only)"
if [ -n "${OMNI_TEST_RUN_ID:-}" ] && [ -z "${OMNI_TEST_RUN_EXPIRES:-}" ]; then
  problem OMNI_TEST_RUN_EXPIRES "a test run needs its expiry"
fi
set_tests=""
[ -z "${OMNI_TEST_RUN_ID:-}" ] || set_tests="$set_tests OMNI_TEST_RUN_ID"
[ -z "${OMNI_TEST_RUN_EXPIRES:-}" ] || set_tests="$set_tests OMNI_TEST_RUN_EXPIRES"
[ -z "${OMNI_TEST_PG_SKU_TIER:-}" ] || set_tests="$set_tests OMNI_TEST_PG_SKU_TIER"
[ -z "${OMNI_TEST_PG_SKU_NAME:-}" ] || set_tests="$set_tests OMNI_TEST_PG_SKU_NAME"
[ -z "${OMNI_TEST_PG_STORAGE_GB:-}" ] || set_tests="$set_tests OMNI_TEST_PG_STORAGE_GB"
[ -z "${OMNI_TEST_KV_PURGE_PROTECTION:-}" ] || set_tests="$set_tests OMNI_TEST_KV_PURGE_PROTECTION"
[ -z "${OMNI_TEST_KV_SOFT_DELETE_DAYS:-}" ] || set_tests="$set_tests OMNI_TEST_KV_SOFT_DELETE_DAYS"
[ -z "${OMNI_TEST_API_MIN_REPLICAS:-}" ] || set_tests="$set_tests OMNI_TEST_API_MIN_REPLICAS"
[ -z "${OMNI_TEST_API_MAX_REPLICAS:-}" ] || set_tests="$set_tests OMNI_TEST_API_MAX_REPLICAS"
[ -z "${OMNI_TEST_WORKER_MAX_REPLICAS:-}" ] || set_tests="$set_tests OMNI_TEST_WORKER_MAX_REPLICAS"
[ -z "${OMNI_TEST_WORM_RETENTION_DAYS:-}" ] || set_tests="$set_tests OMNI_TEST_WORM_RETENTION_DAYS"
[ -z "${OMNI_TEST_LOCK_WORM:-}" ] || set_tests="$set_tests OMNI_TEST_LOCK_WORM"
[ -z "${OMNI_TEST_BLOB_SOFT_DELETE_DAYS:-}" ] || set_tests="$set_tests OMNI_TEST_BLOB_SOFT_DELETE_DAYS"
[ -z "${OMNI_TEST_ALERTS:-}" ] || set_tests="$set_tests OMNI_TEST_ALERTS"
[ -z "${OMNI_AWS_NAME_SUFFIX:-}" ] || set_tests="$set_tests OMNI_AWS_NAME_SUFFIX"
if [ -n "$set_tests" ]; then
  echo "  note: test-only values are set ($set_tests ): this is a test run, never a client deployment"
fi
match OMNI_NOTIFY_SENDER '^[^@ ]+@[^@ ]+$' "a mailbox address"
match OMNI_ROLE_HOLDERS '^(groups|people)$' "groups or people, or empty for groups (section 1.5)"
if [ "${OMNI_ROLE_HOLDERS:-}" = people ]; then
  admins=${OMNI_PEOPLE_ARCHIVEADMIN-}
  [ -n "${admins// /}" ] || problem OMNI_PEOPLE_ARCHIVEADMIN "name at least one ArchiveAdmin (OMNI_ROLE_HOLDERS=people)"
  for n in OMNI_PEOPLE_ARCHIVEADMIN OMNI_PEOPLE_DATASTEWARD; do
    for who in ${!n-}; do
      [[ $who =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] || problem "$n" "'$who' is not a sign-in name such as ann@contoso.example"
    done
  done
fi
match OMNI_AD_DOMAIN '^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$' "the AD DNS domain, such as corp.example"
match OMNI_AD_WORKGROUP '^[A-Za-z0-9-]{1,15}$' "the short domain name, at most 15 characters"
match OMNI_FIRST_UID '^[0-9]+$' "a number"
match OMNI_FIRST_GID '^[0-9]+$' "a number"
for n in OMNI_FIRST_UID OMNI_FIRST_GID OMNI_TEST_UID OMNI_DOMAIN_USERS_GID; do
  v=${!n-}
  if [[ $v =~ ^[0-9]+$ ]] && { [ "$v" -lt 10000 ] || [ "$v" -gt 999999 ]; }; then
    problem "$n" "must be inside 10000-999999 (the filer's idmap range)"
  fi
done
match OMNI_EDGE_NAME '^[a-z][a-z0-9-]{0,62}$' "lowercase letters, digits and hyphens, starting with a letter"
match OMNI_EDGE_HOSTNAME '^[A-Za-z][A-Za-z0-9-]{0,14}$' "at most 15 letters, digits or hyphens, starting with a letter"
match OMNI_EDGE_ADDRESS "$cidr" "the filer's address with its prefix length, such as 10.20.1.10/24"
match OMNI_EDGE_GATEWAY '^([0-9]{1,3}\.){3}[0-9]{1,3}$' "an IPv4 address"
match OMNI_EDGE_DNS '^([0-9]{1,3}\.){3}[0-9]{1,3}( ([0-9]{1,3}\.){3}[0-9]{1,3})*$' "IPv4 addresses separated by spaces"
match OMNI_DATA_DISK_GB '^[0-9]+$' "a size in GB"
match OMNI_SHARE '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$' "1 to 80 letters, digits, '.', '_' or '-'"
match OMNI_TEST_UID '^[0-9]+$' "the test user's uidNumber"
match OMNI_DOMAIN_USERS_GID '^[0-9]+$' "the gidNumber of Domain Users"
match OMNI_CLIENT_MOUNT '^/' "an absolute path"

# No network allowed to reach web or enrol may overlap the relay subnet (threat model T1). main.bicep
# refuses the same overlap, 0.0.0.0/0 included, so this catches it before a deployment does.
relay=${OMNI_SUBNET_RELAY-}
if [ -n "$relay" ]; then
  for n in OMNI_EDGE_CIDRS OMNI_USER_CIDRS OMNI_ADMIN_CIDRS; do
    if python3 - "$relay" "${!n-}" <<'PY'; then problem "$n" "overlaps the relay subnet $relay"; fi
import ipaddress, sys
relay = ipaddress.ip_network(sys.argv[1], strict=False)
for c in filter(None, sys.argv[2].split(",")):
    try:
        if relay.overlaps(ipaddress.ip_network(c, strict=False)):
            sys.exit(0)
    except ValueError:
        pass
sys.exit(1)
PY
  done
fi

# The S3 values go together: all set, or all empty.
aws_set=0 aws_empty=0
for n in OMNI_AWS_ACCOUNT_ID OMNI_AWS_REGION OMNI_AWS_VPC_ID OMNI_AWS_SUBNET_IDS OMNI_AWS_SG_ID \
  OMNI_AWS_SOURCE_BUCKETS OMNI_AWS_OPS_BUCKET; do
  if [ -n "${!n-}" ]; then aws_set=1; else aws_empty=1; fi
done
if [ "$aws_set" = 1 ] && [ "$aws_empty" = 1 ]; then
  problem OMNI_AWS_ "set all the S3 values (section 17), or none"
fi
match OMNI_AWS_ACCOUNT_ID '^[0-9]{12}$' "a 12-digit AWS account id"

# An API client's values go together too (section 18).
api_set=0 api_empty=0
for n in OMNI_API_CLIENT_APP_ID OMNI_API_CLIENT_NAME OMNI_API_CLIENT_SCOPES OMNI_API_CLIENT_OWNER; do
  if [ -n "${!n-}" ]; then api_set=1; else api_empty=1; fi
done
if [ "$api_set" = 1 ] && [ "$api_empty" = 1 ]; then
  problem OMNI_API_CLIENT_ "set all four API client values (section 18), or none"
fi
match OMNI_API_CLIENT_APP_ID "$guid" "an application (client) id (a GUID)"
match OMNI_API_CLIENT_NAME '^.{1,80}$' "1 to 80 characters"
scope='(Policy\.Propose|Recall\.Request|Archive\.Read|Archive\.Adopt)'
match OMNI_API_CLIENT_SCOPES "^$scope(,$scope)*$" "scopes from Policy.Propose, Recall.Request, Archive.Read, Archive.Adopt, comma-separated, no spaces"
match OMNI_API_CLIENT_OWNER '^[^@ ]+@[^@ ]+$' "a sign-in name such as someone@contoso.example"

if [ "$bad" = 0 ]; then
  echo "omnifile.env: every value is set and has the right shape"
else
  echo "omnifile.env: fix the values above, then run this step again"
fi
exit "$bad"
