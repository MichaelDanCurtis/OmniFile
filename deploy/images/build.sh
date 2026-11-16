#!/usr/bin/env bash
# Build OmniFile's container images for Azure (linux/amd64) and record their digests.
#
#   deploy/images/build.sh [--only service|web|relay]... [--lock FILE] [--tag-prefix PREFIX]
#                          [--min-free-gib N] [--builder NAME]
#   deploy/images/build.sh --clean [--lock FILE]
#
# The images, each built from this checkout:
#   service  service/Containerfile.api, context service/  -> the apps api, enrol, worker, verifier
#   web      service/Containerfile.web, context the repo   -> web (the portal SPA and its API)
#   relay    relay/Containerfile, context relay/           -> relay
# Each app's command is in deploy/azure/main.bicep (appCommands), not in the image.
#
# Arguments:
#   --only NAME        build only this image (repeatable); the lock keeps the other entries
#   --lock FILE        default deploy/images/images.lock.json
#   --tag-prefix P     local tags are P/<repository>:<short commit> (default omnifile-local)
#   --min-free-gib N   refuse to start a build with less than N GiB free where Docker writes
#                      (default 8: a build adds about 1.5 GiB, and the machine's floor is 6)
#   --builder NAME     use this buildx builder and keep its cache. Default: a builder of its own
#                      (docker-container driver) for each image, removed with its cache after it,
#                      so a build leaves nothing behind but the loaded image
#   --clean            remove the local tags the lock names (this script's own), then exit
#
# Outputs:
#   The lock file, JSON:
#     {"schema": 1, "platform": "linux/amd64",
#      "images": {"<image>": {"repository", "containerfile", "context", "digest",
#                             "size_bytes", "local_tag", "source_commit", "built_at"}},
#      "apps": {"api": "service", "enrol": "service", "worker": "service",
#               "verifier": "service", "web": "web", "relay": "relay"}}
#   `digest` is the image manifest's digest; an app's image reference is
#   <registry>/<repository>@<digest> (deploy/azure/test.bicepparam builds it that way).
#   `source_commit` ends in "+dirty" when the checkout had uncommitted changes (the lock aside).
#   One local tag per image, for deploy/images/smoke.sh.
#
# It pushes nothing and needs no registry or account. A release step pushes these manifests;
# Docker's containerd image store pushes the manifest it built, so the digest stays the same.
# Builds run one at a time, with provenance and SBOM attestations off so the manifest is the
# plain image (SBOMs are made by the release lane).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
lock="$here/images.lock.json"
prefix="omnifile-local"
min_free=8
only=()
clean=0
builder=""

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --only) [ $# -ge 2 ] || usage; only+=("$2"); shift 2 ;;
    --lock) [ $# -ge 2 ] || usage; lock="$2"; shift 2 ;;
    --tag-prefix) [ $# -ge 2 ] || usage; prefix="$2"; shift 2 ;;
    --min-free-gib) [ $# -ge 2 ] || usage; min_free="$2"; shift 2 ;;
    --builder) [ $# -ge 2 ] || usage; builder="$2"; shift 2 ;;
    --clean) clean=1; shift ;;
    -h|--help) usage ;;
    *) echo "build.sh: unknown argument $1" >&2; usage ;;
  esac
done

# image -> repository, Containerfile, context (relative to the repository root)
spec() {
  case "$1" in
    service) echo "omnifile-service service/Containerfile.api service" ;;
    web) echo "omnifile-web service/Containerfile.web ." ;;
    relay) echo "omnifile-relay relay/Containerfile relay" ;;
    *) echo "build.sh: no image called $1 (service, web, relay)" >&2; exit 2 ;;
  esac
}

if [ "$clean" = 1 ]; then
  [ -f "$lock" ] || { echo "build.sh: no $lock"; exit 0; }
  python3 -c 'import json,sys
for i in json.load(open(sys.argv[1]))["images"].values():
    if i.get("local_tag"): print(i["local_tag"])' "$lock" | while read -r tag; do
    if docker image inspect "$tag" >/dev/null 2>&1; then
      docker image rm "$tag" >/dev/null && echo "removed $tag"
    fi
  done
  exit 0
