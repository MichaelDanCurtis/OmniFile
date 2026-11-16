#!/usr/bin/env bash
# M4 exit test (docs/milestones/M4.md "Exit", the part against sim and the fakes), on one
# machine: a Mac, a dev VM or a CI runner. No edge and no kernel features: this script
# plays the edge over the control API (tests/e2e/m4.py).
#
#   OMNIFILE_DATABASE_URL=postgresql://user:pw@host:port/postgres tests/e2e/m4_exit_test.sh
#
# Steps:
#   1. a fresh database on that server (dropped at the end unless E2E_KEEP=1);
#   2. the service and relay environments (uv), the portal build and Playwright;
#   3. dev/m4/stack.py up: sim_http (virtual clock), api (mutual TLS), web (TLS, dev OIDC
#      issuer, portal/dist), worker, the fake Graph and Bot Connector, the real relay;
#   4. tests/e2e/m4.py seed: an edge (certificate from the dev CA), a share, archived and
#      versioned files as sim objects;
#   5. portal/e2e (Playwright, chromium) against the real web app;
#   6. tests/e2e/m4.py run: Teams, relay, recalls, restores, policies, proposals;
#   7. results: $E2E_RESULTS (default tests/e2e/results/m4-latest.md).
#
# Environment: M4_WORK (stack work dir, default a new temp dir), M4_PORT_BASE (18400),
# E2E_KEEP=1 (leave the stack and database up), M4_SKIP_BUILD=1 (serve portal/dist, or
# M4_STATIC_DIR, as built),
# E2E_REV / E2E_HEAD (for the results header).
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
: "${OMNIFILE_DATABASE_URL:?set OMNIFILE_DATABASE_URL to a Postgres 16+ server (any database on it)}"
export M4_WORK=${M4_WORK:-$(mktemp -d "${TMPDIR:-/tmp}/omnifile-m4.XXXXXX")}
RESULTS=${E2E_RESULTS:-$REPO/tests/e2e/results/m4-latest.md}
PY=${M4_SERVICE_PY:-$REPO/service/.venv/bin/python}
say() { printf '[m4] %s\n' "$*"; }
die() { printf '[m4] FAIL: %s\n' "$*" >&2; exit 1; }

# The portal's toolchain, before anything else: portal/package.json says which (`engines`,
# `packageManager`; `.nvmrc` for nvm). Another npm earlier on PATH is easy to miss, and an
# npm ci that fails must say so, not end the run with nothing on screen.
npm_min=$(node -p 'require(process.argv[1]).engines.npm.replace(/^>=/, "")' \
  "$REPO/portal/package.json") || die "node can't read portal/package.json ($(command -v node))"
npm_have=$(npm --version) || die "no npm on PATH"
node -e 'const [a, b] = process.argv.slice(1).map((v) => v.split(".").map(Number));
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) process.exit(a[i] > b[i] ? 0 : 1);' \
  "$npm_have" "$npm_min" \
  || die "npm $npm_have at $(command -v npm) is older than the portal's npm >=$npm_min." \
    "Put a newer one first on PATH (nvm use, from the repository root: .nvmrc), then run again."
npm_ci() {
  local log
  log="$M4_WORK/npm-ci-$(basename "$1").log"
  if ! (cd "$1" && npm ci --no-audit --no-fund >"$log" 2>&1); then
    sed -n '1,20p' "$log" >&2
    die "npm ci failed in ${1#"$REPO"/} with npm $npm_have at $(command -v npm): the lines" \
      "above, all of it in $log. The portal's npm is in portal/package.json (packageManager):" \
      "nvm use, from the repository root, then run again."
  fi
}

# 2. Environments --------------------------------------------------------------------------
say "service and relay environments"
(cd "$REPO/service" && uv sync --frozen --quiet)
(cd "$REPO/relay" && uv sync --frozen --quiet --no-dev)
if [ "${M4_SKIP_BUILD:-}" != 1 ]; then
  # Into the work directory, not portal/dist: a build or clean in the checkout meanwhile
  # can't pull the files from under the running web app.
  say "portal build"
  export M4_STATIC_DIR=$M4_WORK/portal-dist
  npm_ci "$REPO/portal"
  (cd "$REPO/portal" && npx vite build --outDir "$M4_STATIC_DIR" --emptyOutDir --logLevel warn) \
    || die "the portal build failed (the lines above)"
