#!/usr/bin/env bash
# Lint the customer guide (docs/guide/README.md) against the repository and the compiled Bicep.
#
#   docs/guide/check.sh [GUIDE.md]
#
# Compiles deploy/azure/main.bicep to JSON with the Bicep CLI (`bicep`, or `az bicep` when only the Azure
# CLI is there), offline, then runs docs/guide/lint/guide_lint.py: every step's fence, every OMNI_ value,
# every Bicep parameter and output, every repo path, script, flag and subcommand, every portal label and
# every setup answer the guide names must resolve (the docstring there lists the rules). It also builds
# docs/guide/customer.bicepparam for phase 1 and phase 2 with the example values, and runs shellcheck on
# the guide's scripts when shellcheck is installed. Nothing is pending: every name resolves against this
# checkout. Never signs in to Azure; changes nothing.
#
# Exit 0 when everything resolves, 1 otherwise.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
guide="${1:-$here/README.md}"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

if command -v bicep >/dev/null 2>&1; then
  bicep build "$repo/deploy/azure/main.bicep" --outfile "$out/main.json" >"$out/build.log" 2>&1 ||
    { cat "$out/build.log"; echo "check.sh: main.bicep doesn't build"; exit 1; }
  build_params="bicep build-params"
elif command -v az >/dev/null 2>&1; then
  az bicep build --file "$repo/deploy/azure/main.bicep" --outfile "$out/main.json" >"$out/build.log" 2>&1 ||
    { cat "$out/build.log"; echo "check.sh: main.bicep doesn't build"; exit 1; }
  build_params="az bicep build-params --file"
else
  echo "check.sh: needs the Bicep CLI (brew install azure/bicep/bicep) or the Azure CLI" >&2
  exit 2
fi

fail=0
python3 -I "$here/lint/guide_lint.py" --repo "$repo" --main-json "$out/main.json" \
  --build-params "$build_params" "$guide" || fail=1

if command -v shellcheck >/dev/null 2>&1; then
  scripts=()
  while IFS= read -r f; do scripts+=("$f"); done < <(find "$here" -name '*.sh' -not -path '*/fixtures/*' | sort)
  if shellcheck -x "${scripts[@]}"; then
    echo "shellcheck: ok, ${#scripts[@]} scripts"
  else
    echo "shellcheck: problems above"; fail=1
  fi
else
  echo "shellcheck: not installed, skipped"
fi
exit "$fail"
