#!/usr/bin/env bash
# Record a value the customer guide's later steps need, in omnifile.state.env (docs/guide/README.md).
#
#   docs/guide/tools/state.sh set NAME VALUE      record VALUE as NAME
#   docs/guide/tools/state.sh set-file NAME FILE  record the contents of FILE as NAME (a PEM, a JSON document)
#   docs/guide/tools/state.sh get NAME            print NAME's recorded value
#
# The file is in the current directory (the release folder). Each value is one `export NAME=...` line,
# quoted so that the shell reads it back exactly; setting a name again replaces its line. Names must be
# OMNI_ followed by capitals, digits and underscores. Nothing secret belongs here.
set -euo pipefail

STATE=${OMNI_STATE_FILE:-./omnifile.state.env}

usage() {
  echo "usage: state.sh set NAME VALUE | set-file NAME FILE | get NAME" >&2
  exit 2
}

check_name() {
  [[ $1 =~ ^OMNI_[A-Z0-9_]+$ ]] || { echo "state.sh: $1 is not a name the guide uses (OMNI_...)" >&2; exit 2; }
}

put() {
  local name=$1 value=$2 tmp
  check_name "$name"
  touch "$STATE"
  tmp=$(mktemp "$STATE.XXXXXX")
  grep -v "^export $name=" "$STATE" >"$tmp" || true
  printf 'export %s=%q\n' "$name" "$value" >>"$tmp"
  mv "$tmp" "$STATE"
  echo "recorded $name"
}

[ $# -ge 2 ] || usage
case "$1" in
  set)
    [ $# -eq 3 ] || usage
    put "$2" "$3"
    ;;
  set-file)
    [ $# -eq 3 ] || usage
    [ -s "$3" ] || { echo "state.sh: $3 is missing or empty" >&2; exit 1; }
    put "$2" "$(cat "$3")"
    ;;
  get)
    [ $# -eq 2 ] || usage
    check_name "$2"
    [ -f "$STATE" ] || { echo "state.sh: no $STATE yet" >&2; exit 1; }
    # shellcheck disable=SC1090
    (. "$STATE" && v="${!2-}" && [ -n "$v" ] && printf '%s\n' "$v") ||
      { echo "state.sh: $2 is not recorded" >&2; exit 1; }
    ;;
  *) usage ;;
esac
