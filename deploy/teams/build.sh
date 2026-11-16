#!/usr/bin/env bash
# Build the OmniFile Teams app package, validated against Microsoft's manifest schema.
#
#   deploy/teams/build.sh --bot-app-id GUID --portal-url https://HOST [--version X.Y.Z]
#                         [--app-id GUID] [--name NAME] [--relay-host HOST]
#                         [--out-dir DIR] [--schema FILE]
#
# Arguments:
#   --bot-app-id GUID   the bot's Microsoft App ID: the deployment output teamsBotAppId (the
#                       worker's managed identity, deploy/azure/main.bicep)
#   --portal-url URL    the portal's origin (deployment output webOrigin): the developer,
#                       privacy and terms links in the manifest
#   --version X.Y.Z     the app's version (default 1.0.0); raise it for every new upload
#   --app-id GUID       the manifest id (default: derived from the bot id, the same every build)
#   --name NAME         the app's short name (default OmniFile)
#   --relay-host HOST   added to validDomains (optional)
#   --out-dir DIR       default deploy/teams/out
#   --schema FILE       a local copy of MicrosoftTeams.schema.json v1.17 (default: fetched once
#                       from npm, @microsoft/app-manifest 1.1.3, MIT, checked against its
#                       pinned sha512, and cached under ${XDG_CACHE_HOME:-~/.cache}/omnifile)
#
# Output: DIR/omnifile-teams-app.zip (manifest.json, color.png 192x192, outline.png 32x32),
# whose path is the last line printed. Upload it in the Teams admin centre, or with
# deploy/entra/register.sh --teams-app-zip, which prints the catalog id for teamsAppCatalogId.
# The zip is deterministic: the same arguments give the same bytes.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out_dir="$here/out"
schema=""
args=()

PKG="@microsoft/app-manifest"
PKG_VERSION="1.1.3"
PKG_INTEGRITY="sha512-ensiiCOlAZFBCgj/HpYpV9jCe2Otcj21C3QZo+gYRA45zOMBGWzlDCGblnNHPqTiYesGHJr5xVtPPjCjneswpA=="
SCHEMA_IN_PKG="package/build/json-schemas/teams/v1.17/MicrosoftTeams.schema.json"

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --bot-app-id|--portal-url|--version|--app-id|--name|--relay-host)
      [ $# -ge 2 ] || usage; args+=("$1" "$2"); shift 2 ;;
    --out-dir) [ $# -ge 2 ] || usage; out_dir="$2"; shift 2 ;;
    --schema) [ $# -ge 2 ] || usage; schema="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "build.sh: unknown argument $1" >&2; usage ;;
  esac
done

if [ -z "$schema" ]; then
  cache="${XDG_CACHE_HOME:-$HOME/.cache}/omnifile/teams-schema/$PKG_VERSION"
  schema="$cache/MicrosoftTeams.schema.json"
  if [ ! -f "$schema" ]; then
    command -v npm >/dev/null || { echo "build.sh: needs npm to fetch the schema (or --schema FILE)" >&2; exit 2; }
    mkdir -p "$cache"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    (cd "$tmp" && npm pack "$PKG@$PKG_VERSION" --silent >/dev/null)
    tgz="$(ls "$tmp"/*.tgz)"
    got="sha512-$(python3 -c 'import base64,hashlib,sys; print(base64.b64encode(hashlib.sha512(open(sys.argv[1],"rb").read()).digest()).decode())' "$tgz")"
    if [ "$got" != "$PKG_INTEGRITY" ]; then
      echo "build.sh: $PKG@$PKG_VERSION does not match its pinned sha512" >&2; exit 1
    fi
    tar -xzf "$tgz" -C "$tmp" "$SCHEMA_IN_PKG"
    mv "$tmp/$SCHEMA_IN_PKG" "$schema"
  fi
fi

mkdir -p "$out_dir"
uv --directory "$repo/service" run --quiet python "$here/package.py" build \
  --schema "$schema" --out "$out_dir/omnifile-teams-app.zip" "${args[@]+"${args[@]}"}"
