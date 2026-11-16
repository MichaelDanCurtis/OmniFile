#!/usr/bin/env bash
# "Removing a user from a group revokes access within the documented cache
# time" (access-control.md, tests that prove it). Runs on the Mac: it changes
# AD on omnifile-dc and measures on omnifile-acl. Run tests/acl/matrix.sh
# (setup servers tickets) first, so the share, smbd and Ganesha are up.
#
#   tests/acl/revoke.sh            # creates, uses and deletes AD user acl-revoke
#
# The user is throwaway (uid 20099, member of Proj-Readers only while the test
# runs); its random password is passed over stdin and never printed.
# Documented bounds (edge/config/README.md): winbind cache time 300 s and
# Ganesha Manage_Gids_Expiration 300 s for NFS; SMB applies group changes to
# new sessions only (the session token is built at logon).
set -euo pipefail
DC=${DC_VM:-omnifile-dc}
VM=${ACL_VM:-omnifile-acl}
LIMIT=${REVOKE_LIMIT_S:-900}
STEP=${REVOKE_STEP_S:-15}
dc() { limactl shell --workdir / "$DC" -- sudo bash -s; }
vm() { limactl shell --workdir / "$VM" -- sudo bash -s; }
log() { printf '[revoke] %s\n' "$*"; }
# with_pw <vm>: the first stdin line ($PW) becomes $pw, the rest is the script.
with_pw() { { printf '%s\n' "$PW"; cat; } | limactl shell --workdir / "$1" -- sudo bash -c 'read -r pw; export pw; exec bash -s'; }

PW="$(head -c 48 /dev/urandom | base64 | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c1-20)aA1-"
cleanup() {
  dc <<'EOF' >/dev/null 2>&1 || true
samba-tool user delete acl-revoke
EOF
  vm <<'EOF' >/dev/null 2>&1 || true
rm -f /root/acl-auth/acl-revoke
EOF
}
trap cleanup EXIT

log "creating acl-revoke (uid 20099) in Proj-Readers on $DC"
with_pw "$DC" <<'EOF' >/dev/null
samba-tool user delete acl-revoke >/dev/null 2>&1 || true
nis=$(samba-tool domain info 127.0.0.1 2>/dev/null | awk -F': ' '/^Netbios domain/{print tolower($2)}')
samba-tool user create acl-revoke "$pw" --uid-number=20099 --gid-number=10000 \
  --login-shell=/bin/false --unix-home=/nonexistent --nis-domain="${nis:-omni}" >/dev/null
samba-tool group addmembers Proj-Readers acl-revoke >/dev/null
EOF
with_pw "$VM" <<'EOF'
umask 077; install -d -m 700 /root/acl-auth
printf 'username = acl-revoke\npassword = %s\ndomain = OMNI\n' "$pw" >/root/acl-auth/acl-revoke
net cache flush >/dev/null 2>&1
EOF
unset PW

probe() { # -> "nfs=<OK|DENIED> smb=<OK|DENIED>"
  vm <<'EOF'
mountpoint -q /mnt/acl/sys || mount -t nfs -o vers=4.2,sec=sys,noac,lookupcache=none acl1.omni.test:/proj-sys /mnt/acl/sys
n=$(setpriv --reuid=20099 --regid=10000 --clear-groups --inh-caps=-all timeout 20 dd if=/mnt/acl/sys/resident.txt of=/dev/null status=none 2>/dev/null && echo OK || echo DENIED)
o=$(timeout 20 smbclient //acl1.omni.test/proj -A /root/acl-auth/acl-revoke -c 'get resident.txt /dev/null' 2>&1)
if grep -q NT_STATUS <<<"$o"; then s=DENIED; else s=OK; fi
echo "nfs=$n smb=$s"
EOF
}

for _ in $(seq 1 20); do
  st=$(probe); [ "$st" = "nfs=OK smb=OK" ] && break; sleep 3
done
log "member: $st"
[ "$st" = "nfs=OK smb=OK" ] || { log "FAIL: member could not read ($st)"; exit 1; }

dc <<'EOF' >/dev/null
samba-tool group removemembers Proj-Readers acl-revoke >/dev/null
EOF
t0=$(date +%s)
log "removed from Proj-Readers at $(date +%T); probing every ${STEP}s (limit ${LIMIT}s)"
nfs_t="" smb_t=""
while :; do
  st=$(probe); now=$(( $(date +%s) - t0 ))
  log "t+${now}s $st"
  [ -z "$nfs_t" ] && [[ $st == *nfs=DENIED* ]] && nfs_t=$now
  [ -z "$smb_t" ] && [[ $st == *smb=DENIED* ]] && smb_t=$now
  [ -n "$nfs_t" ] && [ -n "$smb_t" ] && break
  [ "$now" -ge "$LIMIT" ] && break
  sleep "$STEP"
done
res() { # cell limit got
  local v=FAIL; [ -n "$3" ] && [ "$3" -le "$2" ] && v=PASS
  printf '%s\t<=%ss\t%s\t%s\n' "$1" "$2" "$([ -n "$3" ] && echo "${3}s" || echo never)" "$v"
}
lines="$(res "revoke: NFS sec=sys new access denied after group removal" 600 "$nfs_t")
$(res "revoke: SMB new session denied after group removal" 600 "$smb_t")"
printf '%s\n' "$lines"
printf '%s\n' "$lines" | limactl shell --workdir / "$VM" -- sudo tee -a /var/tmp/acl-matrix/results.tsv >/dev/null
log "NFS revoked after ${nfs_t:-never}s, SMB (new session) after ${smb_t:-never}s"
