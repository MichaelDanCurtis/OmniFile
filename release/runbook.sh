#!/usr/bin/env bash
# One release, end to end (docs/release/README.md):
#
#   1 stamp       release/stamp.sh --check: VERSION is the version of every part
#   2 build       the edge binaries, the appliance (appliance/build.sh: the image, its manifest, initrd.cpio.zst
#                 and BUILD-INFO) and the images (deploy/images/build.sh, which arrives with PR #34)
#   3 offer       the GPL/LGPL source offer from the build's manifest (appliance/source-offer/source_offer.py),
#                 then release/check_offer.py
#   4 sbom        CycloneDX 1.6 SBOMs (release/sbom.py), the schema check, and release/licences.py
#   5 scan        osv-scanner over every lockfile, with release/vuln-waivers.toml (release/scan.py)
#   6 notes       release notes from the merged pull requests (release/notes.sh)
#   7 gate+sign   appliance/release.sh: the R3 gate, then the UKI (Secure Boot, e_akv) and SHA256SUMS
#                 (pgp_kv.py, the Key Vault update key)
#   8 params      the image digests written into the Bicep parameters, compiled (bicep build-params)
#   9 publish     the images pushed (digests compared with the lock), the sources repository's release
#                 (publish_sources.py, decision 46), then the feed's GitHub Release, created as a draft on the built
#                 commit (--target) and made the latest release last, so a box never sees a feed whose sources
#                 aren't published
#
#   release/runbook.sh --out DIR [--version V] [--from REF] [--to REF] [--publish] [--stop-after STEP]
#   release/runbook.sh --dry-run --out DIR --appliance-out BUILD --keys KEYS --source-offer OFFER
#                      [--images-lock FILE] [--bin-dir D --canary-bin-dir D] [--version V] [--full-gate]
#
# A real run uses release.conf's Key Vault keys (az signed in as the release identity), builds everything from this
# checkout, and publishes only with --publish (without it, it stops before step 9 and prints what it would run).
# It refuses a dirty tree, a commit the remote doesn't have (the tag is made on it), and an --images-lock (it builds
# the images, and each must name this commit). --stop-after stamp|build|offer|sbom|scan|notes|gate+sign|params ends
# the run after that step (to see the SBOMs, scan and notes before the long gate). The sources repository is
# --sources-repo, else $OMNIFILE_SOURCES_REPO, else release.conf's SOURCES_REPO.
#
# --dry-run touches no account and no network (OMNIFILE_OFFLINE=1 and UV_OFFLINE=1: cargo, uv and osv-scanner use
# only their caches, and the sbsigntool image of release/docker-tools must already be built):
#   - signing: the update key is kv:<a fake key id> answered by release/fakes/az (appliance/tests/fake_az.py, from
#     KEYS/update.pem), so pgp_kv.py's Key Vault path runs; the UKI key is fake:KEYS/sb.key with KEYS/sb.crt;
#     KEYS/update-ring.pgp must be the keyring the cached build embeds (build.sh --pubring). The gate runs the
#     quick subset (a01, k1); --full-gate runs every check. Fake keys sign only with --allow-partial, so a dry run
#     always passes it.
#   - publishing: release/fakes/gh first on PATH; releases land in OUT/gh-releases/<owner>/<repo>/<tag>/, the layout
#     appliance/tests/gh-releases-stub.py serves, so appliance/tests/signed-update.sh installs exactly them.
#     RELEASE_REPO is omnifile/omnifile-edge there (signed-update.sh's), the sources repository
#     dry-run-owner/omnifile-gateway-sources, the registry omnifile-dry-run.azurecr.invalid.
#   - inputs it would build or fetch come from a cached build instead, and the record says so: --appliance-out (an
#     appliance/build.sh output), --bin-dir/--canary-bin-dir (the gate's binaries; default: the build's own
#     omnifile-edge tools are not needed when they are in BUILD/bin), --source-offer (collected beforehand, it
#     needs the Ubuntu archive), --images-lock (default the committed deploy/images/images.lock.json),
#     and the OSV databases (release/scan.py --download-db, beforehand).
#   - AZURE_*, AWS_*, ARM_* and GH_TOKEN/GITHUB_TOKEN are unset.
# The appliance build in --appliance-out is changed in place by the gate (its ESP is signed): give it a copy.
#
# Outputs in OUT: RELEASE-RECORD (each step and what it used), sbom/, scan/, NOTES.md, the parameters file, and
# release-assets/ (what step 9 publishes). Needs bash, python3 (3.11+), git, cargo, uv, osv-scanner and, for the
# gate, what appliance/release.sh needs (qemu, OVMF, sbsigntool, mtools; on macOS release/docker-tools has
# sbsign/sbverify/sbattach through Docker).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
# shellcheck source=../appliance/release.conf
. "$REPO/appliance/release.conf"
DRY=0 PUBLISH=0 FULL_GATE=0 V='' OUT='' FROM='' TO=HEAD STOP_AFTER=''
APPLIANCE_OUT='' KEYS='' OFFER='' IMAGES_LOCK='' BIN_DIR='' CANARY_BIN='' SNAPSHOT='' REGISTRY=${IMAGE_REGISTRY:-}
# The sources repository (decision 46): --sources-repo, else $OMNIFILE_SOURCES_REPO, else release.conf's SOURCES_REPO.
SOURCES_REPO=${OMNIFILE_SOURCES_REPO:-${SOURCES_REPO:-}}
A_ONLY=a01 K_ONLY=k1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --publish) PUBLISH=1 ;;
    --full-gate) FULL_GATE=1 ;;
    --stop-after) STOP_AFTER=$2; shift ;;
    --version) V=$2; shift ;;
    --out) OUT=$2; shift ;;
    --from) FROM=$2; shift ;;
    --to) TO=$2; shift ;;
    --appliance-out) APPLIANCE_OUT=$2; shift ;;
    --keys) KEYS=$2; shift ;;
    --source-offer) OFFER=$2; shift ;;
    --images-lock) IMAGES_LOCK=$2; shift ;;
    --bin-dir) BIN_DIR=$2; shift ;;
    --canary-bin-dir) CANARY_BIN=$2; shift ;;
    --snapshot) SNAPSHOT=$2; shift ;;
    --registry) REGISTRY=$2; shift ;;
    --sources-repo) SOURCES_REPO=$2; shift ;;
    --a-suite-only) A_ONLY=$2; shift ;;
    --canary-only) K_ONLY=$2; shift ;;
    -h | --help) sed -n '2,48p' "$0"; exit 0 ;;
    *) echo "runbook: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$OUT" ] || { echo "runbook: --out DIR is required" >&2; exit 2; }
