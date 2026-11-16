#!/usr/bin/env bash
# Boot the appliance disk once and check what the image promises (decision 38;
# appliance/README.md "Tests"):
#   /usr is the verity-backed erofs, read-only; root is ext4 and grew to fill the disk;
#   the smb-guard kernel check passed; the edge unit waits for its config; Ganesha
#   resolves all its libraries; smbd has vfs_omnifile_prevver; tty1 offers setup
#   (no login yet); nothing failed.
#   The annex copier (docs/design/annex.md §3.2): omnifile-annex runs, both its units are
#   installed and load, the preset enables the supervisor, which waits for setup (no restart
#   loop), the NAS mounts' tools are there (mount.cifs, mount.nfs, cifs.upcall with its
#   request-key rule, kinit), a control in a namespace of its own with each removed finds none,
#   and an annex unit, as sandboxed, mounts read-only where no other process sees it.
#   The IAM Roles Anywhere helper is in the image (the pinned binary, on the read-only
#   /usr), idle until provisioned; then, provisioned with a test trust anchor and a key
#   sealed by systemd-creds (to the vTPM when swtpm is installed), it starts and serves
#   credentials on 127.0.0.1:9911 the IMDSv2 way, from a local stand-in for the Roles
#   Anywhere endpoint (tests/ra-stub.py on the host): offline, no AWS.
#   The box made its own SSH host keys (the image ships none); the written GPL source offer
#   names this release's source, next to the GPL/LGPL texts and every package's copyright.
#   Ganesha, as its unit runs it with the wizard's base config, is active and keeps its NFSv4
#   recovery records on the root partition; a record written before a reboot is still there
#   after it, and Ganesha starts again in grace (a second boot).
#
#   appliance/tests/smoke.sh [DISK]    (default appliance/build/out/omnifile-edge_*.raw)
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISKS=("$HERE"/../build/out/omnifile-edge_*.raw)
DISK=${1:-${DISKS[0]}}
WORK=${WORK:-$HERE/../build/smoke}

