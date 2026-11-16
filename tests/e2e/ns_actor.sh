#!/usr/bin/env bash
# Who made each namespace change, and hard links refused (control-api.md 4.1
# `actor`, decision 42; docs/design/namespace-sync.md). Ported from line B's
# tests/e2e/ns_changes.sh (PR #10: d981ee9, 81fa187) onto this line's stack.
#
# Runs on the edge VM as root, on the M5 stack left up by the M5 exit test,
# with the DC VM up (winbind names geo-writer's SID):
#
#   E2E_KEEP=1 tests/e2e/m5_exit_test.sh
#   tests/e2e/ns_actor.sh
#
# Checks, each against the catalog (file_version.actor_*, migration 0030):
#   - over NFSv3 and NFSv4.2 (kernel mounts of the wait share, sec=sys), as the AD
#     user geo-writer: mkdir, rename, unlink and rmdir carry via=nfs, the user's uid
#     and SID, proof claimed; `ln` is refused with "not supported" and links nothing;
#   - over SMB (smbd joined to the domain, a session as geo-writer): mkdir, rename,
#     rmdir and delete carry via=smb and the session's SID; an SMB hard link to a
#     local file is split into its own file (two inodes, two catalog files);
#   - a local hard link is split too; a local or SMB one to a placeholder whose bytes
#     are only in S3 is removed, the placeholder untouched (inode, marker, file id, no
#     tombstone) and still reading the object's bytes over NFS;
#   - the daemon down while a local admin runs `mv X Y; mkdir X`: Y keeps the old
#     directory's file_ids, X is new, nothing under Y is tombstoned, the changes say
#     via=unknown;
#   - the cutover report (portal-api.md 5.18) of a migration of the wait dataset's
#     bucket names geo-writer (identity_id, proof) for an S3-resident file deleted
#     over NFS.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
PY=/opt/omnifile-service/venv/bin/python
E2E="$PY $HERE/e2e.py"
DATA=/srv/omnifile/data
MNT=/mnt/omnifile-ns
WORK=/var/tmp/omnifile-ns-actor
RESULTS=${E2E_RESULTS:-$HERE/results/ns-actor-latest.md}
# The AD passwords (dev/up.sh writes them; not in git).
AD_ENV=${AD_ENV:-$REPO/dev/.secrets/ad.env}
WUID=20002
WGID=10000
NPASS=0
NFAIL=0
T0=$(date +%s)
R=$(date +%s)
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. /etc/omnifile-dev/service.env
set +a
install -m 0700 -d "$WORK"
: >"$WORK/checks.txt"

say() { printf '[ns %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
jt() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
j() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v, default=str))' "$1" "$2"; }
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 1; done
  "$@"
}
writer() { setpriv --reuid=$WUID --regid=$WGID --clear-groups -- env HOME=/tmp "$@"; }
row() { $E2E path wait "$1"; }                             # the catalog's live file at wait/PATH
fid() { j "$(row "$1")" 'd.get("file_id", "")'; }
present() { jt "$(row "$1")" 'd.get("file_id")'; }
gone() { jt "$(row "$1")" 'd == {}'; }
is_fid() { jt "$(row "$1")" "d.get('file_id') == $2"; }
committed() { jt "$(row "$1")" '(d.get("content_version_id") or 0) > 0'; }
versions() { $E2E actors "$1"; }
# last_actor FILE_ID PYEXPR: the file's last version (as `v`) satisfies PYEXPR
last_is() { jt "$(versions "$1")" "(lambda v: $2)(d['versions'][-1])"; }
first_is() { jt "$(versions "$1")" "(lambda v: $2)(d['versions'][0])"; }
nlink() { stat -c %h "$DATA/wait/$1" 2>/dev/null; }
inode() { stat -c %i "$DATA/wait/$1" 2>/dev/null; }

cleanup() {
  for m in "$MNT"/v3 "$MNT"/v4.2; do umount -f -l "$m" 2>/dev/null; done
  [ -f "$WORK/shares.conf.orig" ] && install -m 0644 "$WORK/shares.conf.orig" /etc/samba/omnifile-shares.conf &&
    systemctl try-reload-or-restart smbd 2>/dev/null
  rm -f "$WORK/smb-auth"
}
trap cleanup EXIT

SID=$(wbinfo -n geo-writer 2>/dev/null | cut -d' ' -f1)
check "winbind names geo-writer (${SID:-none}): the DC is up" test -n "$SID"
for u in omnifile-edge nfs-ganesha-omni omnifile-dev-api; do
  systemctl is-active -q "$u" || { fail "$u is not active: run E2E_KEEP=1 tests/e2e/m5_exit_test.sh first"; exit 1; }
