#!/usr/bin/env bash
# The appliance as an NFS server for WP3's grace test (tests/e2e/nfs_grace_test.sh --server HOST
# --restart-cmd CMD), across the two restarts only the appliance has: a reboot, and an A/B
# update's reboot into the new /usr.
#
#   appliance/tests/grace.sh --disk 0.1.0.raw --feed 0.2.0/feed --keys DIR [--grace-test SCRIPT]
#
# The guest boots in qemu (tests/vm.sh) with its NFS and SSH ports forwarded to 127.0.0.1 on
# this host (NFS on 2049, so --server 127.0.0.1 needs no port; P_NFS, P_SSH change them).
# A test unit, injected as the other tests' are, makes it serve one export:
#   - Ganesha as nfs-ganesha-omni.service runs it (its ExecStart, capabilities, drop-ins) but
#     without the guard units it binds to, which need an enrolled edge, as a unit in /etc so it
#     comes back by itself after each restart;
#   - the setup wizard's base config, plus one FSAL_VFS export of /srv/grace (Protocols 3 and 4,
#     sec=sys, no root squash). FSAL_OMNI needs an enrolled edge and isn't what grace is about:
#     the clients' state and recovery records are Ganesha's own, in /var/lib/nfs/ganesha;
#   - root's SSH key from a credential (ssh.authorized_keys.root), for the restart commands.
# Two restart commands are written to WORK, each returning once SSH answers again and Ganesha
# is active (in grace):
#   WORK/restart-reboot.sh  systemctl reboot
#   WORK/restart-ab.sh      systemd-sysupdate update (FEED, signed with KEYS/update.pem), then
#                           the reboot into the other slot
# With --grace-test, it runs SCRIPT --server 127.0.0.1 --restart-cmd for each. Without it (WP3's
# test not merged yet) it checks the restart paths themselves: after each, Ganesha is active
# and in grace, its recovery records and the export's data are still there, and the A/B one
# runs the new version.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' FEED='' KEYS='' GT=''
P_NFS=${P_NFS:-2049} P_SSH=${P_SSH:-12222} P_FEED=${P_FEED:-18091}
WORK=${WORK:-$HERE/../build/grace}
while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK=$2; shift ;;
    --feed) FEED=$2; shift ;;
    --keys) KEYS=$2; shift ;;
    --grace-test) GT=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DISK" ] && [ -n "$FEED" ] && [ -n "$KEYS" ] || { sed -n '2,29p' "$0"; exit 2; }
KEYS=$(cd "$KEYS" && pwd)
rm -rf "$WORK" && mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
vm_init "$DISK"

# ---- the feed for the A/B restart ------------------------------------------------------------------
mkdir -p "$WORK/feed"
find "$FEED" -maxdepth 1 -type f -name 'omnifile-edge_*' -exec cp {} "$WORK/feed/" \;
(cd "$WORK/feed" && sha256sum -- omnifile-edge_* >SHA256SUMS)
python3 "$HERE/../pgp_kv.py" sign --signer "fake:$KEYS/update.pem" --created 1792540800 \
  --ring "$KEYS/update-ring.pgp" --in "$WORK/feed/SHA256SUMS" --out "$WORK/feed/SHA256SUMS.gpg" >/dev/null