RA_PORT=${RA_PORT:-8443}
mkdir -p "$WORK"
rm -f "$WORK/ra-stub.log"
python3 "$HERE/ra-stub.py" "$RA_PORT" "$WORK/ra-stub.log" >"$WORK/ra-stub.out" 2>&1 &
STUB=$!
GUEST=$(mktemp)
trap 'kill $STUB 2>/dev/null || true; rm -f "$GUEST" "$GUEST.unit"' EXIT
{
  echo '[ -n "$BASH_VERSION" ] || exec /bin/bash "$0" "$@"'
  echo "RA_PORT=$RA_PORT"
  cat "$HERE/ra-guest.sh"
} >"$GUEST"
cat >>"$GUEST" <<'SH'
# Two boots: everything below on the first, then Ganesha again after a reboot.
sd=/var/lib/omnifile-smoke; mkdir -p $sd
n=$(( $(cat $sd/n 2>/dev/null || echo 0) + 1 )); echo $n >$sd/n
# Ganesha as the appliance runs it (nfs-ganesha-omni.service: its ExecStart, capabilities and
# limits) with the setup wizard's base config and no exports, but without the guard units it
# binds to, which wait for an enrolled edge. Its NFSv4 recovery root must be on the root
# partition: the clients' grace records outlive a reboot and an A/B update there.
ganesha() {
  sed -e '/^Requires=omnifile-edge.service/d' -e '/^BindsTo=omnifile-edge.service/d' \
    -e 's/^After=omnifile-edge.service /After=/' /usr/lib/systemd/system/nfs-ganesha-omni.service \
    >/run/systemd/system/smoke-ganesha.service
  # Its appliance drop-ins too (NLM's), all but the guard's.
  mkdir -p /run/systemd/system/smoke-ganesha.service.d
  for f in /usr/lib/systemd/system/nfs-ganesha-omni.service.d/*.conf; do
    case ${f##*/} in 10-omnifile-guard.conf) ;; *) cp "$f" /run/systemd/system/smoke-ganesha.service.d/ ;; esac
  done
  if [ ! -s /etc/ganesha/ganesha.conf ]; then
    mkdir -p /etc/ganesha
    cp /usr/share/omnifile/ganesha-base.conf /etc/ganesha/omnifile-base.conf
    printf '%%include "/etc/ganesha/omnifile-base.conf"\n' >/etc/ganesha/ganesha.conf
  fi
  systemctl daemon-reload
  systemctl start smoke-ganesha.service
  for _ in 1 2 3 4 5 6 7 8 9 10; do sleep 1; done
  echo "ganesha$n=$(systemctl is-active smoke-ganesha.service) root_fs=$(findmnt -n -o FSTYPE --target /var/lib/nfs/ganesha) compiled_default=$(readlink -f /opt/ganesha/var/lib/nfs/ganesha) grace=$(journalctl -b -u smoke-ganesha -o cat | grep -c 'IN GRACE') fatal=$(journalctl -b -u smoke-ganesha -o cat | grep -ciE 'fatal|recovery.*(fail|could not)') dbus_fail=$(journalctl -b -u smoke-ganesha -o cat | grep -c 'server bus reg failed')"
  echo "ganesha${n}_tree=$(cd /var/lib/nfs/ganesha && find . -mindepth 1 | sort | tr '\n' ' ')"
  echo "nlm$n: statd=$(systemctl is-active rpc-statd.service) sm=$(stat -c '%U %a' /var/lib/nfs/sm 2>&1) sm_notify=$(systemctl show -p ExecStartPre --value smoke-ganesha.service | grep -o 'sm-notify.*status=[0-9]*' | sed 's/.*status=/status=/')"
  journalctl -b -u smoke-ganesha -o cat | grep -iE 'recov|grace|fatal|crit' | head -6 | sed "s/^/  ganesha$n: /"
}
keypairs() { # every host key's .pub must be its private key's public half
  local k bad=0 all=0
  for k in /etc/ssh/ssh_host_*_key; do
    [ -e "$k" ] || continue
    all=$((all + 1))
    [ "$(ssh-keygen -y -f "$k" 2>/dev/null | cut -d' ' -f1,2)" = "$(cut -d' ' -f1,2 "$k.pub" 2>/dev/null)" ] || bad=$((bad + 1))
  done
  echo "pairs=$all mismatched=$bad sshd_keygen=$(systemctl show -p ConditionResult --value sshd-keygen.service)/$(systemctl is-active sshd-keygen.service)"
}
if [ "$n" -ge 2 ]; then
  echo "SMOKE boot=$n"
  echo "keys2: $(keypairs)"
  # Units that compare mtimes (ConditionNeedsUpdate), run at every boot instead.
  echo "every_boot: ldconfig=$(systemctl show -p ConditionResult --value ldconfig.service)/$(systemctl show -p Result --value ldconfig.service) catalog=$(systemctl show -p ConditionResult --value systemd-journal-catalog-update.service) hwdb_in_usr=$(test -s /usr/lib/udev/hwdb.bin && echo yes || echo no)"
  ganesha
  echo "record_kept=$(find /var/lib/nfs/ganesha -name 'smoke-client-record*' | wc -l)"
  # A unit ordering cycle makes systemd drop a job at random (it dropped ssh.socket once).
  echo "boot2: ssh=$(systemctl is-active ssh.socket) firewall=$(systemctl is-active omnifile-firewall.service) cycles=$(journalctl -b -o cat | grep -c 'ordering cycle')"
  systemctl stop smoke-ganesha.service
  # Keys from an image (boot 1 removed this box's marker, as on a box updated from an image
  # that shipped shared keys): replaced at this boot, once, and the marker written.
  echo "rekey: before=$(cat $sd/ssh-fp) now=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}') marker=$(test -s /etc/ssh/omnifile-own-host-keys && echo yes)"
  # The first boot removed the statd user: it is back, from /usr (sysusers.d) at this boot.
  echo "statd_user: removed=$(cat $sd/statd-removed) back=$(getent passwd statd | cut -d: -f1,6,7) sysusers=$(systemctl show -p ConditionResult --value systemd-sysusers.service)"
  # Fail closed: with the firewall unable to load, the network and the listeners don't start.
  # Its rules made to fail, then a restart (Requires= restarts the network and the listeners
  # with it; each start waits for the firewall's, which fails).
  mkdir -p /run/systemd/system/omnifile-firewall.service.d
  { echo '[Service]'; echo 'ExecStart='; echo 'ExecStart=/bin/false'; } >/run/systemd/system/omnifile-firewall.service.d/99-smoke-broken.conf
  systemctl daemon-reload
  systemctl restart omnifile-firewall.service 2>/dev/null
  systemctl start systemd-networkd.service 2>/dev/null; systemctl start ssh.socket 2>/dev/null
  sleep 2
  echo "failclosed: firewall=$(systemctl is-active omnifile-firewall.service) table=$(nft list table inet omnifile >/dev/null 2>&1 && echo yes || echo no) networkd=$(systemctl is-active systemd-networkd.service) ssh=$(systemctl is-active ssh.socket)"
  echo SMOKE-END
  systemctl --no-block poweroff
  exit 0