done
# geo-writer as the identity cache has it once the directory resolved it (the dev
# domain is not one this stack's shares name, so check_new_sid would not add it).
"$PY" - "$SID" <<'PY'
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    c.execute("INSERT INTO catalog.identity (sid, kind, sam_account_name, posix_id)"
              " VALUES (%s, 'user', 'geo-writer', 20002) ON CONFLICT (sid) DO NOTHING", (sys.argv[1],))
PY
# The wait share's NFSv4 pseudo path, as the daemon renders it (M5 step 9 on).
V4_PSEUDO=${V4_PSEUDO:-/wait}
mkdir -p "$MNT/v3" "$MNT/v4.2"
mount -t nfs -o vers=3,proto=tcp,nolock,mountproto=tcp,port=2049,actimeo=1 "127.0.0.1:$DATA/wait" "$MNT/v3" ||
  fail "mount NFSv3"
mount -t nfs -o vers=4.2,proto=tcp,actimeo=1 127.0.0.1:$V4_PSEUDO "$MNT/v4.2" || fail "mount NFSv4.2"

# ------------------------------------------------------------------------------- NFS
for v in 3 4.2; do
  M=$MNT/v$v
  d=nfs$(echo "$v" | tr -d .)-$R
  writer mkdir "$M/$d" || fail "v$v: mkdir"
  if wait_for 30 present "$d"; then
    D=$(fid "$d")
    check "NFSv$v mkdir: $d's create says via=nfs, uid $WUID, geo-writer's SID, claimed (sec=sys)" \
      first_is "$D" "v['actor_via'] == 'nfs' and v['actor_uid'] == $WUID and v['actor_sid'] == '$SID' and v['actor_proof'] == 'claimed' and v['identity_sid'] == '$SID'"
  else
    fail "NFSv$v: mkdir never reached the catalog"; continue
  fi
  writer sh -c "echo nfs-$v > '$M/$d/f.txt'"
  wait_for 90 committed "$d/f.txt" || fail "v$v: $d/f.txt never committed"
  F=$(fid "$d/f.txt")
  writer mv "$M/$d/f.txt" "$M/$d/g.txt"
  check "NFSv$v rename: the same file ($F) at $d/g.txt" wait_for 30 is_fid "$d/g.txt" "$F"
  check "NFSv$v rename: via=nfs with geo-writer's SID" \
    last_is "$F" "v['kind'] == 'rename' and v['actor_via'] == 'nfs' and v['actor_sid'] == '$SID'"
  out=$(writer ln "$M/$d/g.txt" "$M/$d/h.txt" 2>&1) && fail "v$v: ln SUCCEEDED (hard links are refused)"
  check "NFSv$v ln (hard link) answers not supported and links nothing ($out)" \
    bash -c "echo '$out' | grep -qiE 'not supported|error 524' && [ ! -e '$DATA/wait/$d/h.txt' ] && [ \"\$(stat -c %h '$DATA/wait/$d/g.txt')\" = 1 ]"
  writer rm "$M/$d/g.txt" && writer rmdir "$M/$d" || fail "v$v: rm/rmdir"
  if wait_for 30 gone "$d"; then
    check "NFSv$v unlink: a tombstone with via=nfs and geo-writer's SID" \
      last_is "$F" "v['kind'] == 'tombstone' and v['actor_via'] == 'nfs' and v['actor_sid'] == '$SID'"
    check "NFSv$v rmdir: a tombstone with via=nfs and geo-writer's SID" \
      last_is "$D" "v['kind'] == 'tombstone' and v['actor_via'] == 'nfs' and v['actor_sid'] == '$SID'"
  else
    fail "NFSv$v: rmdir never reached the catalog"
  fi
done

# ------------------------------------------- local hard links (decision 42): split
echo local-bytes >"$DATA/wait/hl-$R.txt"
wait_for 90 committed "hl-$R.txt" || fail "hl-$R.txt never committed"
HF=$(fid "hl-$R.txt")
ln "$DATA/wait/hl-$R.txt" "$DATA/wait/hl-$R.lnk"
split_ok() { [ -n "$(inode "hl-$R.lnk")" ] && [ "$(inode "hl-$R.lnk")" != "$(inode "hl-$R.txt")" ] && [ "$(nlink "hl-$R.txt")" = 1 ]; }
check "a local hard link to a local file is split: the new name has its own inode" wait_for 60 split_ok
apart() { is_fid "hl-$R.txt" "$HF" && committed "hl-$R.lnk" && ! is_fid "hl-$R.lnk" "$HF"; }
check "and is committed as a separate catalog file ($HF keeps the first name)" wait_for 90 apart
check "with the same bytes" cmp -s "$DATA/wait/hl-$R.txt" "$DATA/wait/hl-$R.lnk"

