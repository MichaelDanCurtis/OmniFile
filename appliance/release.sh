#!/usr/bin/env bash
# Sign an appliance update feed, but only once the new image's kernel has passed the
# smb-guard A-suite and the kernel canary (risk R3, docs/BUILD-PLAN.md; appliance/README.md
# "Releasing"). This is the only script that signs a feed: appliance/build.sh writes it
# unsigned, and a box refuses an unsigned feed.
#
#   appliance/release.sh --out DIR --bin-dir DIR --canary-bin-dir DIR --source-offer DIR
#                        [--signer kv:<key id>] [--uki-signer kv:<vault>:<key>] [--uki-cert F]
#                        [--a-suite-only a01,a05] [--canary-only k1] [--allow-partial]
#
# Signing (decisions 47 and 48; release.conf has the defaults): after the gate, the UKI in the
# feed and the disk's ESP (UKI, systemd-boot, the fallback loader) are signed for Secure Boot
# with OmniFile's Key Vault key (sbsign through the e_akv OpenSSL engine), and SHA256SUMS is
# signed by the Key Vault update key (pgp_kv.py: Key Vault signs only a digest, the OpenPGP
# packet is built here, and gpg/systemd-sysupdate verify it against the image's keyring).
# Nothing is uploaded: the release's assets are laid out in DIR/release-assets with the
# `gh release create` command that publishes them on RELEASE_REPO. fake:<key.pem> signers
# are for test feeds (--allow-partial) and dry runs only.
#   appliance/release.sh --list-checks [--a-suite-only ...] [--canary-only ...]
#
# DIR (--out) is one build's output (appliance/build.sh --out): omnifile-edge_V.raw and
# feed/. --bin-dir holds the x86-64 omnifile-smbguard-harness, smbguard-tool and
# omnifile-guardctl (cargo build --release -p omnifile-smbguard); --canary-bin-dir holds
# omnifile-guard and omnitool (spikes/filer/guard). --source-offer is this release's GPL and
# LGPL source collection (appliance/source-offer/source_offer.py --release V, from the
# build's omnifile-edge_V.manifest): a release is not signed until its MANIFEST.json says it
# is complete, for this version, and its SHA256SUMS still match. A test feed
# (--allow-partial) may go without one.
#
# The gate boots the image being released in qemu (appliance/tests/vm.sh), so the suites run
# on exactly its kernel, systemd, Samba and read-only /usr. The test trees and binaries
# reach the guest as a second disk holding a tar stream; nothing is added to the image.
# Inside, it runs ci/kernel-canary/run.sh and then the A-suite
# (edge/crates/omnifile-smbguard/tests/a-suite/run.sh). The feed is signed only when:
#   - the guest reports the version of the image (the kernel under test is that image's);
#   - both suites exit 0;
#   - every check of both suites (or of the --*-only lists) reports PASS in its summary,
#     so a suite that ran nothing passes nothing.
#   - the feed is the booted disk's (feed_binding.py: the /usr and verity partitions' UUIDs
#     and bytes, and the UKI the guest booted has the feed UKI's SHA-256), so a feed from
#     another build of the same version can't borrow this one's pass.
# Otherwise it exits 1, removes any SHA256SUMS.gpg, and says which check failed.
#
# --a-suite-only / --canary-only run a subset (to try the gate quickly). A subset is never
# enough to sign unless --allow-partial is also given, which records it in R3-GATE and is
# for test feeds only (a throwaway key).
#
# On success the feed gets R3-GATE (the kernel, versions, checks, times and source commit),
# listed in SHA256SUMS, so the signature covers the gate's own record.
#
# The full suites take hours under TCG (the A-suite's A6 kill loops and A8's 17,000 readers):
# run releases where /dev/kvm is there. Needs qemu-system-x86_64, OVMF, sha256sum, python3,
# zstd, sbsigntool, mtools and openssl; az (signed in as the release identity) for kv: signers.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=tests/vm.sh
. "$HERE/tests/vm.sh"
# shellcheck source=release.conf
. "$HERE/release.conf"
OUT='' SIGNER='' UKI_SIGNER='' BIN_DIR='' CANARY_BIN='' A_ONLY='' K_ONLY='' PARTIAL=0 LIST=0 OFFER=''
# Every check's time budget is multiplied by this in the guest (A-suite SG_BUDGET_SCALE, canary
# CANARY_BUDGET_SCALE): 1 with KVM or HVF; an x86 guest under TCG needs several times more.
BUDGET_SCALE=${R3_BUDGET_SCALE:-1}
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift ;;
    --signer) SIGNER=$2; shift ;;
    --uki-signer) UKI_SIGNER=$2; shift ;;
    --uki-cert) UKI_CERT=$2; shift ;;
    --update-ring) UPDATE_RING=$2; shift ;;
    --uki-key-id) UKI_KEY_ID=$2; shift ;;
    --key-created) UPDATE_KEY_CREATED=$2; shift ;;
    --github-repo) RELEASE_REPO=$2; shift ;;
    --sign-key) echo "--sign-key (gpg) is gone: the feed is signed by Key Vault (--signer kv:<key id>, release.conf)" >&2; exit 2 ;;
    --bin-dir) BIN_DIR=$2; shift ;;
    --canary-bin-dir) CANARY_BIN=$2; shift ;;
    --a-suite-only) A_ONLY=$2; shift ;;
    --canary-only) K_ONLY=$2; shift ;;
    --allow-partial) PARTIAL=1 ;;
    --source-offer) OFFER=$2; shift ;;
    --list-checks) LIST=1 ;;
    --budget-scale) BUDGET_SCALE=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