fi
echo SMOKE-BEGIN
. /usr/lib/os-release
echo "version=$IMAGE_VERSION"
echo "usr=$(findmnt -n -o SOURCE,FSTYPE,OPTIONS /usr)"
echo "root=$(findmnt -n -o FSTYPE /) $(df --output=size -B1G / | tail -1 | tr -d ' ')G"
echo "kernel=$(uname -r)"
echo "kernel_check=$(systemctl is-active omnifile-kernel-check.service)"
journalctl -b -u omnifile-kernel-check -o cat | grep '^kernel-check' | sed 's/^/  /'
echo "edge_condition=$(systemctl show -p ConditionResult --value omnifile-edge.service)"
echo "signing_helper=$(systemctl is-enabled omnifile-aws-signing-helper.service) condition=$(systemctl show -p ConditionResult --value omnifile-aws-signing-helper.service)"
echo "edge_binary=$(/usr/local/bin/omnifile-edge --version)"
# The annex copier (docs/design/annex.md §3.2): its binary, both units, the supervisor enabled
# by the preset, and what its NAS mounts use (mount.cifs and mount.nfs; for SMB Kerberos the
# kernel's request-key upcall runs cifs.upcall, from cifs.spnego.conf; kinit for the ticket).
# annex_probe looks at the files; annex_systemd at systemd's view of the units.
annex_probe() { # annex_probe TAG
  local v u t p
  hash -r
  v=$(/usr/local/bin/omnifile-annex --version 2>/dev/null) || v=none
  echo "$1: bin=${v// /-} libs_missing=$(ldd /usr/local/bin/omnifile-annex 2>&1 | grep -c 'not found') units=$(for u in omnifile-annex.service omnifile-annex@.service; do test -s /usr/lib/systemd/system/$u && printf '%s,' $u; done) preset_link=$([ "$(readlink -f /etc/systemd/system/multi-user.target.wants/omnifile-annex.service)" = /usr/lib/systemd/system/omnifile-annex.service ] && echo yes || echo no) gssd_unit=$(test -s /usr/lib/systemd/system/rpc-gssd.service && grep -qE '^Wants=.*\brpc-gssd\.service' /usr/lib/systemd/system/omnifile-annex@.service && echo wanted || echo no) helpers=$(for t in mount.cifs mount.nfs mount.nfs4 cifs.upcall request-key kinit rpc.gssd; do p=$(command -v $t) && [ -f "$p" ] && [ -x "$p" ] && printf '%s,' $t; done) spnego=$(grep -qsE '^create +cifs\.spnego .*/cifs\.upcall' /etc/request-key.d/cifs.spnego.conf && echo yes || echo no)"
}
annex_probe annex
echo "annex_systemd: loads=$(for u in omnifile-annex.service omnifile-annex@smoke.service; do [ "$(systemctl show -p LoadState --value $u)" = loaded ] && printf '%s,' $u; done) enabled=$(systemctl is-enabled omnifile-annex.service 2>&1) gssd_wanted_by=$(systemctl list-dependencies --reverse --plain --no-legend rpc-gssd.service 2>/dev/null | sed -n '2,$p' | tr -d ' ' | tr '\n' ,) verify=$(systemd-analyze verify --recursive-errors=no /usr/lib/systemd/system/omnifile-annex.service omnifile-annex@smoke.service 2>&1 | grep -c .)"
echo "annex_supervisor: active=$(systemctl is-active omnifile-annex.service) condition=$(systemctl show -p ConditionResult --value omnifile-annex.service) restarts=$(systemctl show -p NRestarts --value omnifile-annex.service)"
# A control: the same probe, in a mount namespace of its own where each of those is removed (an
# empty file bound over it on the read-only /usr, an empty directory over the preset's links),
# must find none of them. Nothing outside that namespace changes.
gone=$(for f in /usr/local/bin/omnifile-annex /usr/lib/systemd/system/omnifile-annex.service \
  /usr/lib/systemd/system/omnifile-annex@.service /etc/request-key.d/cifs.spnego.conf \
  /usr/lib/systemd/system/rpc-gssd.service \
  $(for t in mount.cifs mount.nfs mount.nfs4 cifs.upcall request-key kinit rpc.gssd; do command -v $t; done); do
  readlink -f "$f"; done | sort -u | tr '\n' ' ')
unshare -m --propagation private /bin/bash -c "$(declare -f annex_probe)
for f in $gone; do mount --bind /dev/null \$f; done
mount -t tmpfs none /etc/systemd/system/multi-user.target.wants
annex_probe annex_removed"
annex_probe annex_restored
# The per-annex unit's sandbox, as it runs (its capabilities, seccomp filter, ProtectSystem):
# it can mount under its runtime directory, read-only, and that mount is in its own namespace
# only. A tmpfs stands in for the NAS: no filer is reachable here.
mkdir -p /run/systemd/system/omnifile-annex@smoke.service.d
printf '%s\n' '[Service]' 'Restart=no' 'ExecStart=' \
  "ExecStart=/bin/sh -c 'mkdir -p /run/omnifile/annex/smoke/mnt && mount -t tmpfs -o ro,nosuid,nodev,noexec annex-smoke /run/omnifile/annex/smoke/mnt && echo annex_inside=\$\$(findmnt -n -o FSTYPE,OPTIONS /run/omnifile/annex/smoke/mnt | cut -d, -f1); sleep 8'" \
  >/run/systemd/system/omnifile-annex@smoke.service.d/99-smoke.conf