V2=$(find "$WORK/feed" -name 'omnifile-edge_*.efi' | sed -n 's|.*/omnifile-edge_\(.*\)\.efi$|\1|p')
python3 -m http.server "$P_FEED" --bind 127.0.0.1 --directory "$WORK/feed" >"$WORK/http.log" 2>&1 &
HTTP=$!
ssh-keygen -q -t ed25519 -N '' -C omnifile-grace-test -f "$WORK/id"
SSH=(ssh -i "$WORK/id" -p "$P_SSH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes root@127.0.0.1)

# ---- the guest's setup, once -------------------------------------------------------------------------
cat >"$WORK/guest.sh" <<SH
P_FEED=$P_FEED
SH
cat >>"$WORK/guest.sh" <<'SH'
[ -e /etc/systemd/system/grace-ganesha.service ] && exit 0
mkdir -p /srv/grace /etc/ganesha
echo "written before any restart" >/srv/grace/before.txt
cp /usr/share/omnifile/ganesha-base.conf /etc/ganesha/omnifile-base.conf
cat >/etc/ganesha/ganesha.conf <<'EOF'
%include "/etc/ganesha/omnifile-base.conf"
EXPORT {
	Export_Id = 7;
	Path = /srv/grace;
	Pseudo = /grace;
	Protocols = 3, 4;
	Access_Type = RW;
	Squash = No_Root_Squash;
	SecType = sys;
	FSAL { Name = VFS; }
}
EOF
sed -e '/^Requires=omnifile-edge.service/d' -e '/^BindsTo=omnifile-edge.service/d' \
  -e 's/^After=omnifile-edge.service /After=/' /usr/lib/systemd/system/nfs-ganesha-omni.service \
  >/etc/systemd/system/grace-ganesha.service
mkdir -p /etc/systemd/system/grace-ganesha.service.d
for f in /usr/lib/systemd/system/nfs-ganesha-omni.service.d/*.conf; do
  case ${f##*/} in 10-omnifile-guard.conf) ;; *) cp "$f" /etc/systemd/system/grace-ganesha.service.d/ ;; esac
done
for t in 10-uki 20-usr 21-usr-verity; do
  mkdir -p /etc/sysupdate.d/$t.transfer.d
  printf '[Source]\nPath=http://10.0.2.2:%s/\n' "$P_FEED" >/etc/sysupdate.d/$t.transfer.d/50-test.conf
done
systemctl daemon-reload
systemctl enable --now grace-ganesha.service
# A listener on a port the firewall does not name: the host must not reach it.
systemd-run --unit=grace-probe python3 -m http.server 5555 --directory /srv/grace >/dev/null 2>&1
echo "GRACE-READY $(systemctl is-active grace-ganesha.service)"
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
vm_prepare "$WORK" "$DISK" 16G
mapfile -t EXTRA < <(vm_cred ssh.authorized_keys.root "$(cat "$WORK/id.pub")")
P_PROBE=${P_PROBE:-15555}
export VM_NIC_EXTRA="hostfwd=tcp:127.0.0.1:$P_SSH-:22,hostfwd=tcp:127.0.0.1:$P_NFS-:2049,hostfwd=tcp:127.0.0.1:$P_PROBE-:5555"
echo "booting $DISK; NFS on 127.0.0.1:$P_NFS, SSH on 127.0.0.1:$P_SSH (log: $WORK/serial.log)"
vm_run "$WORK" "${GRACE_TIMEOUT:-7200}" omnifile-grace.service "$WORK/guest.unit" "${EXTRA[@]}" &
QEMU=$!
# vm_run's timeout and qemu are children of the background job: stop them by their disk.
trap 'kill $HTTP 2>/dev/null || true; kill $QEMU 2>/dev/null || true; pkill -f "$WORK/disk.qcow2" 2>/dev/null || true' EXIT

# up STATE_FILE: wait until SSH answers and Ganesha is active, and print what is running.
up() {
  local i
  for i in $(seq 1 120); do
    if "${SSH[@]}" true 2>/dev/null &&
      [ "$("${SSH[@]}" systemctl is-active grace-ganesha.service 2>/dev/null)" = active ]; then
      "${SSH[@]}" '. /usr/lib/os-release; echo "GRACE-UP version=$IMAGE_VERSION boot=$(cat /proc/sys/kernel/random/boot_id | cut -c1-8) ganesha=$(systemctl is-active grace-ganesha) in_grace=$(journalctl -b -u grace-ganesha -o cat | grep -c "IN GRACE") marker=$(cat /var/lib/nfs/ganesha/grace-marker 2>/dev/null || echo none) data=$(cat /srv/grace/before.txt)"'
      return 0
    fi
    sleep 5
  done
  echo "GRACE-DOWN: no SSH or no Ganesha after 10 minutes"; return 1
}
cat >"$WORK/restart-reboot.sh" <<EOF
#!/usr/bin/env bash
# The appliance's restart: a reboot. Returns once Ganesha serves again (in grace).
$(declare -f up)
SSH=($(printf '%q ' "${SSH[@]}"))
"\${SSH[@]}" 'sync; systemctl --no-block reboot' || true
sleep 10
up
EOF
cat >"$WORK/restart-ab.sh" <<EOF
#!/usr/bin/env bash
# The appliance's A/B update: the new version into the other slot, then the reboot into it.
$(declare -f up)
SSH=($(printf '%q ' "${SSH[@]}"))
"\${SSH[@]}" '/usr/lib/systemd/systemd-sysupdate --no-pager update >/var/tmp/grace-sysupdate.log 2>&1; echo "GRACE-SYSUPDATE rc=\$?"; sync; systemctl --no-block reboot' || true
sleep 10
up
EOF
chmod +x "$WORK/restart-reboot.sh" "$WORK/restart-ab.sh"

