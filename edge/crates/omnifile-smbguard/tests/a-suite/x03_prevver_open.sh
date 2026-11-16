#!/bin/bash
# M5, decision 36: "Previous Versions -> Open" on a file in an unfilled label. A file opened
# directly by its @GMT path opens no directory, so the guard's directory marks (x02) never
# fire and shadow_copy2 answers NOT_FOUND. vfs_omnifile_prevver (samba-vfs/), stacked before
# shadow_copy2, opens the path's directories under the label first: the guard holds that open
# while the folder fills, and the file is there by the time shadow_copy2 looks.
# Needs the module built (samba-vfs/build.sh): $OMNIFILE_PREVVER_SO, or the default build path.
. "$(dirname "$0")/lib.sh"
begin "X3: a file opened directly in an unfilled label (vfs_omnifile_prevver)"
SO=${OMNIFILE_PREVVER_SO:-$REPO/samba-vfs/build/omnifile_prevver.so}
command -v smbclient > /dev/null || { fail "smbclient not installed"; end_check; }
[ -f "$SO" ] || { fail "module not built: $SO (samba-vfs/build.sh)"; end_check; }
# smbd loads VFS modules by name, from its MODULESDIR, as on the appliance.
DEST=$(smbd -b | sed -nE 's/^ *MODULESDIR: *(.*)$/\1/p')/vfs/omnifile_prevver.so
had=0; [ -f "$DEST" ] && had=1
# On the appliance the module is already there, on a read-only /usr (the R3 gate runs this
# suite in the image it releases, with OMNIFILE_PREVVER_SO set to that module).
if [ "$had" = 1 ] && cmp -s "$SO" "$DEST"; then
  evidence "module already installed: $DEST"
else
  "$REPO/samba-vfs/install.sh" "$SO" > /dev/null || { fail "install.sh failed"; end_check; }
fi
mkdir -p "$STATE/ctl"
policy wait; delay 0
echo fill > "$STATE/dirpolicy"
rm -f "$STATE/populates.log"
harness_args "--server-comm smbd"; restart_guard || fail "guard did not restart"
N=$$
watch() {
  local n=$RANDOM$RANDOM
  echo "$1" > "$STATE/ctl/watchdir.$n"
  wait_until 10 test -f "$STATE/ctl/watchdir.$n.done" || { echo "no answer"; return; }
  cat "$STATE/ctl/watchdir.$n.done"
}
unfilled() { "$T" dirmarker "$1" | grep -q "unpopulated=true"; }
# One live folder and one rewind area per share; each label is an unfilled directory, marked
# as the daemon marks them. The harness's fill writes filled.txt into the folder it fills.
share() { # share NAME VFS_OBJECTS
  mkdir -p "$DATA/pv/$1.$N/live" "$DATA/pv/$1.$N/snap"
  cat >> "$STATE/samba/smb.conf" << EOF
[$1]
  path = $DATA/pv/$1.$N/live
  guest ok = yes
  read only = yes
  force user = root
  vfs objects = $2
  shadow:snapdir = $DATA/pv/$1.$N/snap
  shadow:basedir = $DATA/pv/$1.$N/live
  shadow:format = %Y-%m-%dT%H%MZ
  shadow:localtime = no
  shadow:sort = desc
EOF
}
label() { # label SHARE LABEL
  "$T" mkudir "$DATA/pv/$1.$N/snap/$2" --seed "$3" > /dev/null
  expect "mark on $1/$2" "$(watch "pv/$1.$N/snap/$2")" ok
}
cp "$STATE/samba/smb.conf" "$STATE/samba/smb.conf.x03"
share pvc "shadow_copy2"
share pvm "omnifile_prevver shadow_copy2"
label pvc 2026-10-14T0000Z 931
label pvm 2026-10-14T0000Z 932
label pvm 2026-10-13T0000Z 933
systemctl restart "$SMBU"
wait_until 15 bash -c "[ \"\$(systemctl is-active $SMBU)\" = active ]" || fail "test smbd did not start"
wait_until 60 smb_listening || fail "test smbd never listened on $SMBPORT"
GET() { # GET SHARE GMTPATH OUT
  rm -f "$3"
  timeout -s KILL 120 smbclient "//127.0.0.1/$1" -p "$SMBPORT" -N -m SMB3 -t 100 -c "get $2 $3" > "$LOGS/x03-smb.out" 2>&1
}