log() { printf '[release] %s\n' "$*"; }
refuse() {
  printf '[release] NOT SIGNED: %s\n' "$*" >&2
  [ -z "$OUT" ] || rm -f "$OUT/feed/SHA256SUMS.gpg"
  exit 1
}
# Scaled budgets (an emulated guest) prove less than the gate's own limits: test feeds only.
awk -v s="$BUDGET_SCALE" 'BEGIN { exit !(s ~ /^[0-9]+(\.[0-9]+)?$/ && s + 0 > 0) }' ||
  refuse "--budget-scale $BUDGET_SCALE: a positive number"
if [ "$BUDGET_SCALE" != 1 ] && [ $PARTIAL = 0 ]; then
  refuse "--budget-scale $BUDGET_SCALE loosens the gate's time limits, so it never signs a release; --allow-partial is for test feeds"
fi
# Every check each suite has, as its run.sh lists them, or the subset asked for.
A_ALL=$(awk '/^CHECKS="/ { f = 1; sub(/^CHECKS="/, "") }
  f { done = ($0 ~ /"[[:space:]]*$/); gsub(/["\\]/, ""); n = split($0, w, " ")
      for (i = 1; i <= n; i++) { sub(/:.*/, "", w[i]); if (w[i] != "") print w[i] }
      if (done) exit }' "$REPO/edge/crates/omnifile-smbguard/tests/a-suite/run.sh")
K_ALL=$(sed -n 's/^ *"\(k[0-9]*_[a-z_]*\)|.*/\1/p' "$REPO/ci/kernel-canary/run.sh")
pick() { # pick ALL ONLY: the checks of ALL whose key (before _) is in ONLY, or all
  local c
  for c in $1; do
    if [ -z "$2" ] || grep -qw "${c%%_*}" <<<"${2//,/ }"; then echo "$c"; fi
  done
}
A_WANT=$(pick "$A_ALL" "$A_ONLY")
K_WANT=$(pick "$K_ALL" "$K_ONLY")
[ -n "$A_WANT" ] || refuse "no A-suite check matches --a-suite-only $A_ONLY"
[ -n "$K_WANT" ] || refuse "no kernel canary check matches --canary-only $K_ONLY"
if [ $LIST = 1 ]; then
  echo "a-suite: $(echo $A_WANT)"
  echo "canary: $(echo $K_WANT)"
  exit 0