systemctl daemon-reload
# rpc-gssd's condition is checked at boot (annex_systemd's gssd_wanted_by says what else wants
# it), before any keytab: an annex unit's start must check it again, so that a box joined to
# AD since it booted gets rpc.gssd.
gssd_t0=$(systemctl show -p ConditionTimestampMonotonic --value rpc-gssd.service)
# A control: a unit ordered after rpc-gssd but without Wants= on it leaves it alone.
systemd-run -q --wait --unit=smoke-no-gssd -p After=rpc-gssd.service /bin/true
echo "annex_gssd_control: rechecked=$([ "$(systemctl show -p ConditionTimestampMonotonic --value rpc-gssd.service)" -gt "${gssd_t0:-0}" ] && echo yes || echo no)"
systemctl start omnifile-annex@smoke.service
sleep 3
echo "annex_unit: state=$(systemctl is-active omnifile-annex@smoke.service) $(journalctl -b -u omnifile-annex@smoke.service -o cat | grep -m1 '^annex_inside=') outside=$(grep -c annex-smoke /proc/self/mountinfo)"
# Its start pulled in rpc.gssd (NFS sec=krb5), which waits for the AD join's keytab.
echo "annex_gssd: wants=$(systemctl show -p Wants --value omnifile-annex@smoke.service | grep -ow rpc-gssd.service) rechecked=$([ "$(systemctl show -p ConditionTimestampMonotonic --value rpc-gssd.service)" -gt "${gssd_t0:-0}" ] && echo yes || echo no) state=$(systemctl is-active rpc-gssd.service) keytab=$(test -e /etc/krb5.keytab && echo yes || echo no) condition=$(systemctl show -p ConditionResult --value rpc-gssd.service)"
systemctl stop omnifile-annex@smoke.service
rm -rf /run/systemd/system/omnifile-annex@smoke.service.d
systemctl daemon-reload
echo "ganesha_missing=$(ldd /opt/ganesha/bin/ganesha.nfsd /opt/ganesha/lib/ganesha/libfsalomni.so | grep -c 'not found')"
echo "smbd=$(smbd --version)"
echo "prevver=$(ls "$(smbd -b | sed -n 's/^ *MODULESDIR: //p')/vfs/omnifile_prevver.so")"
echo "prevver_load=$(/usr/local/bin/omnifile-setup check-smb-module 2>&1 | tr '\n' ' ')"
echo "pubring=$(gpg --show-keys --with-colons /usr/lib/systemd/import-pubring.pgp 2>/dev/null | awk -F: '/^uid/{printf "%s; ", $10}')"
if touch /usr/.w 2>/dev/null; then echo usr_writable=yes; else echo usr_writable=no; fi
echo "setup_console=$(systemctl is-active omnifile-setup-console.service) getty1=$(systemctl is-active getty@tty1.service)"
echo "recovery=$(passwd -S omnifile-recovery 2>&1 | cut -d' ' -f1,2) home=$(stat -c '%U %a' /var/lib/omnifile-recovery 2>&1) pam=$(test -s /usr/share/pam-configs/omnifile-recovery && echo yes)"
echo "tools: $(for t in python3 openssl pkill timeout losetup mkfs.xfs setfattr smbclient; do command -v $t >/dev/null && printf '%s ' $t || printf '!%s ' $t; done) kconfig=$(ls /boot/config-* /usr/lib/modules/*/config 2>/dev/null | tr '\n' ' ')$(test -r /proc/config.gz && echo /proc/config.gz)"
H=/usr/libexec/omnifile/aws_signing_helper
echo "ra_binary=$(stat -c %a "$H" 2>&1) $(findmnt -n -o TARGET --target "$H") sha256=$(sha256sum "$H" | cut -d' ' -f1) version=$("$H" version 2>&1 | head -1) license=$(test -s /usr/share/omnifile/licenses/aws_signing_helper/LICENSE && echo yes)"
echo "failed=$(systemctl --failed --no-legend --plain | awk '{print $1}' | tr '\n' ' ')"
# The initrd's part of this boot (up to the switch-root): no unit failed there, and nothing
# from the image's /usr (the firewall's Requires= drop-ins) reached it.
ij=$(journalctl -b -o cat | sed '/Switching root/q')
echo "initrd: lines=$(wc -l <<<"$ij") switch_root=$(grep -c 'Switching root' <<<"$ij") failed=$(grep -cE 'Failed to start|Failed with result|FAILED|Dependency failed' <<<"$ij") fw_not_found=$(grep -c 'omnifile-firewall.service' <<<"$ij")"
# This box's own SSH host keys: made during this boot, so not shipped in the image (every box
# from one image would share them).
boot=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) - 5 ))
k=$(ls /etc/ssh/ssh_host_*_key 2>/dev/null | wc -l)
old=$(for f in /etc/ssh/ssh_host_*_key; do [ -e "$f" ] && [ "$(stat -c %Y "$f")" -lt "$boot" ] && echo "$f"; done | wc -l)
O=/usr/share/doc/omnifile/GPL-SOURCE-OFFER.txt
echo "offer=$(test -s $O && echo yes) url=$(grep -o 'https://github.com/[^ >]*/releases/tag/v[^ >]*' $O | head -1) left=$(grep -c '{{' $O 2>/dev/null) licences=$(for l in GPL-2 GPL-3 LGPL-2.1 LGPL-3; do test -s /usr/share/common-licenses/$l && printf '%s,' $l; done) ours=$(ls /usr/share/doc/omnifile/FSAL_OMNI/COPYING.LESSER /usr/share/doc/omnifile/vfs_omnifile_prevver/COPYING 2>/dev/null | wc -l) copyrights=$(ls /usr/share/doc/*/copyright 2>/dev/null | wc -l) samba=$(test -s /usr/share/doc/samba/copyright && echo yes) kernel=$(ls /usr/share/doc/linux-image-*/copyright 2>/dev/null | wc -l)"
echo "ssh_keys=$k made_before_boot=$old keygen=$(systemctl show -p Result --value omnifile-ssh-keygen.service) sshd_t=$(/usr/sbin/sshd -t 2>&1 && echo ok)"
echo "ssh_marker=$(test -s /etc/ssh/omnifile-own-host-keys && echo yes)"
echo "keys1: $(keypairs)"
# Nothing listens on the network before the firewall's table is loaded, and nothing listens
# that the appliance doesn't need: no LLMNR (5355) or mDNS (5353) from systemd-resolved.
fwt=$(systemctl show -p ActiveEnterTimestampMonotonic --value omnifile-firewall.service)
early=$(for u in systemd-resolved.service systemd-networkd.service ssh.socket rpcbind.socket rpcbind.service; do
  t=$(systemctl show -p InactiveExitTimestampMonotonic --value $u)
  [ "${t:-0}" -gt 0 ] && [ "$t" -lt "$fwt" ] && printf '%s ' $u
done)
echo "listen: fw_loaded=$([ "${fwt:-0}" -gt 0 ] && echo yes || echo no) early=${early:-none} mdns_llmnr=$(ss -lntuH | awk '{print $5}' | grep -cE ':(5353|5355)$') resolved=$(resolvectl status 2>/dev/null | grep -m1 -oE '[+-]LLMNR [+-]mDNS' | tr ' ' ,)"
# For the second boot: forget that this box made its keys, as on a box updated from an image
# that shipped them.
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}' >$sd/ssh-fp
rm -f /etc/ssh/omnifile-own-host-keys
# Units this image adds start from /usr (an A/B update never touches /etc, where presets live).
echo "vendor: $(for u in omnifile-firewall omnifile-ssh-keygen; do readlink -e /usr/lib/systemd/system/sysinit.target.wants/$u.service >/dev/null && printf '%s ' $u; done)"
echo "fw_requires: $(for u in systemd-networkd.service systemd-resolved.service ssh.socket smbd.service nfs-ganesha-omni.service rpcbind.socket; do systemctl show -p Requires --value $u | grep -qw omnifile-firewall.service && printf '%s ' $u; done)"
# Ubuntu's nftables.service would flush our table: it never runs, even when restarted by hand.
t0=$(nft list table inet omnifile >/dev/null 2>&1 && echo yes || echo no)
systemctl restart nftables.service 2>/dev/null
echo "nftables: enabled=$(systemctl is-enabled nftables.service 2>&1) active=$(systemctl is-active nftables.service) table_at_boot=$t0 table_after_restart=$(nft list table inet omnifile >/dev/null 2>&1 && echo yes || echo no)"
# Provision it by hand, as setup's aws step does: a key sealed by systemd-creds, a
# certificate from a test trust anchor, the ARNs, and (for this test only) the stand-in
# endpoint on the host. The test CA and the key's plain copy live in /run (RAM) only.
T=/run/ra-test; mkdir -p -m 0700 $T
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout $T/ca.key \
  -out $T/ca.pem -days 2 -subj "/CN=OmniFile test trust anchor" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign 2>/dev/null
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout $T/ra.key \
  -out $T/ra.csr -subj "/CN=omnifile-edge-smoke" 2>/dev/null
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n' >$T/ext
openssl x509 -req -in $T/ra.csr -CA $T/ca.pem -CAkey $T/ca.key -CAcreateserial -days 1 \
  -extfile $T/ext -out /etc/omnifile/roles-anywhere.pem 2>/dev/null