section "without the module: NOT_FOUND, and the label stays unfilled (the gap)"
GET pvc @GMT-2026.10.14-00.00.00/filled.txt "$LOGS/x03-c.out"; rc=$?
evidence "$(grep -m1 NT_STATUS "$LOGS/x03-smb.out")"
expect "smbclient get fails" "$(( rc != 0 ))" 1
grep -q NT_STATUS_OBJECT_PATH_NOT_FOUND "$LOGS/x03-smb.out" || grep -q NT_STATUS_OBJECT_NAME_NOT_FOUND "$LOGS/x03-smb.out" \
  && pass "NOT_FOUND" || fail "not NOT_FOUND"
unfilled "$DATA/pv/pvc.$N/snap/2026-10-14T0000Z" && pass "still unfilled" || fail "filled without the module"

section "with the module: the label fills for the open, and the file reads"
delay 1500
T0=$(date +%s.%N)
GET pvm @GMT-2026.10.14-00.00.00/filled.txt "$LOGS/x03-m.out"; rc=$?
el=$(awk "BEGIN{print $(date +%s.%N) - $T0}")
evidence "$(tail -n 1 "$LOGS/x03-smb.out")"
expect "smbclient get" "$rc" 0
expect "the file's content" "$(cat "$LOGS/x03-m.out" 2> /dev/null)" filled
expect_ge "waited for the fill (s)" "$el" 1.2
unfilled "$DATA/pv/pvm.$N/snap/2026-10-14T0000Z" && fail "label still unfilled" || pass "the label is filled"
l=$(grep "path=$DATA/pv/pvm.$N/snap/2026-10-14T0000Z\$" "$STATE/populates.log" | head -n 1)
evidence "$l"
grep -q "comm=smbd" <<< "$l" && pass "filled for smbd's open (through the module)" || fail "fill not asked by smbd"
unfilled "$DATA/pv/pvm.$N/snap/2026-10-13T0000Z" && pass "another label is left alone" || fail "another label was filled"

section "filled already: no second fill, and no wait"
delay 0
T0=$(date +%s.%N)
GET pvm @GMT-2026.10.14-00.00.00/filled.txt "$LOGS/x03-m2.out"; rc=$?
el=$(awk "BEGIN{print $(date +%s.%N) - $T0}")
expect "smbclient get" "$rc" 0
expect_le "not held (s)" "$el" 1
expect "one fill in all" "$(grep -c "path=$DATA/pv/pvm.$N/snap/2026-10-14T0000Z\$" "$STATE/populates.log")" 1

section "a fill that fails: an SMB error, and smbd keeps serving"
echo fail > "$STATE/dirpolicy"
GET pvm @GMT-2026.10.13-00.00.00/filled.txt "$LOGS/x03-f.out"; rc=$?
evidence "$(grep -m1 NT_STATUS "$LOGS/x03-smb.out")"
expect "smbclient get fails" "$(( rc != 0 ))" 1
grep -q NT_STATUS "$LOGS/x03-smb.out" && pass "the client gets an NT_STATUS error" || fail "no NT_STATUS error"
echo fill > "$STATE/dirpolicy"
GET pvm @GMT-2026.10.14-00.00.00/filled.txt "$LOGS/x03-m3.out"; rc=$?
expect "the share still serves (smbd alive)" "$rc" 0

section "a path that walks out of the label opens nothing outside it"
GET pvm "@GMT-2026.10.14-00.00.00/../../snap/2026-10-13T0000Z/x" "$LOGS/x03-t.out"; rc=$?
expect "smbclient get fails" "$(( rc != 0 ))" 1
unfilled "$DATA/pv/pvm.$N/snap/2026-10-13T0000Z" && pass "the other label was not opened" || fail "a .. path filled another label"

systemctl stop "$SMBU"
mv "$STATE/samba/smb.conf.x03" "$STATE/samba/smb.conf"
[ "$had" = 1 ] || "$REPO/samba-vfs/install.sh" --uninstall > /dev/null
harness_args ""; restart_guard
end_check