fi
# release.conf's paths are the repository's.
case $UPDATE_RING in /*) ;; *) [ -f "$UPDATE_RING" ] || UPDATE_RING=$REPO/$UPDATE_RING ;; esac
case $UKI_CERT in /*) ;; *) [ -f "$UKI_CERT" ] || UKI_CERT=$REPO/$UKI_CERT ;; esac
SIGNER=${SIGNER:-$UPDATE_KEY}
UKI_SIGNER=${UKI_SIGNER:-$UKI_KEY}
[ -n "$OUT" ] && [ -n "$SIGNER" ] && [ -n "$BIN_DIR" ] && [ -n "$CANARY_BIN" ] || { sed -n '2,13p' "$0"; exit 2; }
OUT=$(cd "$OUT" && pwd)
FEED=$OUT/feed
WORK=${WORK:-$OUT/r3-gate}

# ---- the build: one image, its feed, SHA256SUMS that still match ---------------------------
shopt -s nullglob
disks=()
for f in "$OUT"/omnifile-edge_*.raw; do
  # The disk, not the split /usr and verity partitions (omnifile-edge_V.usr-*.raw).
  case ${f##*/} in *.usr-*) ;; *) disks+=("$f") ;; esac
done
shopt -u nullglob
[ ${#disks[@]} = 1 ] || refuse "expected one omnifile-edge_V.raw in $OUT, found ${#disks[@]}"
DISK=${disks[0]}
V=${DISK##*/omnifile-edge_}; V=${V%.raw}
[ -f "$FEED/omnifile-edge_$V.efi" ] || refuse "the feed has no omnifile-edge_$V.efi: not this build's feed"
rm -f "$FEED/SHA256SUMS.gpg" "$FEED/R3-GATE"
(cd "$FEED" && grep -v ' R3-GATE$' SHA256SUMS >SHA256SUMS.files && sha256sum -c --quiet SHA256SUMS.files) ||
  refuse "the feed's files do not match its SHA256SUMS"
# The GPL source offer (#26): every release ships one, collected from this build's manifest.
[ -f "$OUT/omnifile-edge_$V.manifest" ] ||
  refuse "no omnifile-edge_$V.manifest in $OUT: the source offer is collected from it (ManifestFormat=json)"
OFFER_RECORD="none (test feed)"
if [ -n "$OFFER" ]; then
  [ -f "$OFFER/MANIFEST.json" ] || refuse "no MANIFEST.json in $OFFER (appliance/source-offer/source_offer.py)"
  # Complete, and the very offer written into the image: its release, archive snapshot,
  # sources repository and architecture (BUILD-INFO) match the bundle's MANIFEST.json.
  python3 "$HERE/offer_binding.py" check "$OUT/BUILD-INFO" "$OFFER/MANIFEST.json" "$V" 2>"$OUT/.offer.err" ||
    refuse "the source offer in $OFFER is not the one in this image: $(tr '\n' ';' <"$OUT/.offer.err")"
  rm -f "$OUT/.offer.err"
  (cd "$OFFER" && sha256sum -c --quiet SHA256SUMS) || refuse "the source offer's files do not match its SHA256SUMS"
  OFFER_RECORD="$(sha256sum "$OFFER/SHA256SUMS" | cut -d' ' -f1) ($(basename "$OFFER"))"
elif [ $PARTIAL = 0 ]; then
  refuse "a release needs its source offer (--source-offer DIR, appliance/README.md \"Source offer\")"
fi
# The feed must be this disk's (Codex on #28): the /usr and verity partitions the gate boots
# have the feed's partition UUIDs and bytes, and the feed has one UKI for V, whose SHA-256
# the guest's booted UKI must then match.
BINDING=$(python3 "$HERE/feed_binding.py" check "$DISK" "$FEED" "$V" 2>"$OUT/.binding.err") ||
  refuse "$(cat "$OUT/.binding.err")"
rm -f "$OUT/.binding.err"
UKI_SHA=$(sed -n 's/^uki_sha256=//p' <<<"$BINDING")
log "the feed is this disk's: $(tr '\n' ' ' <<<"$BINDING")"
for b in omnifile-smbguard-harness smbguard-tool omnifile-guardctl; do
  [ -x "$BIN_DIR/$b" ] || refuse "missing $BIN_DIR/$b"
done
for b in omnifile-guard omnitool; do
  [ -x "$CANARY_BIN/$b" ] || refuse "missing $CANARY_BIN/$b"
done
case $SIGNER in kv:*) ;; fake:*) [ $PARTIAL = 1 ] || refuse "a fake signer ($SIGNER) signs test feeds only (--allow-partial)" ;; *) refuse "--signer $SIGNER: kv:<key id> or fake:<key.pem>" ;; esac
case $UKI_SIGNER in kv:*) ;; fake:*) [ $PARTIAL = 1 ] || refuse "a fake UKI signer signs test feeds only (--allow-partial)" ;; *) refuse "--uki-signer $UKI_SIGNER: kv:<vault>:<key> or fake:<key.pem>" ;; esac
[ -f "$UKI_CERT" ] || refuse "no Secure Boot certificate $UKI_CERT (--uki-cert; release.conf UKI_CERT)"
# The keys must be the ones installed boxes trust (Codex on #31): the update key, as Key Vault
# holds it now, must be in the keyring images embed, and the Secure Boot key must be the key
# of the enrolled certificate. A rotated key fails here, before anything is booted or signed.
[ -f "$UPDATE_RING" ] || refuse "no update keyring $UPDATE_RING (--update-ring; release.conf UPDATE_RING)"
SIGNER_FPR=$(python3 "$HERE/pgp_kv.py" fingerprint --signer "$SIGNER" --created "$UPDATE_KEY_CREATED") ||
  refuse "could not read the update key ($SIGNER)"