STEPS="stamp build offer sbom scan notes gate+sign params publish"
case " $STEPS " in *" ${STOP_AFTER:-stamp} "*) ;; *)
  echo "runbook: --stop-after $STOP_AFTER: one of ${STEPS% publish}" >&2; exit 2 ;; esac
[ "$STOP_AFTER" != publish ] || { echo "runbook: --stop-after publish is the whole run" >&2; exit 2; }
mkdir -p "$OUT" && OUT=$(cd "$OUT" && pwd)
REC=$OUT/RELEASE-RECORD
log() { printf '[runbook] %s\n' "$*"; }
die() { printf '[runbook] FAILED: %s\n' "$*" >&2; echo "result=failed: $*" >>"$REC"; exit 1; }
record() { echo "$1=$2" >>"$REC"; }
step() { # step NAME: start it, unless the step before was --stop-after's
  if [ -n "${STEP:-}" ] && [ "$STEP" = "$STOP_AFTER" ]; then
    record result "stopped after $STEP (--stop-after)"; log "stopped after $STEP: $REC"; cat "$REC"; exit 0
  fi
  STEP=$1; log "== step $1"
}
: >"$REC"
# Whatever stops the run from here on, its record ends with a result (set after the record is emptied).
finish() {
  local rc=$?
  grep -q '^result=' "$REC" 2>/dev/null || echo "result=failed: exit $rc in step ${STEP:-setup}" >>"$REC"
}
trap finish EXIT
VERSION_FILE=$(tr -d '[:space:]' <"$REPO/VERSION")
V=${V:-$VERSION_FILE}
record version "$V"
record mode "$([ $DRY = 1 ] && echo dry-run || echo release)"
record commit "$(git -C "$REPO" rev-parse HEAD)$(git -C "$REPO" diff --quiet HEAD 2>/dev/null || echo +dirty)"
record started "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TARGET=$(git -C "$REPO" rev-parse HEAD)