# A placeholder whose bytes are only in S3: a new object of the chaos source (folder
# chaos/ns-$R), written into the catalog as the source's follower does. A second name
# for it can't be copied without a recall, and must not stay as an untracked alias of
# the managed inode: it is removed (Codex review of #25).
PHD=chaos/ns-$R
PH=$PHD/p.segy
PH_SHA=""
src=$("$PY" - <<'PY'
import os, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    r = c.execute("SELECT source_id FROM catalog.source WHERE container = 'e2e-chaos'").fetchone()
print(r[0] if r else "")
PY
)
PH_SIZE=$((2 << 20 | 4321))
put=$($E2E s3-put "ns-$R/p.segy" "$PH_SIZE" 71 --bucket e2e-chaos) && PH_SHA=$(j "$put" 'd["sha256"]')
reg=$($E2E s3-seed-one "$src" "ns-$R/p.segy" "$PH_SIZE" 2>&1) || fail "seed the new object: $reg"
ph_ready() { [ -f "$DATA/wait/$PH" ] && present "$PH"; }
marker_hex() { getfattr -e hex -n trusted.omnifile --only-values "$DATA/wait/$1" 2>/dev/null | od -An -tx1 | tr -d ' \n'; }
local_link_removed() { [ ! -e "$DATA/wait/$PHD/p.lnk" ]; }
untouched() { # the catalogued name keeps its inode, marker, file id and no tombstone
  [ "$(inode "$PH")" = "$PINO" ] && [ "$(nlink "$PH")" = 1 ] && [ "$(marker_hex "$PH")" = "$PMARK" ] &&
    is_fid "$PH" "$PF" && last_is "$PF" "v['kind'] != 'tombstone'"
}
if [ -n "$PH_SHA" ] && wait_for 90 ph_ready; then
  PF=$(fid "$PH"); PINO=$(inode "$PH"); PMARK=$(marker_hex "$PH")
  check "the new S3 object is a placeholder with no local bytes" \
    bash -c "[ -n '$PMARK' ] && [ \"\$(stat -c %b '$DATA/wait/$PH')\" = 0 ]"
  ln "$DATA/wait/$PH" "$DATA/wait/$PHD/p.lnk"
  check "a local hard link to an S3-resident placeholder is removed (decision 42)" wait_for 30 local_link_removed
  check "and the catalogued name keeps its inode, marker and file id ($PF), nothing tombstoned" untouched
else
  fail "the S3-resident placeholder $PH never appeared: $reg"
fi

# ------------------------------------------------------------------------------- SMB
# The wait share over SMB, from smbd joined to the domain (install-samba.sh), with a
# session as geo-writer: who made each change is the session's user.
if [ -f /etc/samba/omnifile-shares.conf ] && [ -r "$AD_ENV" ]; then
  cp /etc/samba/omnifile-shares.conf "$WORK/shares.conf.orig"
  cat >>/etc/samba/omnifile-shares.conf <<EOF

# tests/e2e/ns_actor.sh (removed at its end)
[nswait]
	path = $DATA/wait
	read only = no
	guest ok = no
	nt acl support = yes
	inherit acls = yes
	inherit owner = no