python3 "$HERE/pgp_kv.py" ring-fingerprints "$UPDATE_RING" | grep -qx "$SIGNER_FPR" ||
  refuse "the update key $SIGNER ($SIGNER_FPR) is not in $UPDATE_RING, the keyring images embed: a rotated key needs a new keyring and a new image first (docs/design/secure-boot.md, \"Rotating a key\")"
case $UKI_SIGNER in
  kv:*) UKI_CHECK=kv:$UKI_KEY_ID ;;
  *) UKI_CHECK=$UKI_SIGNER ;;
esac
python3 "$HERE/pgp_kv.py" check-cert --signer "$UKI_CHECK" --cert "$UKI_CERT" >/dev/null ||
  refuse "the Secure Boot key is not the key of $UKI_CERT, the certificate clients enrolled"
for t in sbsign sbverify sbattach mcopy python3; do
  command -v $t >/dev/null || refuse "$t is needed to sign (sbsigntool, mtools)"
done
if { [ -n "$A_ONLY" ] || [ -n "$K_ONLY" ]; } && [ $PARTIAL = 0 ]; then
  refuse "a subset of the suites (--a-suite-only/--canary-only) never signs a release; --allow-partial is for test feeds"
fi

log "version $V; A-suite: $(echo $A_WANT); canary: $(echo $K_WANT)"

# ---- the payload: the suites and their binaries, as a tar stream on a second disk ---------
mkdir -p "$WORK"
STAGE=$WORK/payload
rm -rf "$STAGE" && mkdir -p "$STAGE/bin" "$STAGE/canary-bin"
(cd "$REPO" && tar -cf - ci/kernel-canary edge/crates/omnifile-smbguard/tests/a-suite edge/systemd \
  spikes/filer/guard/systemd samba-vfs/install.sh fsal/tests) | tar -xf - -C "$STAGE"
install -m 0755 "$BIN_DIR/omnifile-smbguard-harness" "$BIN_DIR/smbguard-tool" "$BIN_DIR/omnifile-guardctl" "$STAGE/bin/"
install -m 0755 "$CANARY_BIN/omnifile-guard" "$CANARY_BIN/omnitool" "$STAGE/canary-bin/"
tar -cf "$WORK/payload.tar" -C "$STAGE" .
# qemu wants whole sectors; tar pads to 10 KiB records already, this is belt and braces.
truncate -s %1M "$WORK/payload.tar"