if [ $DRY = 1 ]; then
  # Nothing in a dry run may reach an account: drop credentials, and put the fakes first.
  for v in $(compgen -e); do
    case $v in AZURE_* | AWS_* | ARM_* | GH_TOKEN | GITHUB_TOKEN | GH_ENTERPRISE_TOKEN) unset "$v" ;; esac
  done
  export PATH="$HERE/fakes:$PATH" AZ="$HERE/fakes/az" GH_FAKE_ROOT="$OUT/gh-releases" GH_FAKE_LOG="$OUT/gh.log"
  export OMNIFILE_OFFLINE=1 UV_OFFLINE=1
  # No network for tools either: the Docker sbsigntool must be built already (release/docker-tools/sbtool).
  if [ "$(command -v sbsign)" = "$HERE/docker-tools/sbsign" ] &&
    ! docker image inspect omnifile-sbsigntool:0.9.4 >/dev/null 2>&1; then
    die "the dry run uses release/docker-tools/sbsign, but its image omnifile-sbsigntool:0.9.4 isn't built: run release/docker-tools/sbverify --help once, online"
  fi
  RELEASE_REPO=omnifile/omnifile-edge
  REGISTRY=${REGISTRY:-omnifile-dry-run.azurecr.invalid}
  [ "$(command -v gh)" = "$HERE/fakes/gh" ] || die "the fake gh is not first on PATH"
  [ "$(command -v az)" = "$HERE/fakes/az" ] || die "the fake az is not first on PATH"
  [ "$V" = "$VERSION_FILE" ] || log "dry run of $V while VERSION says $VERSION_FILE (allowed in a dry run only)"
  [ -n "$APPLIANCE_OUT" ] || die "--dry-run needs --appliance-out: a cached appliance/build.sh output (it doesn't build an image)"
  [ -n "$KEYS" ] || die "--dry-run needs --keys DIR: update.pem, update-ring.pgp (the one the build embeds), sb.key, sb.crt"
  [ -n "$OFFER" ] || die "--dry-run needs --source-offer DIR, collected beforehand (it fetches from the Ubuntu archive)"
  # The sources repository the cached offer names (publish_sources.py refuses any other).
  SOURCES_REPO=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sources_repo") or "")' \
    "$OFFER/MANIFEST.json" 2>/dev/null || true)
  SOURCES_REPO=${SOURCES_REPO:-dry-run-owner/omnifile-gateway-sources}
  for k in update.pem update-ring.pgp sb.key sb.crt; do [ -f "$KEYS/$k" ] || die "no $KEYS/$k"; done
  KEYS=$(cd "$KEYS" && pwd)
  export FAKE_AZ_KEY=$KEYS/update.pem FAKE_AZ_LOG=$OUT/az.log
  # A key id in Key Vault's shape: pgp_kv.py takes its kv: path, and the fake az answers.
  UPDATE_KEY=kv:https://omnifile-dry-run.vault.azure.net/keys/update-signing/$(printf %s "$KEYS" | sha256sum | cut -c1-32)
  UPDATE_RING=$KEYS/update-ring.pgp
  UKI_KEY=fake:$KEYS/sb.key
  UKI_CERT=$KEYS/sb.crt
else
  [ -z "$IMAGES_LOCK" ] || die "--images-lock is for dry runs: a release builds its images from this commit"
  [ "$V" = "$VERSION_FILE" ] || die "--version $V is not VERSION ($VERSION_FILE): set VERSION and stamp it (release/stamp.sh) first"
  [ -z "$(git -C "$REPO" status --porcelain)" ] || die "the tree has uncommitted changes: a release is built from a commit"
  # The release's tag is made on this commit (gh release create --target), so the remote must have it.
  git -C "$REPO" fetch --quiet origin || die "git fetch origin failed"
  git -C "$REPO" branch -r --contains "$TARGET" | sed 's/^[* ]*//' | grep -Eqx 'origin/(main|release/.+)' ||
    die "commit $TARGET is not on origin/main or an origin/release/* branch: merge it before releasing it"
  [ -n "$SOURCES_REPO" ] || die "no sources repository (--sources-repo or OMNIFILE_SOURCES_REPO, decision 46)"
  [ -n "$REGISTRY" ] || die "no image registry (--registry or IMAGE_REGISTRY): the owner's choice"
fi
record target "$TARGET"
record release_repo "$RELEASE_REPO"
record sources_repo "$SOURCES_REPO"
record registry "$REGISTRY"
ASSETS=$OUT/release-assets
rm -rf "$ASSETS" && mkdir -p "$ASSETS"

