#!/usr/bin/env bash
# omnifile-setup's domain join and admin steps against a real Samba AD DC
# (docs/design/setup-wizard.md, "Tests"). Two containers on their own network:
#   dc1    a Samba AD DC for CORP.EXAMPLE (RFC2307), with the group "filer admins"
#          (gidNumber 20100), its member alice (uid 20001) and a non-member bob;
#   edge1  the member: systemd as PID 1, omnifile-setup apply --only ad,admins.
# Then: the join holds, winbind maps alice to her RFC2307 ids, sudo is for the
# group only, sshd allows only the group, the password is nowhere on disk, and
# a rerun changes nothing.
#
#   edge/crates/omnifile-setup/tests/ad/run.sh
#   BIN=path/to/omnifile-setup   (default edge/target/release/omnifile-setup)
#   CGROUP_DIR=...               a cgroup2 mount for the member's systemd
#                                (default /sys/fs/cgroup)
#   KEEP=1                       leave the containers running afterwards
# Needs docker; runs privileged containers.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
EDGE=$(cd "$HERE/../../../.." && pwd)
BIN=${BIN:-$EDGE/target/release/omnifile-setup}
CGROUP_DIR=${CGROUP_DIR:-/sys/fs/cgroup}
IMG=omnifile-ad-test
NET=omnifile-ad-net
ADMIN_PW='Adm1n-Pass-Test!'
log() { printf '[ad-test] %s\n' "$*"; }

[ -x "$BIN" ] || { echo "no $BIN (cargo build --release -p omnifile-setup)" >&2; exit 2; }
cleanup() {
  [ "${KEEP:-}" = 1 ] && [ "${1:-}" = exit ] && { log "KEEP=1: containers omni-ad-dc and omni-ad-edge left running"; return; }
  docker rm -f omni-ad-dc omni-ad-edge >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap 'cleanup exit' EXIT
cleanup

log "image"
# AD_TEST_BASE: another Ubuntu 26.04 image to build on (a local one, offline).
docker build -q -t "$IMG" --build-arg "BASE=${AD_TEST_BASE:-ubuntu:26.04}" -f "$HERE/Containerfile" "$HERE" >/dev/null
docker network create --subnet 172.30.9.0/24 "$NET" >/dev/null

log "domain controller"
docker run -d --name omni-ad-dc --hostname dc1 --network "$NET" --ip 172.30.9.10 \
  --privileged "$IMG" sleep infinity >/dev/null
docker exec -e PW="$ADMIN_PW" omni-ad-dc bash -euc '
  rm -f /etc/samba/smb.conf
  samba-tool domain provision --realm=CORP.EXAMPLE --domain=CORP --server-role=dc \
    --dns-backend=SAMBA_INTERNAL --use-rfc2307 --adminpass="$PW" --host-ip=172.30.9.10 \
    --option="dns forwarder = 127.0.0.11" >/tmp/provision.log
  cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
  echo "nameserver 127.0.0.1" >/etc/resolv.conf
  samba -D
  for i in $(seq 1 60); do host -t SRV _ldap._tcp.corp.example 127.0.0.1 >/dev/null 2>&1 && break; sleep 1; done
  samba-tool group addunixattrs "Domain Users" 20000 >/dev/null
  samba-tool group add "filer admins" --gid-number=20100 --nis-domain=corp >/dev/null
  samba-tool user create alice "Al1ce-Pass!" --uid-number=20001 --gid-number=20100 \
    --login-shell=/bin/bash --unix-home=/home/alice --nis-domain=corp >/dev/null
  samba-tool user create bob "B0b-Pass!!" --uid-number=20002 --gid-number=20000 \
    --login-shell=/bin/bash --unix-home=/home/bob --nis-domain=corp >/dev/null
  samba-tool group addmembers "filer admins" alice >/dev/null
'

log "member (systemd)"
docker run -d --name omni-ad-edge --hostname edge1 --network "$NET" --ip 172.30.9.20 \
  --dns 172.30.9.10 --dns-search corp.example --privileged --cgroupns=host \
  -v "$CGROUP_DIR:/sys/fs/cgroup" --tmpfs /run --tmpfs /run/lock \
  -v "$BIN:/usr/local/bin/omnifile-setup:ro" "$IMG" /sbin/init >/dev/null
for _ in $(seq 1 60); do
  docker exec omni-ad-edge systemctl is-system-running 2>/dev/null | grep -Eq 'running|degraded' && break
  sleep 1
done
docker exec -i omni-ad-edge bash -c 'cat >/root/answers.toml' <<'TOML'
version = 1
hostname = "edge1"
[network]
mode = "dhcp"
[data]
device = "/dev/vdb"
[ad]
domain = "corp.example"
workgroup = "CORP"
join_user = "Administrator"
admin_group = 'CORP\filer admins'
TOML

fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }
said() { # said FILE REGEX NAME: the run's output has the line
  if grep -q "$2" "$1"; then ok "$3"; else bad "$3"; fi
}
check() { # check NAME CMD... : the command must succeed
  local name=$1; shift
  if out=$(docker exec omni-ad-edge bash -c "$*" 2>&1); then ok "$name"; else bad "$name"; printf '%s\n' "$out" | tail -5 | sed 's/^/     /'; fi
}