cat >"$WORK/guest.sh" <<SH
K_ONLY='$K_ONLY'
A_ONLY='$A_ONLY'
export SG_BUDGET_SCALE='$BUDGET_SCALE' CANARY_BUDGET_SCALE='$BUDGET_SCALE'
SH
cat >>"$WORK/guest.sh" <<'SH'
echo R3-BEGIN
. /usr/lib/os-release
echo "R3 image=$IMAGE_VERSION kernel=$(uname -r) systemd=$(systemctl --version | head -1 | cut -d' ' -f2)"
entry=$(tail -c +5 /sys/firmware/efi/efivars/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f 2>/dev/null | tr -d '\0')
esp=$(bootctl --print-boot-path 2>/dev/null || echo /boot)
echo "R3 image_ring=$(gpg --show-keys --with-colons /usr/lib/systemd/import-pubring.pgp 2>/dev/null | awk -F: '/^fpr/ {printf "%s,", $10}')"
echo "R3 booted_uki=$entry booted_uki_sha256=$(sha256sum "$esp/EFI/Linux/$entry" 2>/dev/null | cut -d' ' -f1) usrhash=$(sed -n 's/.*usrhash=\([0-9a-f]*\).*/\1/p' /proc/cmdline)"
R=/var/tmp/r3; rm -rf $R; mkdir -p $R
tar -xf /dev/vdb -C $R
DEST=$(smbd -b | sed -nE 's/^ *MODULESDIR: *(.*)$/\1/p')/vfs/omnifile_prevver.so
export OMNIFILE_PREVVER_SO=$DEST SG_REEXEC=1
t0=$(date +%s)
bash $R/ci/kernel-canary/run.sh --bin-dir $R/canary-bin ${K_ONLY:+--only $K_ONLY} >/var/tmp/r3-canary.log 2>&1; k=$?
t1=$(date +%s)
bash $R/edge/crates/omnifile-smbguard/tests/a-suite/run.sh --bin-dir $R/bin ${A_ONLY:+--only $A_ONLY} >/var/tmp/r3-asuite.log 2>&1; a=$?
t2=$(date +%s)
echo "R3 canary_rc=$k canary_s=$((t1 - t0)) a_suite_rc=$a a_suite_s=$((t2 - t1)) budget_scale=$SG_BUDGET_SCALE arch=$(uname -m) cpus=$(nproc) mem_mib=$(free -m | awk '/^Mem:/ {print $2}')"
# Each check's line from the summaries, and the tail of a failing log.
grep -E '^\| .* \| (PASS|\*\*FAIL)' /srv/omni-a/results/summary.md 2>/dev/null | sed 's/^/R3 canary: /'
grep -E '^## ' /srv/omni-guard/results/summary.md 2>/dev/null | sed 's/^/R3 a-suite: /'
grep -E '^[a-z0-9_]+: (PASS|\*\*FAIL)' /var/tmp/r3-canary.log | sed 's/^/R3 canary-check: /'
# When each A-suite check started and ended (its log's timestamps), then the evidence of every
# failed check, and always of the floods (A8, K4: reader counts and drain times, risk R6).
grep -E '^\[[0-9:]+\] --- ' /var/tmp/r3-asuite.log | sed 's/^/R3 a-suite-time: /'
for m in /srv/omni-guard/results/*.md /srv/omni-a/results/*.md; do
  n=${m##*/}; n=${n%.md}
  case $n in summary) continue ;; esac
  if grep -q 'FAIL' "$m" || [ "$n" = a08_flood ] || [ "$n" = k4_flood ]; then
    grep -v '^$' "$m" | sed "s/^/R3 evidence $n: /"
  fi
done
[ $k = 0 ] || tail -n 30 /var/tmp/r3-canary.log | sed 's/^/R3 canary-log: /'
[ $a = 0 ] || tail -n 30 /var/tmp/r3-asuite.log | sed 's/^/R3 a-suite-log: /'
echo R3-END
systemctl --no-block poweroff
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
vm_prepare "$WORK" "$DISK" 32G
# A8 and K4 run 17,000 reader threads: the kernel's threads-max follows the guest's memory
# (about 24,000 at 3 GiB), so the gate's guest gets 8 GiB unless told otherwise.
export VM_MEM=${VM_MEM:-8192}
log "booting $DISK ($VM_ARCH, ${VM_MEM} MiB) with the suites, budgets x$BUDGET_SCALE (log: $WORK/serial.log)"
vm_run "$WORK" "${R3_TIMEOUT:-36000}" omnifile-r3-gate.service "$WORK/guest.unit" \
  -drive "if=virtio,format=raw,readonly=on,file=$WORK/payload.tar" || true