systemd-creds has-tpm2 >/dev/null 2>&1 && tpm=yes || tpm=no
systemd-creds encrypt --name=roles-anywhere-key --with-key=auto --tpm2-pcrs= $T/ra.key \
  /var/lib/omnifile/identity/roles-anywhere.cred
rm -f $T/ra.key $T/ca.key
echo "ra_sealed=$(test -s /var/lib/omnifile/identity/roles-anywhere.cred && echo yes) tpm=$tpm plain_key=$(grep -rlE 'BEGIN (EC |ENCRYPTED )?PRIVATE KEY' /etc /var/lib/omnifile --exclude-dir=ssh 2>/dev/null | wc -l)"
cat >/etc/omnifile/aws-signing-helper.env <<EOF
OMNIFILE_RA_TRUST_ANCHOR_ARN=arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b
OMNIFILE_RA_PROFILE_ARN=arn:aws:rolesanywhere:eu-west-1:123456789012:profile/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d
OMNIFILE_RA_ROLE_ARN=arn:aws:iam::123456789012:role/omnifile-edge-s3-read
OMNIFILE_RA_REGION=eu-west-1
OMNIFILE_RA_PORT=9911
OMNIFILE_RA_SESSION_SECONDS=3600
OMNIFILE_RA_ENDPOINT=http://10.0.2.2:$RA_PORT
EOF
systemctl start omnifile-aws-signing-helper.service
echo "ra_condition=$(systemctl show -p ConditionResult --value omnifile-aws-signing-helper.service)"
ra_serves
ganesha
# NFSv3 locking on fixed ports, and the firewall opens exactly what is registered (plus SSH,
# SMB and DHCP), nothing else.
rpc=$(rpcinfo -p 2>/dev/null | awk 'NR > 1 {print $5 "/" $3 "/" $4}' | sort -u)
echo "rpc: $(tr '\n' ' ' <<<"$rpc")"
fwc=$(nft list chain inet omnifile input 2>/dev/null)
fwt=$(sed -n 's/.*tcp dport { \(.*\) } accept.*/\1/p' <<<"$fwc" | tr -d ' ')
fwu=$(sed -n 's/.*udp dport { \(.*\) } accept.*/\1/p' <<<"$fwc" | tr -d ' ' | sed 's/^68,546$//' | grep -v '^$' | head -1)
shut=$(while IFS=/ read -r _ proto port; do
  l=$fwt; [ "$proto" = udp ] && l=$fwu
  grep -qw "$port" <<<"${l//,/ }" || printf '%s/%s ' "$port" "$proto"
done <<<"$rpc")
echo "fw: active=$(systemctl is-active omnifile-firewall.service) policy_drop=$(grep -c 'policy drop' <<<"$fwc") tcp=$fwt udp=$fwu rpc_not_open=${shut:-none}"
# The servers restart after a crash, and stay bound to the guard (fail closed).
for u in smbd nmbd winbind nfs-ganesha-omni; do
  echo "restart_$u: $(for q in Restart RestartUSec StartLimitBurst StartLimitIntervalUSec; do printf '%s ' "$(systemctl show -p $q --value $u.service)"; done)binds=$(systemctl show -p BindsTo --value $u.service | grep -ow omnifile-edge.service || echo none)"