fail=0
check() { if grep -Eq "$2" <<<"$3"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
sleep 3
if ! kill -0 $QEMU 2>/dev/null; then echo "FAIL qemu did not start: $(cat "$WORK/qemu.out")"; exit 1; fi
U0=$(up) || true; echo "$U0"
# A marker in Ganesha's recovery root, which it never writes itself (it recreates its own
# directories at every start, so counting entries proves nothing): it must outlive the reboot
# and the A/B update, as the clients' records there must.
MARK="grace-$(date +%s)-$$"
"${SSH[@]}" "echo $MARK >/var/lib/nfs/ganesha/grace-marker && sync" 2>/dev/null || true
check "the appliance serves the export, Ganesha in grace at start" '^GRACE-UP version=0\.1\.0 .*ganesha=active in_grace=[1-9]' "$U0"
probe=$("${SSH[@]}" 'systemctl is-active grace-probe; python3 -c "import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:5555/\").status)"' 2>/dev/null | tr '\n' ' ')
from_host=$(curl -s -o /dev/null --max-time 8 -w '%{http_code}' "http://127.0.0.1:$P_PROBE/" || true)
check "the firewall: a listener on port 5555 answers inside ($probe), the host can't reach it ($from_host)" \
  '^active 200 000$' "$probe$from_host"
if [ -n "$GT" ]; then
  echo "== $GT across a reboot"
  "$GT" --server 127.0.0.1 --restart-cmd "$WORK/restart-reboot.sh" && echo "ok   grace test across a reboot" || { echo "FAIL grace test across a reboot"; fail=1; }
  echo "== $GT across an A/B update"
  "$GT" --server 127.0.0.1 --restart-cmd "$WORK/restart-ab.sh" && echo "ok   grace test across an A/B update" || { echo "FAIL grace test across an A/B update"; fail=1; }
else
  echo "no --grace-test: checking the restart paths themselves (WP3's tests/e2e/nfs_grace_test.sh pending)"
  U1=$("$WORK/restart-reboot.sh" 2>&1 | grep GRACE) || true; echo "$U1"
  b0=$(sed -n 's/.* boot=\([^ ]*\).*/\1/p' <<<"$U0"); b1=$(sed -n 's/.* boot=\([^ ]*\).*/\1/p' <<<"$U1")
  check "after the reboot: a new boot, Ganesha active and in grace, the recovery root's marker and the data kept" \
    "^GRACE-UP version=0\\.1\\.0 boot=[0-9a-f]+ ganesha=active in_grace=[1-9][0-9]* marker=$MARK data=written before any restart" "$U1"
  [ -n "$b1" ] && [ "$b0" != "$b1" ] && echo "ok   it really rebooted ($b0 -> $b1)" || { echo "FAIL no reboot seen"; fail=1; }
  U2=$("$WORK/restart-ab.sh" 2>&1 | grep GRACE) || true; echo "$U2"
  check "the A/B update installed $V2" '^GRACE-SYSUPDATE rc=0' "$U2"
  check "after it: $V2 runs, Ganesha active and in grace, the recovery root's marker and the data kept" \
    "^GRACE-UP version=$V2 boot=[0-9a-f]+ ganesha=active in_grace=[1-9][0-9]* marker=$MARK data=written before any restart" "$U2"
fi
"${SSH[@]}" 'systemctl --no-block poweroff' 2>/dev/null || true
wait $QEMU 2>/dev/null || true
exit $fail