G=$(grep -ao 'R3.*' "$WORK/serial.log" | tr -d '\r')
printf '%s\n' "$G"

# ---- the verdict ----------------------------------------------------------------------------
grep -q '^R3-END' <<<"$G" || refuse "the gate did not finish (serial log: $WORK/serial.log)"
grep -q "^R3 image=$V " <<<"$G" || refuse "the guest is not version $V"
grep -q "^R3 booted_uki=omnifile-edge_$V.efi booted_uki_sha256=$UKI_SHA " <<<"$G" ||
  refuse "the UKI the gate booted is not the feed's ($(grep '^R3 booted_uki' <<<"$G"); the feed's is $UKI_SHA)"
# The image the gate booted trusts the key the feed is signed with (Codex on #31): otherwise
# the feed verifies here and every box built from this image rejects it.
grep -Eq "^R3 image_ring=(.*,)?$SIGNER_FPR," <<<"$G" ||
  refuse "the booted image's keyring ($(sed -n 's/^R3 image_ring=//p' <<<"$G")) does not hold the update key $SIGNER_FPR: build the image with --pubring $UPDATE_RING"
KREL=$(sed -n 's/^R3 image=[^ ]* kernel=\([^ ]*\) .*/\1/p' <<<"$G")
grep -q '^R3 canary_rc=0 .* a_suite_rc=0 ' <<<"$G" || refuse "a suite failed on kernel $KREL: $(grep '^R3 canary_rc' <<<"$G")"
for c in $K_WANT; do
  grep -Eq "^R3 canary-check: $c: PASS" <<<"$G" || refuse "kernel canary $c did not pass on $KREL"
done
for c in $A_WANT; do
  grep -Eq "^R3 a-suite: ## $c: PASS" <<<"$G" || refuse "A-suite $c did not pass on $KREL"
done

{
  echo "version=$V"
  echo "kernel=$KREL"
  echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "commit=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)$(git -C "$REPO" diff --quiet 2>/dev/null || echo +dirty)"
  echo "source_offer=$OFFER_RECORD"
  sed 's/^/binding./' <<<"$BINDING"
  grep '^R3 booted_uki' <<<"$G" | sed 's/^R3 //'
  if [ -n "$A_ONLY$K_ONLY" ] || [ "$BUDGET_SCALE" != 1 ]; then
    echo "partial=yes (a-suite: ${A_ONLY:-all}; canary: ${K_ONLY:-all}; budget_scale: $BUDGET_SCALE)"
  else
    echo "partial=no"
  fi
  grep -E '^R3 (image=|canary_rc=|canary-check: |a-suite: |evidence (a08_flood|k4_flood): )' <<<"$G" | sed 's/^R3 //'
} >"$FEED/R3-GATE"
# ---- Secure Boot (owner decision C): the UKI and systemd-boot, with OmniFile's key ---------
# The feed's UKI and the disk's ESP (its UKI, systemd-boot and the fallback loader) are signed
# with the Key Vault key (sbsign through the e_akv engine) or, for a test feed, a local key.
# Stripping the signature must give back the bytes the gate booted.
sign_pe() { # sign_pe IN OUT
  case $UKI_SIGNER in
    kv:*) local id=${UKI_SIGNER#kv:}
      sbsign --engine "${AKV_ENGINE:-e_akv}" --key "vault:${id%%:*}:${id#*:}" --cert "$UKI_CERT" \
        --output "$2" "$1" ;;
    fake:*) sbsign --key "${UKI_SIGNER#fake:}" --cert "$UKI_CERT" --output "$2" "$1" ;;
  esac >/dev/null 2>&1 || refuse "sbsign failed on ${1##*/}"
  sbverify --cert "$UKI_CERT" "$2" >/dev/null 2>&1 || refuse "${1##*/} does not verify after signing"
  cp "$2" "$2.strip" && sbattach --remove "$2.strip" >/dev/null 2>&1
  local a b; a=$(sha256sum "$1" | cut -d' ' -f1); b=$(sha256sum "$2.strip" | cut -d' ' -f1)
  rm -f "$2.strip"
  [ "$a" = "$b" ] || refuse "${1##*/}: the signed file is not the booted one plus a signature"
}
S=$WORK/sign; rm -rf "$S"; mkdir -p "$S"
ESP=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import feed_binding as f
for n, t, u, off, size in f.gpt_partitions(sys.argv[2]):
    if str(t) == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": print(off)' "$HERE" "$DISK")
