#!/usr/bin/env bash
# Run one step of the customer guide exactly as written (docs/guide/README.md, "How to read this guide").
#
#   docs/guide/tools/run-step.sh N            run workstation step N, from the release folder
#   docs/guide/tools/run-step.sh --show N     print step N, wherever it runs
#
# A workstation step runs with bash, stopping at its first error (bash -euo pipefail), in the current
# folder, which should be the release folder. A step that runs elsewhere (the filer, the portal, a
# hypervisor) is printed with where it runs, and not run.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GUIDE=${OMNI_GUIDE:-$HERE/../README.md}
show=0
[ "${1:-}" = --show ] && { show=1; shift; }
[[ ${1:-} =~ ^[0-9]+$ ]] || { echo "usage: run-step.sh [--show] N" >&2; exit 2; }
n=$1
block=$(mktemp)
trap 'rm -f "$block"' EXIT
where=$(python3 -I - "$GUIDE" "$n" "$block" <<'PY'
import re, sys
guide, n, out = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(guide, encoding="utf-8").read().splitlines()
for i, line in enumerate(lines):
    m = re.match(r"^```(\w+) (.*)$", line)
    if not m or not re.search(r"(^| )guide-step=%s( |$)" % n, m.group(2)):
        continue
    attrs = dict(w.partition("=")[::2] for w in m.group(2).split())
    body = []
    for line in lines[i + 1:]:
        if line.startswith("```"):
            break
        body.append(line)
    open(out, "w", encoding="utf-8").write("\n".join(body) + "\n")
    print(m.group(1), attrs.get("on", ""), attrs.get("manual", ""))
    sys.exit(0)
sys.exit(f"run-step.sh: the guide has no step {n}")
PY
)
read -r lang on manual <<<"$where"
if [ "$show" = 1 ] || [ "$on" != workstation ] || [ "$lang" != bash ]; then
  echo "== step $n runs on: $on${manual:+ (needs a person)}"
  cat "$block"
  exit 0
fi
echo "== step $n (workstation)${manual:+: it needs you, as the guide says}"
exec bash -euo pipefail "$block"
