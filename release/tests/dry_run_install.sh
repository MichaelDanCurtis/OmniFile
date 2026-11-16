#!/usr/bin/env bash
# The release dry run, proved end to end: two releases made by release/runbook.sh --dry-run, and the update
# path installing the second (appliance/tests/signed-update.sh).
#
#   release/tests/dry_run_install.sh --builds DIR --keys DIR --offers DIR --work DIR [--full-gate]
#
# DIR/0.1.0 and DIR/0.2.0 (--builds) are appliance/build.sh outputs of those versions, each built with
# --pubring KEYS/update-ring.pgp and --feed-url http://10.0.2.2:$PORT/omnifile/omnifile-edge/releases/latest/download/
# (PORT defaults to 8746), each with bin/ and canary-bin/ (the gate's binaries for that architecture). They are
# copied into WORK first (APFS clones where it can), because the gate signs the disk's ESP in place. KEYS has
# update.pem and its update-ring.pgp, sb.key and sb.crt, and forged.pem (another P-256 key). --offers has 0.1.0/
# and 0.2.0/, each a complete source offer for its build (appliance/source-offer/source_offer.py).
#
# 1. release/runbook.sh --dry-run for 0.1.0, then for 0.2.0: stamp check, offer check, SBOMs, licences, scan
#    (offline), notes, the R3 gate's quick subset with --allow-partial, the signatures (the update key through the
#    fake az's Key Vault path, the UKI with a local key), the Bicep parameters, and the fake gh "publishing" both
#    into WORK/gh-releases, v0.2.0 as the latest.
# 2. The forged release: v0.2.0's assets with SHA256SUMS.gpg made by forged.pem.
# 3. appliance/tests/signed-update.sh with Secure Boot on and only sb.crt enrolled: the unsigned 0.1.0 doesn't
#    start, the signed 0.1.0 boots, the forged release is refused, and v0.2.0, as the fake gh published it, installs
#    and boots.
# --no-secure-boot: the firmware does not enforce Secure Boot (qemu's edk2 for arm64 under HVF), so signed-update.sh's
# three Secure Boot checks are recorded as pending, and the feed's checks (forged refused, the release installs and
# boots 0.2.0, through GitHub's redirects) must still pass.
# Nothing reaches an account or the network. Exits non-zero on the first failure; WORK/RESULT says what passed.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
BUILDS='' KEYS='' OFFERS='' WORK='' FULL=() NO_SB=0
PORT=${PORT:-8746}
while [ $# -gt 0 ]; do
  case "$1" in
    --builds) BUILDS=$2; shift ;;
    --keys) KEYS=$2; shift ;;
    --offers) OFFERS=$2; shift ;;
    --work) WORK=$2; shift ;;
    --full-gate) FULL=(--full-gate) ;;
    --no-secure-boot) NO_SB=1 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$BUILDS" ] && [ -n "$KEYS" ] && [ -n "$OFFERS" ] && [ -n "$WORK" ] || { sed -n '2,24p' "$0"; exit 2; }
mkdir -p "$WORK" && WORK=$(cd "$WORK" && pwd)
RESULT=$WORK/RESULT
: >"$RESULT"
ok() { echo "ok   $*" | tee -a "$RESULT"; }
fail() { echo "FAIL $*" | tee -a "$RESULT"; exit 1; }
clone() { cp -c "$1" "$2" 2>/dev/null || cp "$1" "$2"; }

# The builds, copied: the gate changes the disk's ESP. The unsigned 0.1.0 disk for the first check, too.
for v in 0.1.0 0.2.0; do
  rm -rf "$WORK/build-$v" && mkdir -p "$WORK/build-$v"
  for f in "$BUILDS/$v"/*; do
    case ${f##*/} in
      feed | bin | canary-bin) cp -R "$f" "$WORK/build-$v/" ;;
      *.raw) clone "$f" "$WORK/build-$v/${f##*/}" ;;
      *) [ -f "$f" ] && cp "$f" "$WORK/build-$v/" ;;
    esac
  done
done
clone "$WORK/build-0.1.0/omnifile-edge_0.1.0.raw" "$WORK/unsigned-0.1.0.raw"

# 1. Two dry-run releases, published into one fake GitHub.
for v in 0.1.0 0.2.0; do
  GH_ROOT_SHARED=$WORK/gh-releases
  if ! "$REPO/release/runbook.sh" --dry-run --version "$v" --out "$WORK/release-$v" \
      --appliance-out "$WORK/build-$v" --keys "$KEYS" --source-offer "$OFFERS/$v" ${FULL[@]+"${FULL[@]}"} \
      >"$WORK/runbook-$v.log" 2>&1; then
    tail -n 30 "$WORK/runbook-$v.log"
    fail "runbook --dry-run $v (log: $WORK/runbook-$v.log)"
  fi
  grep -q '^result=ok$' "$WORK/release-$v/RELEASE-RECORD" || fail "runbook $v: no result=ok"
  mkdir -p "$GH_ROOT_SHARED"
  cp -R "$WORK/release-$v/gh-releases/." "$GH_ROOT_SHARED/"
  ok "runbook --dry-run $v: $(grep -c '^step\.' "$WORK/release-$v/RELEASE-RECORD") steps recorded, result=ok"