# ---- 1 stamp --------------------------------------------------------------------------------------------------
step stamp
"$HERE/stamp.sh" --check || die "the tree is not stamped with VERSION $VERSION_FILE (release/stamp.sh)"
record step.stamp "ok: every manifest, lockfile, cargo metadata and uv's __version__ say $VERSION_FILE"

# ---- 2 build --------------------------------------------------------------------------------------------------
step build
if [ $DRY = 1 ]; then
  APPLIANCE_OUT=$(cd "$APPLIANCE_OUT" && pwd)
  record step.build.appliance "cached: $APPLIANCE_OUT (not built in a dry run)"
else
  (cd "$REPO/edge" && cargo build --release --locked && cargo build --release --locked -p omnifile-acl --example omniacl)
  "$HERE/stamp.sh" --check --no-tools --edge-bin "$REPO/edge/target/release/omnifile-edge" ||
    die "the built omnifile-edge does not report $V"
  (cd "$REPO/spikes/filer/guard" && cargo build --release --locked)
  "$REPO/dev/scripts/build-artifacts.sh"
  "$REPO/samba-vfs/build.sh"
  APPLIANCE_OUT=$OUT/appliance
  "$REPO/appliance/build.sh" --version "$V" --pubring "$REPO/$UPDATE_RING" --out "$APPLIANCE_OUT"
  record step.build.appliance "built: $APPLIANCE_OUT"
fi
[ -f "$APPLIANCE_OUT/omnifile-edge_$V.raw" ] || die "no omnifile-edge_$V.raw in $APPLIANCE_OUT: not a build of $V"
MANIFEST=$APPLIANCE_OUT/omnifile-edge_$V.manifest
[ -f "$MANIFEST" ] || die "no $MANIFEST (ManifestFormat=json)"
BUILD_INFO=$APPLIANCE_OUT/BUILD-INFO
# BUILD-INFO names the builder's packages whose bits are in the image (the UKI's stub) and the archive snapshot, and
# initrd.cpio.zst is mkosi's initrd: without them the source offer can't be complete. appliance/build.sh writes
# BUILD-INFO from WP2's reproducible build on (claude/m6-appliance-gate); a build from before that is refused here.
[ -f "$BUILD_INFO" ] || die "no BUILD-INFO in $APPLIANCE_OUT: a build from before appliance/build.sh wrote it"
[ -f "$APPLIANCE_OUT/initrd.cpio.zst" ] || die "no initrd.cpio.zst in $APPLIANCE_OUT (mkosi's initrd)"
record appliance.manifest_sha256 "$(sha256sum "$MANIFEST" | cut -d' ' -f1)"
BIN_DIR=${BIN_DIR:-$([ $DRY = 1 ] && echo "$APPLIANCE_OUT/bin" || echo "$REPO/edge/target/release")}
CANARY_BIN=${CANARY_BIN:-$([ $DRY = 1 ] && echo "$APPLIANCE_OUT/canary-bin" || echo "$REPO/spikes/filer/guard/target/release")}
if [ -z "$IMAGES_LOCK" ]; then
  if [ $DRY = 0 ]; then
    "$REPO/deploy/images/build.sh"
    IMAGES_LOCK=$REPO/deploy/images/images.lock.json
  else
    IMAGES_LOCK=$REPO/deploy/images/images.lock.json  # the committed lock (deploy/images/build.sh)
    [ -f "$IMAGES_LOCK" ] || die "no $IMAGES_LOCK: give --images-lock"
  fi
fi
if [ $DRY = 0 ]; then
  python3 "$HERE/images_lock.py" "$IMAGES_LOCK" "$TARGET" || die "the images lock is not this commit's build"
fi
record step.build.images "$([ $DRY = 1 ] && echo "cached lock" || echo built): $IMAGES_LOCK sha256 $(sha256sum "$IMAGES_LOCK" | cut -d' ' -f1)"

# ---- 3 the source offer ---------------------------------------------------------------------------------------
step offer
if [ $DRY = 0 ] && [ -z "$OFFER" ]; then
  SNAPSHOT=${SNAPSHOT:-$(sed -n 's/^snapshot=//p' "$BUILD_INFO" 2>/dev/null)}
  [ -n "$SNAPSHOT" ] || die "no snapshot id (--snapshot, or snapshot= in the build's BUILD-INFO)"
  OFFER=$OUT/source-offer
  python3 "$REPO/appliance/source-offer/source_offer.py" --packages "$MANIFEST" --build-info "$BUILD_INFO" \
    --initrd "$APPLIANCE_OUT/initrd.cpio.zst" --snapshot "$SNAPSHOT" --release "$V" \
    --sources-repo "$SOURCES_REPO" --out "$OFFER" || die "the source offer is not complete"
  record step.offer "collected: $OFFER"