[ -n "$ESP" ] || refuse "no ESP on $DISK"
UKI_NAME=omnifile-edge_$V.efi
# The loaders' names are the disk's architecture's (x64, or aa64 for the arm64 test build).
case $VM_ARCH in arm64) EFI_SFX=aa64 FALLBACK=BOOTAA64.EFI ;; *) EFI_SFX=x64 FALLBACK=BOOTX64.EFI ;; esac
sign_pe "$FEED/$UKI_NAME" "$S/$UKI_NAME"
for f in "EFI/systemd/systemd-boot$EFI_SFX.efi" "EFI/BOOT/$FALLBACK" "EFI/Linux/$UKI_NAME"; do
  mcopy -n -i "$DISK@@$ESP" "::$f" "$S/esp.in" || refuse "no $f on the disk's ESP"
  if [ "$f" = "EFI/Linux/$UKI_NAME" ]; then
    cmp -s "$S/esp.in" "$FEED/$UKI_NAME" || cmp -s "$S/esp.in" "$S/$UKI_NAME" ||
      refuse "the disk's $f is not the feed's UKI"
    cp "$S/$UKI_NAME" "$S/esp.out"
  else
    sbverify --cert "$UKI_CERT" "$S/esp.in" >/dev/null 2>&1 && cp "$S/esp.in" "$S/esp.out" ||
      sign_pe "$S/esp.in" "$S/esp.out"
  fi
  mcopy -o -i "$DISK@@$ESP" "$S/esp.out" "::$f" || refuse "could not write $f back"
  rm -f "$S/esp.in" "$S/esp.out"
done
cp "$S/$UKI_NAME" "$FEED/$UKI_NAME"
{
  echo "secure_boot.uki_sha256=$(sha256sum "$FEED/$UKI_NAME" | cut -d' ' -f1)"
  echo "secure_boot.cert_sha256=$(openssl x509 -in "$UKI_CERT" -outform DER | sha256sum | cut -d' ' -f1)"
  echo "secure_boot.signer=${UKI_SIGNER%%:*}"
  echo "update_key=$SIGNER_FPR"
  grep '^R3 image_ring=' <<<"$G" | sed 's/^R3 //'
} >>"$FEED/R3-GATE"

# ---- the feed's signature, made by Key Vault (owner decision B) ------------------------------
(cd "$FEED" && sha256sum -- $(sed 's/^[0-9a-f]*  //' SHA256SUMS.files) >SHA256SUMS && sha256sum -- R3-GATE >>SHA256SUMS \
  && rm -f SHA256SUMS.files)
python3 "$HERE/pgp_kv.py" sign --signer "$SIGNER" --created "$UPDATE_KEY_CREATED" --ring "$UPDATE_RING" \
  --in "$FEED/SHA256SUMS" --out "$FEED/SHA256SUMS.gpg" || refuse "the update signature failed"
log "kernel $KREL passed the A-suite and the kernel canary; signed $FEED/SHA256SUMS ($SIGNER)"

# ---- the GitHub release (owner decision B): the assets, and the command; nothing is uploaded
A=$OUT/release-assets
rm -rf "$A" && mkdir -p "$A"
ln -f "$FEED"/* "$A/" 2>/dev/null || cp "$FEED"/* "$A/"
log "release assets in $A; publish them, as the latest release, with:"
printf '  gh release create v%s --repo %s --latest --title "OmniFile edge %s" --notes-file %s %s/*\n' \
  "$V" "$RELEASE_REPO" "$V" "$A/R3-GATE" "$A"