done
GOOD=$WORK/gh-releases/omnifile/omnifile-edge/v0.2.0
# Published in order (review on #38): sources, then the feed as a draft on the built commit, then live and latest.
R2=$WORK/release-0.2.0/gh-releases/omnifile/omnifile-edge
[ ! -e "$R2/v0.2.0.draft" ] && [ "$(cat "$R2/LATEST")" = v0.2.0 ] || fail "v0.2.0 is still a draft, or not the latest"
[ "$(cat "$R2/v0.2.0.target")" = "$(sed -n 's/^target=//p' "$WORK/release-0.2.0/RELEASE-RECORD")" ] ||
  fail "v0.2.0's tag is not on the built commit"
awk '/omnifile-gateway-sources/ && / create / {s=NR} /omnifile-edge/ && / create / {f=NR} / edit v0.2.0/ {l=NR}
  END {exit !(s && f && l && s < f && f < l)}' "$WORK/release-0.2.0/gh.log" ||
  fail "publishing order: sources, then the draft feed, then live ($WORK/release-0.2.0/gh.log)"
ok "v0.2.0: sources published first, the feed a draft on $(cut -c1-12 "$R2/v0.2.0.target"), then made the latest"
[ -f "$GOOD/SHA256SUMS.gpg" ] && [ -f "$GOOD/R3-GATE" ] || fail "the fake gh holds no signed v0.2.0 feed"
for s in edge service relay portal images appliance tools; do
  [ -f "$GOOD/omnifile-$s-0.2.0.cdx.json" ] || fail "v0.2.0 has no $s SBOM"
done
[ -f "$GOOD/scan.json" ] && [ -f "$GOOD/omnifile-images-0.2.0.bicepparam" ] || fail "v0.2.0 lacks the scan or the parameters"
[ -d "$WORK/release-0.2.0/gh-releases/dry-run-owner/omnifile-gateway-sources/v0.2.0" ] || fail "no sources release"
ok "v0.2.0 published (fake gh): signed feed, R3-GATE, 7 SBOMs, scan.json, the parameters, and the sources release"
rm -rf "$WORK/gnupg" && mkdir -m 700 "$WORK/gnupg"
(cd "$GOOD" && gpg --homedir "$WORK/gnupg" --batch --no-default-keyring --keyring "$KEYS/update-ring.pgp" \
  --verify SHA256SUMS.gpg SHA256SUMS) >"$WORK/gpg.log" 2>&1 || fail "gpg --verify of v0.2.0 ($WORK/gpg.log)"
ok "gpg --verify: v0.2.0's SHA256SUMS is signed by the update key in the image's keyring"

# 2. The forged release.
BAD=$WORK/forged-0.2.0
rm -rf "$BAD" && cp -R "$GOOD" "$BAD"
python3 "$REPO/appliance/pgp_kv.py" pubkey --signer "fake:$KEYS/forged.pem" --created 1792540800 \
  --uid "Forged <forged@omnifile.invalid>" --out "$WORK/forged-ring.pgp"
python3 "$REPO/appliance/pgp_kv.py" sign --signer "fake:$KEYS/forged.pem" --created 1792540800 \
  --ring "$WORK/forged-ring.pgp" --in "$BAD/SHA256SUMS" --out "$BAD/SHA256SUMS.gpg"
ok "forged release: v0.2.0 with SHA256SUMS.gpg from another key"

# 3. The update path installs what was published.
SU=$WORK/signed-update DISK=$WORK/build-0.1.0/omnifile-edge_0.1.0.raw UNSIGNED=$WORK/unsigned-0.1.0.raw
rc=0
WORK=$SU PORT=$PORT "$REPO/appliance/tests/signed-update.sh" --disk "$DISK" --unsigned-disk "$UNSIGNED" \
  --feed "$GOOD" --bad-feed "$BAD" --db-cert "$KEYS/sb.crt" >"$SU.log" 2>&1 || rc=$?
cat "$SU.log"
if [ $NO_SB = 0 ]; then
  [ $rc = 0 ] || fail "signed-update.sh: $(grep -c '^ok ' "$SU.log") passed; $(grep '^FAIL' "$SU.log" | tr '\n' ';')"
  ok "signed-update.sh: $(grep -c '^ok ' "$SU.log")/$(grep -cE '^(ok|FAIL) ' "$SU.log")"
else
  for c in "the forged release is refused, nothing installed" "the signed release installs" "ends cleanly" \
    "downloads followed GitHub's redirects"; do
    grep -qxF "ok   $c" "$SU.log" || fail "signed-update.sh: $c"
    ok "signed-update.sh: $c"
  done
  grep -Eq '^SB boot=2 version=0\.2\.0 ' "$SU.log" || fail "signed-update.sh: 0.2.0 did not boot after the update"
  ok "signed-update.sh: the box rebooted into 0.2.0"
  echo "pending (no Secure Boot in this firmware): the unsigned disk refused; SecureBoot=1 on 0.1.0 and 0.2.0" | tee -a "$RESULT"
fi
echo "dry_run_install: all passed ($RESULT)"