done
# The real smbd and Ganesha can't start while the guard isn't running (here it waits for its
# config). Everything else they need is there (smb.conf, ganesha.conf), and a control run shows
# the binding is what keeps smbd down: with 10-omnifile-guard.conf shadowed by an empty
# drop-in of the same name in /run, the same start brings it up.
[ -s /etc/samba/smb.conf ] || { echo '[global]'; echo '  server role = standalone server'; } >/etc/samba/smb.conf
for u in smbd nfs-ganesha-omni; do
  echo "guard_deps_$u: $(for q in Requires BindsTo After; do systemctl show -p $q --value $u.service | grep -qw omnifile-edge.service && printf '%s ' $q; done)"
done
systemctl stop smoke-ganesha.service
for u in smbd nfs-ganesha-omni; do
  systemctl start $u.service 2>/dev/null
  sleep 3
  echo "without_guard_$u: $u=$(systemctl is-active $u.service) started=$(systemctl show -p ExecMainStartTimestampMonotonic --value $u.service) guard=$(systemctl is-active omnifile-edge.service)"
  systemctl stop $u.service 2>/dev/null
done
# smoke-control: begin
# The control drop-in unbinds smbd from the guard, so it never outlives this block: if the
# script stops part-way (an error, or the unit's timeout), the trap stops smbd and removes it.
CONTROL=/run/systemd/system/smbd.service.d/10-omnifile-guard.conf
drop_control() {
  [ -e "$CONTROL" ] || return 0
  systemctl stop smbd.service 2>/dev/null
  rm -f "$CONTROL"
  systemctl daemon-reload
}
trap drop_control EXIT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 130' INT
mkdir -p "${CONTROL%/*}"
: >"$CONTROL"
systemctl daemon-reload
systemctl start smbd.service 2>/dev/null
sleep 3
echo "control_unbound_smbd: smbd=$(systemctl is-active smbd.service) binds=$(systemctl show -p BindsTo --value smbd.service | grep -ow omnifile-edge.service || echo none)"
drop_control
trap - EXIT TERM HUP INT
# smoke-control: end
systemctl start smoke-ganesha.service
# A crash: smbd as its unit runs it (its /usr drop-ins, the restart one included), without the
# guard lines it binds to, which wait for an enrolled edge, is killed and must come back.
sed -e '/^Requires=omnifile-edge.service/d' -e '/^BindsTo=omnifile-edge.service/d' \
  /usr/lib/systemd/system/smbd.service >/run/systemd/system/smoke-smbd.service
mkdir -p /run/systemd/system/smoke-smbd.service.d
for f in /usr/lib/systemd/system/smbd.service.d/*.conf; do
  case ${f##*/} in 10-omnifile-guard.conf) ;; *) cp "$f" /run/systemd/system/smoke-smbd.service.d/ ;; esac
done
systemctl daemon-reload
systemctl start smoke-smbd.service
crash() { # crash UNIT: kill -9 its main process, then wait for systemd to bring it back
  local u=$1 p0 p1 i
  p0=$(systemctl show -p MainPID --value $u)
  [ "${p0:-0}" -gt 0 ] && kill -9 "$p0"
  for i in $(seq 1 30); do
    sleep 1
    p1=$(systemctl show -p MainPID --value $u)
    [ "$(systemctl is-active $u)" = active ] && [ "${p1:-0}" -gt 0 ] && [ "$p1" != "$p0" ] && break
  done
  echo "crash_$u: before=$p0 after=$p1 state=$(systemctl is-active $u) restarts=$(systemctl show -p NRestarts --value $u) took=${i}s"
}
crash smoke-smbd.service
systemctl stop smoke-smbd.service
crash smoke-ganesha.service
# A grace record, as a client's NFSv4 state leaves one, in the recovery directory Ganesha uses.
rd=$(cd /var/lib/nfs/ganesha && find . -mindepth 1 -maxdepth 1 -type d -name 'v4recov*' | head -1)
mkdir -p "/var/lib/nfs/ganesha/${rd:-v4recov}/smoke-client-record"
systemctl stop smoke-ganesha.service
echo "record_written=$(find /var/lib/nfs/ganesha -name 'smoke-client-record*' | wc -l) in=${rd:-v4recov}"
# As on a box updated from an image without nfs-common (no postinst made the statd user; no
# userdel in the image, so its lines go): the next boot must make it from /usr, before Ganesha
# needs it, though /usr hasn't changed.
sed -i '/^statd:/d' /etc/passwd /etc/shadow
getent passwd statd >/dev/null && echo no >$sd/statd-removed || echo yes >$sd/statd-removed
sync
systemctl --no-block reboot
SH
vm_unit "$GUEST" >"$GUEST.unit"
vm_prepare "$WORK" "$DISK" 16G
mapfile -t EXTRA < <(vm_tpm "$WORK")
TPM=no
if printf '%s\n' "${EXTRA[@]}" | grep -q tpm-tis; then TPM=yes; fi
echo "booting $DISK, TPM: $TPM (log: $WORK/serial.log)"
vm_run "$WORK" 1500 omnifile-smoke.service "$GUEST.unit" "${EXTRA[@]}" || true
R=$(sed -n '/SMOKE-BEGIN/,/SMOKE-END/p' "$WORK/serial.log" | sed 's/.*SMOKE-BEGIN/SMOKE-BEGIN/' | tr -d '\r')
printf '%s\n' "$R"