else
  OFFER=$(cd "$OFFER" && pwd)
  record step.offer "$([ $DRY = 1 ] && echo cached || echo given): $OFFER (collected beforehand by source_offer.py)"
fi
python3 "$HERE/check_offer.py" --offer "$OFFER" --manifest "$MANIFEST" --build-info "$BUILD_INFO" --release "$V" \
  >"$OUT/check-offer.log" 2>&1 || { cat "$OUT/check-offer.log" >&2; die "the source offer is not complete for $MANIFEST"; }
tail -n 2 "$OUT/check-offer.log"
record step.offer.check "$(tail -n 1 "$OUT/check-offer.log")"

# ---- 4 SBOMs and licences -------------------------------------------------------------------------------------
step sbom
rm -rf "$OUT/sbom"
python3 "$HERE/sbom.py" --out "$OUT/sbom" --version "$V" --images-lock "$IMAGES_LOCK" \
  --appliance-manifest "$MANIFEST" --appliance-build-info "$BUILD_INFO" --source-offer "$OFFER" ||
  die "the SBOMs could not be made"
python3 "$HERE/validate_sbom.py" "$OUT"/sbom/*.cdx.json || die "an SBOM is not valid CycloneDX 1.6"
python3 "$HERE/licences.py" --sboms "$OUT/sbom" || die "the SBOMs and LICENSES.md disagree, or a licence is not allowed"
record step.sbom "ok: $(ls "$OUT"/sbom/*.cdx.json | wc -l | tr -d ' ') SBOMs, CycloneDX 1.6, licences checked"

# ---- 5 the vulnerability scan ---------------------------------------------------------------------------------
step scan
SCAN=(--out "$OUT/scan" --sbom "$OUT/sbom/omnifile-appliance-$V.cdx.json" --images-lock "$IMAGES_LOCK")
if [ $DRY = 1 ]; then SCAN+=(--offline); else SCAN+=(--require-images --release); fi
python3 "$HERE/scan.py" "${SCAN[@]}" ||
  die "the vulnerability scan failed: an unwaived HIGH/CRITICAL/unscored finding or a bad waiver ($OUT/scan/scan.md)"
record step.scan "ok: $(tail -c 300 "$OUT/scan/scan.md" >/dev/null; python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d["findings"]), "findings,", sum(1 for f in d["findings"] if f["waiver"]), "waived,", "offline" if d["offline"] else "online")' "$OUT/scan/scan.json")"

# ---- 6 release notes ------------------------------------------------------------------------------------------
step notes
FROM=${FROM:-$(git -C "$REPO" describe --tags --abbrev=0 --match 'v*' "$TO" 2>/dev/null || git -C "$REPO" rev-list --max-parents=0 "$TO" | tail -n 1)}
"$HERE/notes.sh" "$FROM" "$TO" --version "$V" --security "$OUT/scan/scan.md" --out "$OUT/NOTES.md"
record step.notes "ok: $FROM..$TO"

# ---- 7 the R3 gate and the signatures -------------------------------------------------------------------------
step gate+sign
GATE=(--out "$APPLIANCE_OUT" --bin-dir "$BIN_DIR" --canary-bin-dir "$CANARY_BIN" --source-offer "$OFFER"
  --signer "$UPDATE_KEY" --key-created "$UPDATE_KEY_CREATED" --update-ring "$([ $DRY = 1 ] && echo "$UPDATE_RING" || echo "$REPO/$UPDATE_RING")"
  --uki-signer "$UKI_KEY" --uki-cert "$([ $DRY = 1 ] && echo "$UKI_CERT" || echo "$REPO/$UKI_CERT")" --github-repo "$RELEASE_REPO")
if [ $DRY = 1 ]; then
  GATE+=(--allow-partial)  # fake keys sign test feeds only
  [ $FULL_GATE = 1 ] || GATE+=(--a-suite-only "$A_ONLY" --canary-only "$K_ONLY")
fi
"$REPO/appliance/release.sh" "${GATE[@]}" 2>&1 | tee "$OUT/gate.log"
[ "${PIPESTATUS[0]}" = 0 ] || die "the R3 gate or the signing refused ($OUT/gate.log)"
[ -f "$APPLIANCE_OUT/feed/SHA256SUMS.gpg" ] || die "release.sh left no signature"
record step.gate "ok: $(grep -c . "$APPLIANCE_OUT/feed/R3-GATE") lines of R3-GATE; $(grep '^partial=' "$APPLIANCE_OUT/feed/R3-GATE")"
record step.sign "ok: update key $(sed -n 's/^update_key=//p' "$APPLIANCE_OUT/feed/R3-GATE"), Secure Boot $(sed -n 's/^secure_boot.signer=//p' "$APPLIANCE_OUT/feed/R3-GATE")"
cp "$APPLIANCE_OUT"/release-assets/* "$ASSETS/"

# ---- 8 the image digests in the Bicep parameters --------------------------------------------------------------
step params
PARAMS=$ASSETS/omnifile-images-$V.bicepparam
python3 "$HERE/bicep_params.py" --lock "$IMAGES_LOCK" --registry "$REGISTRY" --version "$V" \
  --example "$REPO/deploy/azure/main.example.bicepparam" --out "$PARAMS" --compile "$REPO/deploy/azure" ||
  die "the parameters file could not be written or does not compile"
record step.params "ok: $PARAMS (bicep build-params against deploy/azure/main.bicep)"

# ---- 9 publish ------------------------------------------------------------------------------------------------
step publish
cp "$OUT"/sbom/*.cdx.json "$OUT/scan/scan.json" "$OUT/NOTES.md" "$ASSETS/"
cp "$OFFER/SOURCE-OFFER.md" "$ASSETS/SOURCE-OFFER.md"
(cd "$ASSETS" && rm -f RELEASE-ASSETS.SHA256SUMS && sha256sum -- * >../RELEASE-ASSETS.SHA256SUMS &&
  mv ../RELEASE-ASSETS.SHA256SUMS .)
PUB=(gh release create "v$V" --repo "$RELEASE_REPO" --draft --target "$TARGET" --title "OmniFile Gateway $V"
  --notes-file "$OUT/NOTES.md")
LIVE=(gh release edit "v$V" --repo "$RELEASE_REPO" --draft=false --latest)
SRC=(python3 "$REPO/appliance/source-offer/publish_sources.py" --bundle "$OFFER" --repo "$SOURCES_REPO"
  --work "$OUT/sources-publish" --execute)
push_images() {
  python3 - "$IMAGES_LOCK" "$REGISTRY" "$V" <<'PY'
import json, sys
lock, reg, v = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
for name, i in sorted(lock["images"].items()):
    print(f"{i['local_tag']} {reg}/{i['repository']}:{v} {i['digest']}")
PY
}
if [ $DRY = 1 ] || [ $PUBLISH = 1 ]; then
  if [ $DRY = 1 ]; then
    push_images | while read -r src dst digest; do
      echo "docker tag $src $dst && docker push $dst   # must give $digest" >>"$OUT/push-plan.txt"
    done
    record step.publish.images "planned (dry run, nothing pushed): $OUT/push-plan.txt"
  else
    push_images | while read -r src dst digest; do
      docker tag "$src" "$dst" && docker push "$dst"
      got=$(docker buildx imagetools inspect "$dst" --format '{{json .Manifest.Digest}}' | tr -d '"')
      [ "$got" = "$digest" ] || die "$dst is $got in the registry, not the locked $digest"
    done
    record step.publish.images "pushed to $REGISTRY"
  fi
  # The sources first (decision 46: a release ships with its source), then the feed as a draft, and only then live.
  "${SRC[@]}" || die "publishing the sources failed"
  record step.publish.sources "ok: $SOURCES_REPO v$V$([ $DRY = 1 ] && echo " (fake gh)")"
  "${PUB[@]}" "$ASSETS"/* || die "gh release create (draft) failed"
  record step.publish.feed "draft: $RELEASE_REPO v$V on $TARGET$([ $DRY = 1 ] && echo " (fake gh: $GH_FAKE_ROOT)")"
  "${LIVE[@]}" || die "making v$V the latest release failed: it is still a draft"
  record step.publish.live "ok: v$V is the latest release"
else
  log "not publishing without --publish. Step 9 would run:"
  push_images | while read -r src dst digest; do echo "  docker tag $src $dst && docker push $dst   # $digest"; done
  echo "  ${SRC[*]}"
  echo "  ${PUB[*]} $ASSETS/*"
  echo "  ${LIVE[*]}"
  record step.publish "not run (no --publish)"
fi
record finished "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
record result ok
log "release $V: $REC"
cat "$REC"
