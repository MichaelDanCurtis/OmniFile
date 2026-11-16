#!/usr/bin/env bash
# Every draft in docs/legal starts with the line that says it is a draft, not in effect.
#
#   docs/legal/check.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
want='**DRAFT for counsel review. Not legal advice. Not in effect.**'
fail=0 n=0
for f in "$here"/*.md; do
  n=$((n + 1))
  if [ "$(head -n 1 "$f")" = "$want" ]; then
    echo "ok   $(basename "$f")"
  else
    echo "FAIL $(basename "$f") does not start with: $want"; fail=1
  fi
done
for need in README.md individual-cla.md entity-cla.md dco.md questions-for-counsel.md; do
  [ -f "$here/$need" ] || { echo "FAIL $need is missing"; fail=1; }
done
echo "docs/legal: $n drafts checked"
exit "$fail"