fi
say "Playwright (chromium)"
npm_ci "$REPO/portal/e2e"
(cd "$REPO/portal/e2e" && npx playwright install chromium >/dev/null) \
  || die "Playwright couldn't install chromium (the lines above)"

# 1. A fresh database ------------------------------------------------------------------------
DB_NAME=omnifile_m4_$(date +%s)_$$
SERVER_URL=$OMNIFILE_DATABASE_URL
export OMNIFILE_DATABASE_URL=$("$PY" - "$SERVER_URL" "$DB_NAME" <<'EOF'
import sys
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
server, name = sys.argv[1:]
with psycopg.connect(server, autocommit=True) as c:
    c.execute(f'CREATE DATABASE "{name}"')
params = conninfo_to_dict(server)
params["dbname"] = name
print(make_conninfo(**params))
EOF
)
say "database $DB_NAME; work dir $M4_WORK"

cleanup() {
  rc=$?
  if [ "${E2E_KEEP:-}" = 1 ]; then
    say "E2E_KEEP=1: the stack and database $DB_NAME stay up (dev/m4/stack.py down)"
  else
    "$PY" "$REPO/dev/m4/stack.py" down >/dev/null 2>&1 || true
    "$PY" - "$SERVER_URL" "$DB_NAME" <<'EOF' || true
import sys
import psycopg
server, name = sys.argv[1:]
with psycopg.connect(server, autocommit=True) as c:
    c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
EOF
  fi
  exit $rc
}
trap cleanup EXIT

# 3-4. Stack and seed ------------------------------------------------------------------------
say "stack up"
"$PY" "$REPO/dev/m4/stack.py" up >/dev/null
say "seed"
"$PY" "$REPO/tests/e2e/m4.py" seed >/dev/null

# 5. Browser tests (before the service-side run moves the clock and the policies) -------------
say "portal/e2e (Playwright, chromium)"
pw_rc=0
(cd "$REPO/portal/e2e" && npx playwright test) || pw_rc=$?

# 6. Service side ---------------------------------------------------------------------------
say "tests/e2e/m4.py run"
svc_rc=0
E2E_RESULTS="$M4_WORK/m4-service.md" "$PY" "$REPO/tests/e2e/m4.py" run || svc_rc=$?

# 7. Results --------------------------------------------------------------------------------
"$PY" - "$M4_WORK" "$RESULTS" "${E2E_REV:-$(git -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)}" "${E2E_HEAD:-$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)}" <<'EOF'
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

work, out, rev, head = sys.argv[1:]
work = Path(work)
lines = ["# M4 exit test (sim, fakes, real relay, Playwright)", "",
         f"- When: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
         f"- Revision: `{rev}` (`{head}`)",
         f"- Machine: {platform.system()} {platform.release()} ({platform.machine()})", ""]
svc = work / "m4-service.md"
lines += svc.read_text().splitlines() if svc.exists() else ["Service side: no results"]
lines.append("")
report = work / "playwright" / "results.json"
if report.exists():
    data = json.loads(report.read_text())
    rows = []

    def walk(suite, prefix=""):
        for spec in suite.get("specs", []):
            for t in spec.get("tests", []):
                res = t.get("results", [{}])[-1]
                rows.append((f"{suite.get('title', '')}: {spec['title']}", res.get("status"),
                             res.get("duration", 0) / 1000))
        for s in suite.get("suites", []):
            walk(s)

    for s in data.get("suites", []):
        walk(s)
    ok = sum(1 for _, st, _ in rows if st == "passed")
    lines += [f"### Browser (portal/e2e, Playwright chromium): {ok}/{len(rows)} passed", ""]
    lines += [f"- {'PASS' if st == 'passed' else st.upper()} {name} ({dt:.1f} s)"
              for name, st, dt in rows]
else:
    lines.append("### Browser: no Playwright report")
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_text("\n".join(lines) + "\n")
print("\n".join(lines))
EOF
say "results: $RESULTS"
[ "$pw_rc" -eq 0 ] && [ "$svc_rc" -eq 0 ]