fi

[ ${#only[@]} -gt 0 ] || only=(service web relay)
for name in "${only[@]}"; do spec "$name" >/dev/null; done
command -v docker >/dev/null || { echo "build.sh: needs Docker" >&2; exit 2; }
docker buildx version >/dev/null 2>&1 || { echo "build.sh: needs docker buildx" >&2; exit 2; }

commit="$(git -C "$repo" rev-parse HEAD)"
short="$(git -C "$repo" rev-parse --short=12 HEAD)"
# The lock is this script's output, so it alone doesn't make the checkout dirty.
[ -z "$(git -C "$repo" status --porcelain -- . ':!deploy/images/images.lock.json')" ] || commit="$commit+dirty"

# Free space where Docker keeps images: on macOS the Data volume behind Docker Desktop's disk.
free_gib() {
  local where="/"
  [ "$(uname)" = Darwin ] && where="/System/Volumes/Data"
  df -k "$where" | awk 'NR==2 { printf "%d", $4 / 1048576 }'
}

work="$(mktemp -d)"
own_builder=""
drop_builder() {
  if [ -n "$own_builder" ]; then
    docker buildx rm --force "$own_builder" >/dev/null 2>&1 || true
    own_builder=""
  fi
}
trap 'drop_builder; rm -rf "$work"' EXIT

for name in "${only[@]}"; do
  read -r repository containerfile context <<<"$(spec "$name")"
  free="$(free_gib)"
  if [ "$free" -lt "$min_free" ]; then
    echo "build.sh: only ${free} GiB free for Docker, below --min-free-gib $min_free: not building $name" >&2
    exit 3
  fi
  tag="$prefix/$repository:$short"
  echo "build $name ($containerfile) as $tag, linux/amd64; ${free} GiB free"
  meta="$work/$name.json"
  use="$builder"
  if [ -z "$use" ]; then
    own_builder="omnifile-build-$$-$name"
    docker buildx create --name "$own_builder" --driver docker-container >/dev/null
    use="$own_builder"
  fi
  docker buildx build \
    --builder "$use" \
    --platform linux/amd64 \
    --provenance=false --sbom=false \
    --load \
    --tag "$tag" \
    --file "$repo/$containerfile" \
    --metadata-file "$meta" \
    "$repo/$context"
  python3 - "$lock" "$name" "$repository" "$containerfile" "$context" "$tag" "$commit" "$meta" <<'PY'
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

lock_path, name, repository, containerfile, context, tag, commit, meta_path = sys.argv[1:9]
meta = json.load(open(meta_path))
digest = meta.get("containerimage.digest")
inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
if not digest:
    sys.exit(f"build.sh: buildx gave no manifest digest for {tag}")
if inspect.get("Architecture") != "amd64" or inspect.get("Os") != "linux":
    sys.exit(f"build.sh: {tag} is {inspect.get('Os')}/{inspect.get('Architecture')}, not linux/amd64")
try:
    lock = json.load(open(lock_path))
except FileNotFoundError:
    lock = {}
lock["schema"] = 1
lock["platform"] = "linux/amd64"
lock.setdefault("images", {})[name] = {
    "repository": repository,
    "containerfile": containerfile,
    "context": context,
    "digest": digest,
    "size_bytes": inspect.get("Size"),
    "local_tag": tag,
    "source_commit": commit,
    "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
lock["apps"] = {
    "api": "service",
    "enrol": "service",
    "worker": "service",
    "verifier": "service",
    "web": "web",
    "relay": "relay",
}
lock["images"] = dict(sorted(lock["images"].items()))
tmp = lock_path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(lock, fh, indent=2)
    fh.write("\n")
os.replace(tmp, lock_path)
print(f"  {name}: {repository}@{digest} ({inspect.get('Size', 0) // 1048576} MiB)")
PY
  drop_builder
done
echo "lock: $lock"