fail=0
check() { # check NAME REGEX (a here-string: with pipefail, grep -q's early exit fails a printf pipe)
  if grep -Eq "$2" <<<"$R"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
check "boots and reports" '^SMOKE-END'
check "usr is dm-verity erofs, ro" '^usr=/dev/mapper/usr erofs ro'
check "root is ext4 and grew" '^root=ext4 ([5-9]|[1-9][0-9])G'
check "kernel check passed" '^kernel_check=active'
check "edge waits for its config" '^edge_condition=no'
check "omnifile-annex is in /usr/local/bin and runs, with all its libraries" \
  '^annex: bin=omnifile-annex-[0-9][^ ]* libs_missing=0 '
check "both annex units are installed" '^annex: .* units=omnifile-annex.service,omnifile-annex@.service, '
check "the preset enables the annex supervisor (its multi-user.target link)" '^annex: .* preset_link=yes '
check "the NAS mounts' tools: mount.cifs, mount.nfs(4), cifs.upcall and its request-key rule, kinit, rpc.gssd" \
  '^annex: .* helpers=mount.cifs,mount.nfs,mount.nfs4,cifs.upcall,request-key,kinit,rpc.gssd, spnego=yes$'
check "rpc-gssd.service is installed, and the annex unit wants it (NFS sec=krb5)" '^annex: .* gssd_unit=wanted '
check "control: with each of those removed (in a namespace of its own), the same probe finds none" \
  '^annex_removed: bin=none libs_missing=0 units= preset_link=no gssd_unit=no helpers= spnego=no$'
a1=$(sed -n 's/^annex: //p' <<<"$R")
a2=$(sed -n 's/^annex_restored: //p' <<<"$R")
if [ -n "$a1" ] && [ "$a1" = "$a2" ]; then echo "ok   and outside it, after, it finds them all again"; else echo "FAIL and outside it, after, it finds them all again"; fail=1; fi
check "systemd loads both (the template as omnifile-annex@smoke), the supervisor enabled" \
  '^annex_systemd: loads=omnifile-annex.service,omnifile-annex@smoke.service, enabled=enabled '
check "systemd-analyze verify has nothing to say about either annex unit" '^annex_systemd: .* verify=0$'
check "the annex supervisor waits for the box's config, without a restart loop" \
  '^annex_supervisor: active=inactive condition=no restarts=0$'
check "an annex unit, as sandboxed, mounts read-only in its own namespace (invisible outside)" \
  '^annex_unit: state=active annex_inside=tmpfs ro outside=0$'
check "an annex unit's start pulls in rpc-gssd (its condition checked again), which waits for the AD join's keytab" \
  '^annex_gssd: wants=rpc-gssd.service rechecked=yes state=inactive keytab=no condition=no$'
check "control: a unit only ordered after rpc-gssd, without Wants=, doesn't start it" '^annex_gssd_control: rechecked=no$'
check "the Roles Anywhere helper is enabled, idle until provisioned" '^signing_helper=enabled condition=no'
. "$HERE/../third-party/aws_signing_helper/PIN"
eval "HELPER_SHA256=\$HELPER_SHA256_${VM_ARCH/-/_}"
check "the pinned aws_signing_helper ($VM_ARCH) is on the read-only /usr, with its licence" \
  "^ra_binary=755 /usr sha256=$HELPER_SHA256 version=$HELPER_VERSION license=yes"
check "ganesha has all its libraries" '^ganesha_missing=0'
check "prevver module installed" '^prevver=/.*/omnifile_prevver.so'
check "smbd loads the prevver module (built for this Samba)" '^prevver_load=ok: omnifile_prevver .* and smbd loaded it'
check "usr not writable" '^usr_writable=no'
check "updates are checked against an OmniFile key, alone" '^pubring=[^;]*OmniFile[^;]*; $'
check "setup waits on the console, not a login" '^setup_console=active getty1=inactive'
check "the break-glass account exists, locked" '^recovery=omnifile-recovery L home=omnifile-recovery 700 pam=yes'
check "no failed units" '^failed= *$'
for u in smbd nmbd winbind nfs-ganesha-omni; do
  b=none; case $u in smbd | nfs-ganesha-omni) b=omnifile-edge.service ;; esac
  rs=5s; [ $u = nfs-ganesha-omni ] && rs=2s
  check "$u restarts after a crash (on-failure, $rs, at most 5 starts in 10 min), guard binding: $b" \
    "^restart_$u: on-failure $rs 5 10min binds=$b$"
done
for u in smbd nfs-ganesha-omni; do
  check "$u requires, is bound to and starts after the guard" "^guard_deps_$u: Requires BindsTo After $"
  check "the real $u doesn't start while the guard isn't running" "^without_guard_$u: $u=inactive started=0 guard=inactive$"
done
check "control: with the guard drop-in shadowed, the same smbd start brings it up (so the binding is what kept it down)" \
  '^control_unbound_smbd: smbd=active binds=none$'
check "smbd killed with SIGKILL comes back by itself" '^crash_smoke-smbd.service: before=[1-9][0-9]* after=[1-9][0-9]* state=active restarts=1 '
check "and so does Ganesha" '^crash_smoke-ganesha.service: before=[1-9][0-9]* after=[1-9][0-9]* state=active restarts=1 '
check "and none in the initrd, where the firewall's drop-ins don't reach" '^initrd: lines=[1-9][0-9]* switch_root=1 failed=0 fw_not_found=0$'
IMAGE_V=$(sed -n 's/^version=//p' <<<"$R")
# shellcheck source=../release.conf
. "$HERE/../release.conf"
check "the written GPL source offer names this release's source ($SOURCES_REPO, v$IMAGE_V)" \
  "^offer=yes url=https://github.com/$SOURCES_REPO/releases/tag/v$IMAGE_V left=0 "
