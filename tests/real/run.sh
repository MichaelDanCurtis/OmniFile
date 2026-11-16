#!/usr/bin/env bash
# The real-cloud harness: one command proves what only a real Azure subscription, AWS account
# or Microsoft 365 tenant can, then removes everything it made.
#
#   tests/real/run.sh PHASE --cloud azure|aws|tenant [options]
#
# PHASE:
#   plan     check the signed-in account against the allow-list, fix the run id and expiry,
#            and print the what-if (Azure: deploy/azure/whatif.sh) or the harness's own plan
#            (AWS, tenant) with its digest. Azure creates one empty, tagged resource group,
#            which what-if needs.
#   up       create everything, but only with --approve <digest> equal to a fresh what-if.
#            Azure runs the customer guide's guide-step blocks exactly as written.
#   exit     the M6 exit (Azure) or the M4 exit on a real tenant (tenant), through public
#            interfaces only: NFS, SMB, the portal, Teams.
#   checks   every automated line of docs/milestones/real-*-checks.md for this cloud; lists
#            the manual ones.
#   collect  write results.json and tick the real-* docs (a dry run ticks copies only).
#   down     tear down by tag (WORM policies first) and prove nothing tagged remains.
#   all      plan, up, exit, checks, collect, then down, which always runs once anything
#            was made, even after a failure.
#
# Options:
#   --dry-run            run against the fakes only: PATH shims for az, aws, gh, ssh, scp,
#                        docker and node (tests/real/fakes/bin), moto for AWS, a fixed clock
#                        and run id. Refuses to start if any of those tools is not the shim.
#   --run-id ID          reuse a run (its state is in OUT/state.json). plan makes a new one.
#   --approve DIGEST     the digest plan printed (sha256:<hex>). Repeat it for each later
#                        deploy's own what-if (phase 2, the AWS settings): up stops at that
#                        gate and prints its digest. A dry-run `all` without it approves the
#                        digests it computed itself, and says so; a real run never.
#   --out DIR is refused inside the repository, except under tests/real/out (gitignored).
#   --out DIR            where the run's state, logs and evidence go (default
#                        tests/real/out/<run id>).
#   --allow-list FILE    dry run only (tests); a real run reads tests/real/allow-list.local.
#   --guide DIR          the customer guide (default docs/guide; a dry run falls back to the
#                        fixture in tests/real/fakes/guide).
#   --appliance FILE     the appliance qcow2 the Azure run boots (default: the newest
#                        appliance/build/out/omnifile-edge_*.qcow2).
#   --azure-run ID       (tenant) the Azure run whose deployment the tenant run uses.
#   --ttl-hours N        expiry tag, now + N hours (default 12, at most max-ttl-hours).
#   --scope cheap|full   (aws) cheap: small objects only, no large-object check; fixed at
#                        plan and in its digest (default full).
#   --inject-failure S   fail at step S (a step name from the log), to prove teardown.
#   --expired            (down) also tear down every harness run whose expiry has passed.
#
# Exit codes: 0 ok; 1 a step or check failed; 2 usage; 3 awaiting approval (or the digest
# changed); 4 refused (allow-list, a tool that is not the fake in a dry run).
#
# Needs: python (through uv: tests/real/pyproject.toml), and for a real run az, aws, ssh, the
# Bicep CLI and node with portal/e2e's Playwright. The owner signs in to az and aws first; the
# harness never signs in and never writes the allow-list.
#
# Its own lanes: uv run --project tests/real pytest tests/real/tests -q, and the lint, pinned
# (release/tools.toml): uvx ruff@0.8.6 check tests/real && uvx ruff@0.8.6 format --check tests/real
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${REAL_HARNESS_PY:-}" ]; then
  exec "$REAL_HARNESS_PY" -m harness "$@"
fi
exec uv run --quiet --project "$HERE" python -m harness "$@"
