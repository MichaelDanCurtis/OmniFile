#!/usr/bin/env bash
# Run a command inside a container app and keep the certificate it prints (docs/guide/README.md, 8.3, 8.4).
#
#   docs/guide/tools/enrol-exec.sh --resource-group RG --app APP --command CMD [--then CMD] --pem-out FILE
#
# Runs CMD, then the optional second command, with `az containerapp exec` in one replica of APP (the same
# one for both, since the first may leave a file the second reads). Shows their output, cuts the first
# PEM certificate out of it, checks it is one, and writes it to FILE. The certificates here are public.
# `az containerapp exec` passes the command as words: no shell, no quotes inside CMD.
# If the certificate can't be cut from the output (a terminal that mangles it), copy it by hand: from
# the -----BEGIN CERTIFICATE----- line to the -----END CERTIFICATE----- line, into FILE.
set -euo pipefail

RG='' APP='' CMD='' THEN='' OUT=''
while [ $# -gt 0 ]; do
  case "$1" in
    --resource-group) RG=$2; shift ;;
    --app) APP=$2; shift ;;
    --command) CMD=$2; shift ;;
    --then) THEN=$2; shift ;;
    --pem-out) OUT=$2; shift ;;
    *) echo "usage: enrol-exec.sh --resource-group RG --app APP --command CMD [--then CMD] --pem-out FILE" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$RG" ] && [ -n "$APP" ] && [ -n "$CMD" ] && [ -n "$OUT" ] ||
  { echo "enrol-exec.sh: --resource-group, --app, --command and --pem-out are required" >&2; exit 2; }

replica=$(az containerapp replica list --resource-group "$RG" --name "$APP" --query '[0].name' --output tsv)
[ -n "$replica" ] || { echo "enrol-exec.sh: $APP has no running replica" >&2; exit 1; }
log=$(mktemp)
trap 'rm -f "$log"' EXIT

run() {
  echo "== $APP ($replica): $1"
  az containerapp exec --resource-group "$RG" --name "$APP" --replica "$replica" --command "$1" 2>&1 | tee -a "$log" || true
}
run "$CMD"
[ -z "$THEN" ] || run "$THEN"

# Drop carriage returns and terminal escapes, then keep the first certificate block.
esc=$(printf '\033')
tr -d '\r' <"$log" | sed "s/${esc}\[[0-9;?]*[A-Za-z]//g" |
  awk 'done { next } /-----BEGIN CERTIFICATE-----/ { on = 1 } on { print } /-----END CERTIFICATE-----/ && on { done = 1 }' >"$OUT.new"
if openssl x509 -in "$OUT.new" -noout 2>/dev/null; then
  mv "$OUT.new" "$OUT"
  echo "wrote $OUT: $(openssl x509 -in "$OUT" -noout -subject)"
else
  rm -f "$OUT.new"
  echo "enrol-exec.sh: no certificate could be cut from the output above. Copy it by hand into $OUT" >&2
  exit 1
fi
