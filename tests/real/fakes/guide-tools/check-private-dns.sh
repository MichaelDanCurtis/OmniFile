#!/usr/bin/env bash
# Dry-run stand-in for docs/guide/tools/check-private-dns.sh (tests/real/harness/release.py,
# DRY_TOOLS): the same arguments and output, but the names are resolved by the fake az's DNS
# model (fakecli.py `dns`), never by this machine's resolver.
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: check-private-dns.sh NAME [NAME ...]" >&2; exit 2; }
exec "${REAL_FAKE_PY:?the stand-in runs only inside a harness dry run}" \
  "${REAL_FAKE_REPO:?}/tests/real/fakes/fakecli.py" dns "$@"