log "first run"
docker exec -e OMNIFILE_JOIN_PASSWORD="$ADMIN_PW" omni-ad-edge \
  omnifile-setup apply --answers /root/answers.toml --only ad,admins,recovery | tee /tmp/ad-first.log || true
said /tmp/ad-first.log 'ad *done: .*joined the domain' "joined"
said /tmp/ad-first.log 'admins *done: ' "admins set up"
check "the join holds" 'net ads testjoin | grep -q "Join is OK"'
check "winbind trusts the DC" 'wbinfo -t'
# The primary group comes from AD's primaryGroupID (Domain Users), as winbind does.
check "alice has her RFC2307 ids and the admin group" 'id alice | grep -q "uid=20001(alice) gid=20000(domain users) .*20100(filer admins)"'
check "and her AD login shell (admins need one)" 'getent passwd alice | grep -q ":/bin/bash$"'
check "the admin group resolves" 'getent group "filer admins" | grep -q ":20100:"'
check "alice may sudo" 'sudo -l -U alice | grep -q "(ALL : ALL) ALL"'
check "bob may not" '! sudo -l -U bob | grep -q "(ALL : ALL) ALL"'
check "sshd allows only the group" 'mkdir -p /run/sshd; sshd -T | grep -qi "^allowgroups filer admins$" && sshd -T | grep -qi "^permitrootlogin no$"'
check "alice gets a Kerberos ticket" 'echo "Al1ce-Pass!" | kinit alice@CORP.EXAMPLE >/dev/null && klist | grep -q "alice@CORP.EXAMPLE"'
check "the password is nowhere on disk" "! grep -rqsF -- '$ADMIN_PW' /etc /var/lib/omnifile /root"
# shellcheck disable=SC2016 # expanded in the container
check "the answers are kept, 0600" 'test "$(stat -c %a /etc/omnifile/setup.toml)" = 600'

log "break-glass account"
said /tmp/ad-first.log 'recovery *skipped: omnifile-recovery stays locked' "with no one to show a password to, it stays locked"
check "locked until sealed" 'passwd -S omnifile-recovery | grep -q "^omnifile-recovery L "'
RPW=$(docker exec omni-ad-edge omnifile-setup recovery seal | awk '/shown only now/ { getline; getline; print $1 }')
if [ "${#RPW}" -eq 35 ]; then ok "sealing shows a new password once"; else bad "sealing shows a new password once"; fi
check "sealed" 'passwd -S omnifile-recovery | grep -q "^omnifile-recovery P "'
check "the password works" "perl -e 'my \$h = (getpwnam(\"omnifile-recovery\"))[1]; exit(crypt(\$ARGV[0], \$h) eq \$h ? 0 : 1)' '$RPW'"
check "and is nowhere on disk" "! grep -rqsF -- '$RPW' /etc /var/lib /root /home"
check "the console only: ssh refuses it" 'mkdir -p /run/sshd; sshd -T | grep -qi "^denyusers omnifile-recovery$"'
check "it may sudo" 'sudo -l -U omnifile-recovery | grep -q "(ALL : ALL) ALL"'
check "not used yet" '! test -e /var/lib/omnifile/recovery/used'
docker exec omni-ad-edge su - omnifile-recovery -c true >/dev/null 2>&1 || true
check "a session of it is marked" 'test -e /var/lib/omnifile/recovery/used'
check "and logged at auth.alert" 'journalctl -t omnifile-recovery -p alert --no-pager | grep -q "opened a session"'
check "status says to seal it again" 'omnifile-setup recovery status | grep -q "used since it was last sealed"'
check "sealing again clears it" 'omnifile-setup recovery seal >/dev/null && ! test -e /var/lib/omnifile/recovery/used'
check "other users are not marked" 'su - alice -c true >/dev/null 2>&1; ! test -e /var/lib/omnifile/recovery/used'

log "rerun"
docker exec omni-ad-edge omnifile-setup apply --answers /root/answers.toml --only ad,admins,recovery | tee /tmp/ad-second.log || true
said /tmp/ad-second.log 'ad *already as asked' "a rerun leaves the join alone"
said /tmp/ad-second.log 'admins *already as asked' "and the admin files"
said /tmp/ad-second.log 'recovery *already as asked' "and the sealed recovery account"
exit $fail