EOF
  systemctl restart smbd
  # shellcheck disable=SC1090
  (set -a; . "$AD_ENV"; printf 'username = geo-writer\npassword = %s\ndomain = OMNI\n' "$AD_PASSWORD_GEO_WRITER") >"$WORK/smb-auth"
  chmod 600 "$WORK/smb-auth"
  FQDN=$(hostname -f)
  # One session, kept open a few seconds after the command, as a client's would be
  # (who made a change is read from the live smbd session).
  smb() { { echo "$1"; sleep 3; echo exit; } | timeout -s KILL 60 smbclient "//$FQDN/nswait" -A "$WORK/smb-auth" -m SMB3 -t 60 2>&1 | grep -v '^smb: \\> *$'; }
  out=$(smb "mkdir smbd-$R")
  if wait_for 30 present "smbd-$R"; then
    S=$(fid "smbd-$R")
    check "SMB mkdir: via=smb with the session's SID ($out)" first_is "$S" "v['actor_via'] == 'smb' and v['actor_sid'] == '$SID'"
    out=$(smb "rename smbd-$R smbd-$R-2")
    check "SMB rename: smbd-$R-2 is the same folder ($out)" wait_for 30 is_fid "smbd-$R-2" "$S"
    check "SMB rename: via=smb with the session's SID" last_is "$S" "v['kind'] == 'rename' and v['actor_via'] == 'smb' and v['actor_sid'] == '$SID'"
    out=$(smb "rmdir smbd-$R-2")
    check "SMB rmdir ($out)" wait_for 30 gone "smbd-$R-2"
    check "SMB rmdir: a tombstone with via=smb and the session's SID" \
      last_is "$S" "v['kind'] == 'tombstone' and v['actor_via'] == 'smb' and v['actor_sid'] == '$SID'"
  else
    fail "SMB mkdir never reached the catalog: $out"
  fi
  writer sh -c "echo smb-bytes > '$MNT/v4.2/smbf-$R.txt'"
  if wait_for 90 committed "smbf-$R.txt"; then
    SF=$(fid "smbf-$R.txt")
    out=$(smb "hardlink smbf-$R.txt smbf-$R.lnk")
    smb_split() { [ -n "$(inode "smbf-$R.lnk")" ] && [ "$(inode "smbf-$R.lnk")" != "$(inode "smbf-$R.txt")" ] && [ "$(nlink "smbf-$R.txt")" = 1 ]; }
    check "an SMB hard link to a local file is split into its own inode ($out)" wait_for 60 smb_split
    smb_apart() { is_fid "smbf-$R.txt" "$SF" && committed "smbf-$R.lnk" && ! is_fid "smbf-$R.lnk" "$SF"; }
    check "and committed as a separate catalog file" wait_for 90 smb_apart
    out=$(smb "del smbf-$R.txt")
    check "SMB delete ($out)" wait_for 30 gone "smbf-$R.txt"
    check "SMB delete: a tombstone with via=smb and the session's SID" \
      last_is "$SF" "v['kind'] == 'tombstone' and v['actor_via'] == 'smb' and v['actor_sid'] == '$SID'"
  else
    fail "smbf-$R.txt never committed"
  fi
  if [ -n "${PF:-}" ]; then
    w=${PHD//\//\\}
    out=$(smb "hardlink $w\\p.segy $w\\p2.lnk")
    smb_link_removed() { [ ! -e "$DATA/wait/$PHD/p2.lnk" ]; }
    check "an SMB hard link to the S3-resident placeholder is removed ($out)" wait_for 30 smb_link_removed
    check "and the catalogued name keeps its inode, marker and file id, nothing tombstoned" untouched
  fi
else
  fail "SMB: needs smbd joined to the domain (dev/scripts/install-samba.sh) and $AD_ENV"
fi
if [ -n "${PF:-}" ]; then
  # The placeholder still reads correctly over NFS: "not yet" while its bytes are in S3
  # (the kernel client retries), then the object's bytes, fetched.
  got=$(timeout 180 setpriv --reuid=$WUID --regid=$WGID --clear-groups sha256sum "$MNT/v4.2/$PH" 2>&1 | cut -d' ' -f1)
  check "the placeholder whose links were removed still reads the S3 object's bytes over NFSv4.2" test "$got" = "$PH_SHA"
fi

# ---------------------- daemon down: a directory moved and its old name reused at once
# `mv X Y; mkdir X` by a local admin while the daemon is stopped. The catalog's X is now
# at Y (same inode) and X is a new inode: the sweep must rename X -> Y (renames before
# creates) and create the new X, and tombstone nothing under Y (decision 40: a false
# delete would reach the cutover report).
X=mvx-$R
Y=mvy-$R
mkdir -p "$DATA/wait/$X/sub" && echo one >"$DATA/wait/$X/a.txt" && echo three >"$DATA/wait/$X/sub/c.txt"
if wait_for 90 committed "$X/a.txt" && wait_for 90 committed "$X/sub/c.txt"; then
  dx=$(fid "$X"); ds=$(fid "$X/sub"); fa=$(fid "$X/a.txt"); fc=$(fid "$X/sub/c.txt")
  umount -f -l "$MNT/v3" "$MNT/v4.2" 2>/dev/null
  systemctl stop nfs-ganesha-omni
  systemctl stop omnifile-edge
  mv "$DATA/wait/$X" "$DATA/wait/$Y"
  mkdir "$DATA/wait/$X"
  systemctl start omnifile-edge
  systemctl start nfs-ganesha-omni
  check "omnifile-edge back after mv $X $Y; mkdir $X" wait_for 30 systemctl is-active -q omnifile-edge
  moved() { is_fid "$Y" "$dx" && is_fid "$Y/sub" "$ds" && is_fid "$Y/a.txt" "$fa" && is_fid "$Y/sub/c.txt" "$fc"; }
  check "mv $X $Y while the daemon was down: $Y is the old directory and its contents keep their file_ids" wait_for 90 moved
  newx() { present "$X" && ! is_fid "$X" "$dx"; }
  check "mkdir $X (the old name reused): a new catalog directory" wait_for 60 newx
  check "the moved directory's last version is a rename via=unknown, never a tombstone" \
    last_is "$dx" "v['kind'] == 'rename' and v['actor_via'] == 'unknown' and v['actor_sid'] is None"
  check "the new $X is created via=unknown" first_is "$(fid "$X")" "v['actor_via'] == 'unknown'"
  sleep 10
  alive() { for f in "$dx" "$ds" "$fa" "$fc"; do last_is "$f" "v['kind'] != 'tombstone'" || return 1; done; }
  check "nothing under $Y was tombstoned (after 10 s more)" alive
  mount -t nfs -o vers=4.2,proto=tcp,actimeo=1 127.0.0.1:$V4_PSEUDO "$MNT/v4.2" || fail "remount NFSv4.2"
else
  fail "the files under $X were never committed"
fi

# ------------------------------------------- the cutover report names who deleted it
victim=$("$PY" - <<'PY'
import os, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    r = c.execute(
        "SELECT f.name FROM catalog.file f JOIN catalog.share s ON s.name = 'wait'"
        "  JOIN catalog.file r ON r.file_id = s.root_dir_id"
        "  JOIN catalog.location l ON l.file_id = f.file_id AND l.role = 'primary'"
        "  JOIN catalog.source src ON src.backend_id = l.backend_id AND src.container = l.container"
        " WHERE f.parent_id = r.file_id AND f.deleted_at IS NULL AND f.kind = 'file'"
        " ORDER BY f.file_id LIMIT 1").fetchone()
print(r[0] if r else "")
PY
)
if [ -n "$victim" ]; then
  V=$(fid "$victim")
  writer rm "$MNT/v4.2/$victim" || fail "rm $victim over NFSv4.2"
  if wait_for 30 gone "$victim"; then
    check "the S3-resident $victim's tombstone says via=nfs, geo-writer" \
      last_is "$V" "v['kind'] == 'tombstone' and v['actor_sid'] == '$SID' and v['actor_identity_id']"
    rep=$($E2E cutover-report wait "")
    say "cutover report: $rep"
    check "the cutover report lists $victim, deleted by geo-writer's identity, proof claimed (sec=sys)" \
      jt "$rep" "any(i['file_id'] == '$V' and i['deleted_by']['identity_id'] is not None and i['deleted_by']['proof'] == 'claimed' and i['deleted_by']['kind'] == 'edge' for i in d['items'])"
    ident=$(j "$rep" "[i['deleted_by']['identity_id'] for i in d['items'] if i['file_id'] == '$V'][0]")
    check "and that identity is geo-writer's SID" \
      bash -c "$PY -c \"import os,psycopg,sys; c=psycopg.connect(os.environ['OMNIFILE_DATABASE_URL']); sys.exit(0 if c.execute('SELECT sid FROM catalog.identity WHERE identity_id = %s', ($ident,)).fetchone()[0] == '$SID' else 1)\""
  else
    fail "the delete of $victim never reached the catalog"
  fi
else
  fail "no S3-resident file left in wait/ for the cutover report"
fi

{
  echo "# Who made each namespace change, and hard links (namespace-sync.md)"
  echo
  echo "- When: $(date -u +%Y-%m-%dT%H:%M:%SZ), $(($(date +%s) - T0)) s; $(hostname), kernel $(uname -r)"
  echo "- Daemon: sha256 $(sha256sum /usr/local/bin/omnifile-edge | cut -c1-16)"
  echo "- Result: **$NPASS passed, $NFAIL failed**"
  echo
  echo "| | Check |"
  echo "|---|---|"
  cat "$WORK/checks.txt"
} >"$RESULTS"
say "results: $RESULTS ($NPASS passed, $NFAIL failed)"
[ "$NFAIL" -eq 0 ]