check "with the GPL and LGPL texts, our modules' licences and every package's copyright file" \
  '^offer=yes .* licences=GPL-2,GPL-3,LGPL-2.1,LGPL-3, ours=2 copyrights=[1-9][0-9]{2,} samba=yes kernel=[1-9]'
check "Ganesha, as its unit runs it with the appliance's config, is active" \
  '^ganesha1=active root_fs=ext4 compiled_default=/var/lib/nfs/ganesha grace=[1-9][0-9]* fatal=0'
check "and owns its D-Bus name (its admin interface)" '^ganesha1=active .* dbus_fail=0'
check "it keeps its recovery records on the root partition (/var/lib/nfs/ganesha)" '^ganesha1_tree=.*v4'
check "a grace record written before a reboot" '^record_written=1 '
check "is there after it, and Ganesha starts again, in grace" \
  '^ganesha2=active root_fs=ext4 compiled_default=/var/lib/nfs/ganesha grace=[1-9][0-9]* fatal=0'
check "the record is still there after the reboot" '^record_kept=[1-9]'
check "the second boot has SSH and the firewall, and no unit ordering cycle" '^boot2: ssh=active firewall=active cycles=0'
check "NLM: Ganesha brings up rpc.statd, and sm-notify runs with privileges, after the reboot too" \
  '^nlm2: statd=active sm=statd 700 sm_notify=status=0'
check "NFSv3 on fixed ports: rpcbind 111, NFS 2049, MOUNT 20048, NLM 32803, statd 32765 (rpcinfo -p)" \
  '^rpc: (.* )?mountd/tcp/20048 .*nfs/tcp/2049 .*nlockmgr/tcp/32803 .*portmapper/tcp/111 .*status/tcp/32765 '
check "the firewall drops what it doesn't name, and opens every port rpcinfo shows" \
  '^fw: active=active policy_drop=1 tcp=.* rpc_not_open=none$'
check "the box made its own SSH host keys at first boot (none came in the image)" '^ssh_keys=[3-9] made_before_boot=0 keygen=success sshd_t=ok'
check "and marked them as its own" '^ssh_marker=yes$'
check "every host key pair matches, and Ubuntu's sshd-keygen never runs (ours is the only generator)" \
  '^keys1: pairs=[3-9] mismatched=0 sshd_keygen=no/inactive$'
check "and after the replacement at the second boot too" '^keys2: pairs=[3-9] mismatched=0 sshd_keygen=no/inactive$'
check "the firewall's table is loaded before resolved, networkd, ssh or rpcbind start" '^listen: fw_loaded=yes early=none '
check "no LLMNR or mDNS listener (resolved has them off)" '^listen: .* mdns_llmnr=0 resolved=-LLMNR,-mDNS$'
check "ldconfig and the journal catalog run at every boot; hwdb.bin comes with /usr" \
  '^every_boot: ldconfig=yes/success catalog=yes hwdb_in_usr=yes$'
check "keys it didn't make (no marker: an old image's) are replaced at the next boot, once" \
  '^rekey: before=(SHA256:[^ ]+) now=SHA256:[^ ]+ marker=yes$'
if grep -Eq '^rekey: before=([^ ]+) now=\1 ' <<<"$R"; then echo "FAIL the replaced key is a new one"; fail=1; else echo "ok   the replaced key is a new one"; fi
check "the firewall and the SSH keygen are wanted from /usr (an updated box starts them too)" \
  '^vendor: omnifile-firewall omnifile-ssh-keygen $'
check "the network and every listener require the firewall (no firewall, no network)" \
  '^fw_requires: systemd-networkd.service systemd-resolved.service ssh.socket smbd.service nfs-ganesha-omni.service rpcbind.socket $'
check "and they don't start when it can't load" '^failclosed: firewall=failed table=no networkd=(inactive|failed) ssh=(inactive|failed)$'
check "Ubuntu's nftables.service never runs, so a restart of it leaves the firewall in place" \
  '^nftables: enabled=disabled active=inactive table_at_boot=yes table_after_restart=yes$'
check "a statd user missing from /etc is made at the next boot from /usr (sysusers.d, run at every boot)" \
  '^statd_user: removed=yes back=statd:/var/lib/nfs:/usr/sbin/nologin sysusers=yes$'
if [ $TPM = yes ]; then
  check "its key is sealed to the TPM, with no plain copy on disk" '^ra_sealed=yes tpm=yes plain_key=0$'
else
  check "its key is sealed (host key: no swtpm here), with no plain copy" '^ra_sealed=yes tpm=no plain_key=0$'
fi
check "provisioned, its conditions hold" '^ra_condition=yes'
check "it serves credentials on 127.0.0.1:9911 (IMDSv2: token, role, keys)" \
  '^ra_serves=yes active=active token=yes role=[^ ]+ access_key=ASIASTUBROLESANYWHERE untokened=401'
R2=$(cat "$WORK/ra-stub.log" 2>/dev/null)
printf 'stub: %s\n' "$R2"
if printf '%s\n' "$R2" | grep -q '"path": "/sessions".*"algorithm": "AWS4-X509-ECDSA-SHA256".*"x509_subject": "CN=omnifile-edge-smoke".*"trustAnchorArn": "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b".*"roleArn": "arn:aws:iam::123456789012:role/omnifile-edge-s3-read".*"durationSeconds": 3600'; then
  echo "ok   it signed CreateSession with the sealed key's certificate, for the env file's ARNs"
else
  echo "FAIL it signed CreateSession with the sealed key's certificate, for the env file's ARNs"; fail=1
fi
exit $fail
