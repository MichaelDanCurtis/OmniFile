#!/usr/bin/env bash
# Offline tests of deploy/teams/build.sh and package.py (the schema comes from build.sh's cache,
# or --schema FILE / OMNIFILE_TEAMS_SCHEMA). Prints one line per check; exit 1 on a failure.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
schema="${OMNIFILE_TEAMS_SCHEMA:-${XDG_CACHE_HOME:-$HOME/.cache}/omnifile/teams-schema/1.1.3/MicrosoftTeams.schema.json}"
[ "${1:-}" = "--schema" ] && schema="$2"
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }
py() { uv --directory "$repo/service" run --quiet python "$@"; }

BOT=0a1b2c3d-1111-2222-3333-444455556666
build() { "$here/build.sh" --schema "$schema" --bot-app-id "$BOT" --portal-url https://portal.example.test "$@"; }

if [ ! -f "$schema" ]; then
  # First run on this machine: build.sh fetches and checks the pinned schema package.
  build --out-dir "$work/fetch" >/dev/null 2>&1 || true
  schema="${XDG_CACHE_HOME:-$HOME/.cache}/omnifile/teams-schema/1.1.3/MicrosoftTeams.schema.json"
fi
[ -f "$schema" ] || { echo "FAIL no manifest schema (npm offline?): pass --schema FILE"; exit 1; }

out="$(build --out-dir "$work/a")"
zip="$work/a/omnifile-teams-app.zip"
[ "$(printf '%s\n' "$out" | tail -1)" = "$zip" ] && ok "the last line is the package's path" || bad "last line: $out"
names="$(python3 -c 'import sys,zipfile; print(" ".join(sorted(zipfile.ZipFile(sys.argv[1]).namelist())))' "$zip")"
[ "$names" = "color.png manifest.json outline.png" ] && ok "the zip holds manifest.json and the two icons" || bad "zip: $names"
py "$here/package.py" validate --schema "$schema" "$zip" && ok "the package validates against MicrosoftTeams.schema.json v1.17" || bad "validate"

build --out-dir "$work/b" >/dev/null
cmp -s "$zip" "$work/b/omnifile-teams-app.zip" && ok "the same arguments give the same bytes" || bad "not deterministic"
manifest() { python3 -c 'import json,sys,zipfile; print(json.load(zipfile.ZipFile(sys.argv[1]).open("manifest.json"))[sys.argv[2]])' "$1" "$2"; }
build --out-dir "$work/c" --version 1.0.1 >/dev/null
[ "$(manifest "$zip" id)" = "$(manifest "$work/c/omnifile-teams-app.zip" id)" ] \
  && ok "a new version keeps the manifest id (Teams updates the app)" || bad "manifest id changed"
bot="$(python3 -c 'import json,sys,zipfile; print(json.load(zipfile.ZipFile(sys.argv[1]).open("manifest.json"))["bots"][0]["botId"])' "$zip")"
[ "$bot" = "$BOT" ] && ok "the bot is the given app id" || bad "bot $bot"

if build --out-dir "$work/d" --bot-app-id not-a-guid >/dev/null 2>"$work/err"; then
  bad "a bot id that isn't a GUID was accepted"
else
  grep -q "not a GUID" "$work/err" && ok "a bot id that isn't a GUID is refused" || bad "$(cat "$work/err")"
fi
if build --out-dir "$work/d" --portal-url http://plain.example >/dev/null 2>"$work/err"; then
  bad "an http portal URL was accepted"
else
  ok "the portal URL must be https"
fi

# The schema check itself catches a broken manifest.
python3 - "$zip" "$work" <<'PY'
import json, sys, zipfile
doc = json.load(zipfile.ZipFile(sys.argv[1]).open("manifest.json"))
broken = dict(doc); broken.pop("developer")
json.dump(broken, open(f"{sys.argv[2]}/no-developer.json", "w"))
scopes = json.loads(json.dumps(doc)); scopes["bots"][0]["scopes"] = ["everyone"]
json.dump(scopes, open(f"{sys.argv[2]}/bad-scope.json", "w"))
PY
for case in no-developer bad-scope; do
  if py "$here/package.py" validate --schema "$schema" "$work/$case.json" 2>"$work/err"; then
    bad "$case passed the schema"
  else
    ok "the schema refuses a manifest with $case ($(head -1 "$work/err"))"
  fi
done

# A wrong outline icon (not white on transparent) or a wrong size is refused.
py - "$here" "$zip" "$schema" "$work" <<'PY'
import io, json, sys, zipfile
sys.path.insert(0, sys.argv[1])
import package
data = open(sys.argv[2], "rb").read()
schema = json.load(open(sys.argv[3]))
def repack(**swap):
    src = zipfile.ZipFile(io.BytesIO(data)); buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, swap.get(n.replace(".", "_"), src.read(n)))
    return buf.getvalue()
red = package._png(32, 32, lambda x, y: (255, 0, 0, 255))
assert any("white on transparent" in e for e in package.check_package(repack(outline_png=red), schema))
small = package._png(96, 96, lambda x, y: (0, 0, 0, 255))
assert any("color.png is (96, 96)" in e for e in package.check_package(repack(color_png=small), schema))
print("ok   a coloured outline icon or a wrong-sized colour icon is refused")
PY
exit "$fail"
