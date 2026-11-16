#!/usr/bin/env bash
# Unattended setup on the real image (docs/design/setup-wizard.md): the answers
# arrive as the system credential omnifile.setup, with an empty second disk.
#
# No domain controller is reachable from the VM, so the run stops at the domain
# join, and must say why. Before that, the first steps run for real:
#   hostname, network (DHCP here), time, and the data disk (xfs, mounted by
#   UUID, labelled omni-data).
# Then the test runs the edge and updates steps by hand. They write edge.toml
# with the volume's real UUID and private networking on, and the update feed
# and restart window. A rerun of the first steps changes nothing.
#
# Roles Anywhere (decision 43): the aws step makes the key, sealed with systemd-creds (to
# the vTPM), and its certificate request; a test trust anchor (openssl, in the guest's RAM)
# signs it; `omnifile-setup aws-cert` checks and installs the certificate, and the helper
# then serves credentials on 127.0.0.1:9911 from a stand-in for the Roles Anywhere
# endpoint on the host (tests/ra-stub.py; offline, no AWS). A certificate for another key
# is refused.
#
# The edge's keys are sealed with systemd credentials (key_protection = "auto").
# With swtpm installed the VM gets a TPM 2.0, and the daemon, inside its own
# unit's sandbox, must seal to it (`tpm2`); without one, to the host key.
#
#   appliance/tests/setup.sh [DISK]    (default appliance/build/out/omnifile-edge_*.raw)
#
# With --dc ENVFILE, the same unattended setup runs to the end against a real AD domain and a
# running service instead (`setup_dc` below): the wizard joins the domain, enrols, and starts
# the edge, smbd and Ganesha. ENVFILE sets REALM, WORKGROUP, DC_IP (its DNS), JOIN_USER,
# JOIN_PASSWORD_FILE, ADMIN_GROUP, ADMIN_USER (a member, with its uid as UID_ADMIN),
# ENROL_BUNDLE_FILE (from `python -m omnifile_service.enrol add-edge`), and SERVICE_PORTS: the
# service's ports on this host (enrol, api), which the guest reaches as 127.0.0.1:PORT through
# systemd-socket-proxyd to 10.0.2.2:PORT, so the URLs and certificates of a local dev stack
# (dev/m4/stack.py, 127.0.0.1) work unchanged. API_LOG, if set, is the api's request log
# (the stack's logs/api.log), checked for this edge's answered heartbeats and feed polls.
# The guest leaves the domain at the end.
#   appliance/tests/setup.sh --dc ENVFILE [DISK]
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DC_ENV=''
if [ "${1:-}" = --dc ]; then DC_ENV=$2; shift 2; fi
DISKS=("$HERE"/../build/out/omnifile-edge_*.raw)
DISK=${1:-${DISKS[0]}}
WORK=${WORK:-$HERE/../build/setup}

setup_dc() {
  # shellcheck disable=SC1090
  . "$DC_ENV"
  local realm_lc=${REALM,,} w=$WORK/dc
  mkdir -p "$w"
  local answers="version = 1
hostname = \"edge-wp2\"
[network]
mode = \"dhcp\"
dns = [\"$DC_IP\"]
[data]
device = \"/dev/vdb\"
[ad]
domain = \"$realm_lc\"
workgroup = \"$WORKGROUP\"
join_user = \"$JOIN_USER\"
admin_group = \"$ADMIN_GROUP\"
[updates]
feed = \"https://updates.invalid/omnifile-edge/\"
reboot_window = \"Sat *-*-* 02:00\"
"
  local g=$w/guest.sh p units=() wants=''
  cat >"$g" <<SH
JOIN_USER='$JOIN_USER' REALM='$REALM' ADMIN_USER='$ADMIN_USER' ADMIN_GROUP='$ADMIN_GROUP'
SH
  cat >>"$g" <<'SH'
[ -n "$BASH_VERSION" ] || exec /bin/bash "$0" "$@"
echo DCSETUP-BEGIN
echo "auto=$(systemctl show -p Result --value omnifile-setup-auto.service) status=$(systemctl show -p ExecMainStatus --value omnifile-setup-auto.service) done=$(test -e /var/lib/omnifile/setup/done && echo yes || echo no)"
journalctl -b -u omnifile-setup-auto -o cat | grep -v '^$' | sed 's/^/  auto: /'
echo "testjoin=$(net ads testjoin 2>&1 | tr '\n' ' ')"
echo "trust=$(wbinfo -t 2>&1 | tail -1)"
echo "admin=$(id "$ADMIN_USER" 2>&1)"
echo "admin_group_sudo=$(grep -c "$ADMIN_GROUP" /etc/sudoers.d/omnifile-admins 2>/dev/null)"
echo "services: edge=$(systemctl is-active omnifile-edge.service) smbd=$(systemctl is-active smbd.service) winbind=$(systemctl is-active winbind.service) ganesha=$(systemctl is-active nfs-ganesha-omni.service)"
echo "identity=$(ls /var/lib/omnifile/identity | tr '\n' ' ')"
# Give the daemon two heartbeats' time with the service before reading its log.
sleep 120
echo "edge_log: $(journalctl -b -u omnifile-edge -o cat | grep -iE 'enrol|heartbeat|feed|snapshot|error' | tail -4 | tr '\n' '|' | cut -c1-500)"
echo "bundle_spent=$(grep -c '^bundle\|enrol' /etc/omnifile/setup.toml 2>/dev/null)"
# Leave the domain again: the test machine's computer account is removed from the DC.
PASSWD=$(cat /run/credentials/@system/omnifile.join-password) net ads leave -U "$JOIN_USER" >/run/leave.out 2>&1
echo "leave=$? $(tail -1 /run/leave.out)"
echo DCSETUP-END
systemctl --no-block poweroff
SH
  vm_unit "$g" >"$g.unit"
  # 127.0.0.1:PORT in the guest -> 10.0.2.2:PORT (this host), for each service port.
  for p in $SERVICE_PORTS; do
    printf '[Socket]\nListenStream=127.0.0.1:%s\n' "$p" >"$w/fwd-$p.socket"
    printf '[Unit]\nDescription=test: 127.0.0.1:%s to the host\n[Service]\nExecStart=/usr/lib/systemd/systemd-socket-proxyd 10.0.2.2:%s\n' "$p" "$p" >"$w/fwd-$p.service"
    units+=(-smbios "type=11,value=io.systemd.credential.binary:systemd.extra-unit.wp2-fwd-$p.socket=$(base64 -w0 "$w/fwd-$p.socket")"
      -smbios "type=11,value=io.systemd.credential.binary:systemd.extra-unit.wp2-fwd-$p.service=$(base64 -w0 "$w/fwd-$p.service")")
    wants+=" wp2-fwd-$p.socket"
  done
  units+=(-smbios "type=11,value=io.systemd.credential.binary:systemd.unit-dropin.sockets.target=$(printf '[Unit]\nWants=%s\n' "$wants" | base64 -w0)")
  vm_prepare "$w" "$DISK" 16G
  rm -f "$w/data.qcow2"
  qemu-img create -q -f qcow2 "$w/data.qcow2" 4G
  local extra
  mapfile -t extra < <(vm_cred omnifile.setup "$answers"; vm_cred omnifile.join-password "$(cat "$JOIN_PASSWORD_FILE")";
    vm_cred omnifile.enrol-bundle "$(cat "$ENROL_BUNDLE_FILE")"; vm_tpm "$w")
  echo "booting $DISK to join $REALM ($DC_IP) and enrol (service ports: $SERVICE_PORTS; log: $w/serial.log)"
  vm_run "$w" 1800 omnifile-dcsetup.service "$g.unit" -drive "if=virtio,format=qcow2,file=$w/data.qcow2" \
    "${units[@]}" "${extra[@]}" || true
  R=$(sed -n '/DCSETUP-BEGIN/,/DCSETUP-END/p' "$w/serial.log" | tr -d '\r')
  printf '%s\n' "$R"
  fail=0
  check() { if grep -Eq "$2" <<<"${3:-$R}"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
  check "unattended setup ran every step, from the credentials" '^auto=success status=0 done=yes'
  check "it joined $REALM" "auto: +ad +done: .*joined the domain"
  check "the join holds (net ads testjoin)" '^testjoin=Join is OK'
  check "winbind's trust secret checks out" '^trust=.*succeeded'
  check "the domain's admin, through winbind, with the domain's uid ($ADMIN_USER = $UID_ADMIN)" "^admin=uid=$UID_ADMIN"
  check "the admin group may sudo" '^admin_group_sudo=[1-9]'
  check "it enrolled with the service" 'auto: +enrol +done'
  check "and the edge, smbd, winbind and Ganesha run" '^services: edge=active smbd=active winbind=active ganesha=active'
  check "the edge holds its identity: the sealed key, its certificate record, the pinned CAs" \
    '^identity=client-ca.pem identity.cred identity.json server-ca.pem'
  check "the test machine left the domain at the end" '^leave=0 '
  # API_LOG (optional): the service's request log, which must show this edge's heartbeats and
  # feed polls answered, i.e. the enrolled identity works against the api over mTLS.
  if [ -n "${API_LOG:-}" ]; then
    local id
    id=$(sed -n 's/.*enrolled: edge_id=\([0-9]*\).*/\1/p' <<<"$R" | head -1)
    local hb fd
    hb=$(grep -c "POST /v0/edges/$id/heartbeat HTTP/1.1\" 200" "$API_LOG" || true)
    fd=$(grep -c "GET /v0/edges/$id/feed?.* 200" "$API_LOG" || true)
    echo "api log: edge $id: $hb heartbeats and $fd feed polls answered 200"
    check "the service answered this edge's heartbeats and feed polls (mTLS)" '^[1-9][0-9]* [1-9]' "$hb $fd"
  fi
  return $fail
}
if [ -n "$DC_ENV" ]; then setup_dc; exit $?; fi

ANSWERS='version = 1
hostname = "edge-test"
[network]
mode = "dhcp"
[data]
device = "/dev/vdb"
[ad]
domain = "corp.example"
workgroup = "CORP"
join_user = "svc-join"
admin_group = "filer admins"
[updates]
feed = "https://updates.invalid/omnifile-edge/"
reboot_window = "Sat *-*-* 02:00"
[aws]
trust_anchor_arn = "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b"
profile_arn = "arn:aws:rolesanywhere:eu-west-1:123456789012:profile/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
role_arn = "arn:aws:iam::123456789012:role/omnifile-edge-s3-read"
session_seconds = 3600
issuer = "client"
'
RA_PORT=${RA_PORT:-8444}
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
echo SETUP-BEGIN
echo "hostname=$(cat /etc/hostname) kernel=$(uname -n)"
echo "auto=$(systemctl show -p Result --value omnifile-setup-auto.service) status=$(systemctl show -p ExecMainStatus --value omnifile-setup-auto.service)"
journalctl -b -u omnifile-setup-auto -o cat | grep -v '^$' | sed 's/^/  auto: /'
echo "console=$(systemctl is-active omnifile-setup-console.service)"
echo "data=$(findmnt -n -o FSTYPE,SOURCE /srv/omnifile/data) label=$(blkid -s LABEL -o value /dev/vdb)"
U=$(blkid -s UUID -o value /dev/vdb)
echo "mount_unit=$(systemctl is-enabled srv-omnifile-data.mount) by_uuid=$(grep -c "What=/dev/disk/by-uuid/$U" /etc/systemd/system/srv-omnifile-data.mount)"
echo "network=$(grep -c '^DHCP=yes' /etc/systemd/network/10-omnifile.network) domains=$(grep '^Domains=' /etc/systemd/network/10-omnifile.network)"
echo "ntp=$(grep '^NTP=' /etc/systemd/timesyncd.conf.d/10-omnifile.conf)"
echo "answers=$(stat -c %a /etc/omnifile/setup.toml) done=$(test -e /var/lib/omnifile/setup/done && echo yes || echo no)"
echo "smb=$(testparm -s --suppress-prompt /etc/samba/smb.conf 2>/dev/null | grep -c 'realm = CORP.EXAMPLE')"
/usr/local/bin/omnifile-setup apply --only recovery,edge,aws,updates 2>&1 | sed 's/^/  apply: /'
echo "recovery_pam=$(grep -c 'omnifile-setup recovery-used' /etc/pam.d/common-session) sudoers=$(stat -c %a /etc/sudoers.d/omnifile-recovery)"
echo "edge_uuid=$(grep -c "fs_uuid = \"$U\"" /etc/omnifile/edge.toml) private=$(grep '^private_only' /etc/omnifile/edge.toml)"
# The fill path is the guard unit's bind mount (BindPaths=), which exists only
# inside that unit: check the rest (the config, the xfs volume, trusted.* xattrs).
sed '/^fill_root/d' /etc/omnifile/edge.toml >/run/edge-check.toml
out=$(/usr/local/bin/omnifile-edge --config /run/edge-check.toml --check 2>&1); rc=$?
echo "edge_config=$(printf '%s' "$out" | tail -1 | cut -c1-160); rc=$rc"
echo "cred_key=$(test -s /var/lib/systemd/credential.secret && echo yes || echo no) $(grep '^key_protection' /etc/omnifile/edge.toml)"
systemd-creds has-tpm2 >/dev/null 2>&1 && tpm=yes || tpm=no
mode=host; [ $tpm = yes ] && mode=tpm2
# The daemon's own unit and sandbox (ProtectSystem=strict, its capabilities and
# syscall filter), as a oneshot that runs --check, which seals and opens a probe.
sed "s/^key_protection = .*/key_protection = \"$mode\"/" /run/edge-check.toml >/run/edge-seal.toml
sed -e '/^ExecStartPre=/d' -e '/^Restart/d' -e 's/^Type=.*/Type=oneshot/' \
  -e 's|^ExecStart=.*|ExecStart=/usr/local/bin/omnifile-edge --config /run/edge-seal.toml --check|' \
  /usr/lib/systemd/system/omnifile-edge.service >/run/systemd/system/omnifile-sealcheck.service
systemctl daemon-reload
systemctl start omnifile-sealcheck.service; rc=$?
echo "sealcheck=$rc tpm=$tpm mode=$mode"
journalctl -b -u omnifile-sealcheck -o cat | grep -E 'key|startup' | tail -2 | cut -c1-200 | sed 's/^/  sealcheck: /'
echo "feed=$(cat /etc/sysupdate.d/20-usr.transfer.d/10-omnifile-feed.conf | grep '^Path=')"
echo "window=$(systemctl show -p TimersCalendar --value systemd-sysupdate-reboot.timer)"
echo "ganesha=$(grep -c 'Graceless = false' /etc/ganesha/omnifile-base.conf)"
# The shares the daemon renders (docs/design/share-provisioning.md): included
# by both servers, there (empty) before the daemon's first pass.
echo "shares_render=$(grep -c '^render = true' /etc/omnifile/edge.toml) smb_inc=$(grep -c 'include = /var/lib/omnifile/shares/smb.conf' /etc/samba/omnifile-shares.conf) gan_inc=$(grep -c 'omnifile-exports.conf' /etc/ganesha/ganesha.conf)$(grep -c '/var/lib/omnifile/shares/ganesha.conf' /etc/ganesha/omnifile-exports.conf) files=$(ls /var/lib/omnifile/shares | tr '\n' ,)"
echo "smb_parses=$(testparm -s --suppress-prompt /etc/samba/smb.conf >/dev/null 2>&1 && echo yes || echo no)"
# The daemon reloads the file servers through systemctl, from inside its unit's
# sandbox: its own unit as a oneshot running the [shares] reload command, with
# timesyncd (active, no ExecReload: restarted) standing in for the servers.
t0=$(systemctl show -p ActiveEnterTimestampMonotonic --value systemd-timesyncd.service)
sed -e '/^ExecStartPre=/d' -e '/^Restart/d' -e 's/^Type=.*/Type=oneshot/' \
  -e 's|^ExecStart=.*|ExecStart=/usr/bin/systemctl try-reload-or-restart smbd.service nfs-ganesha-omni.service systemd-timesyncd.service|' \
  /usr/lib/systemd/system/omnifile-edge.service >/run/systemd/system/omnifile-reloadcheck.service
systemctl daemon-reload
systemctl start omnifile-reloadcheck.service; rc=$?
t1=$(systemctl show -p ActiveEnterTimestampMonotonic --value systemd-timesyncd.service)
echo "reloadcheck=$rc restarted=$([ "$t1" != "$t0" ] && echo yes || echo no) smbd=$(systemctl is-active smbd.service) ganesha=$(systemctl is-active nfs-ganesha-omni.service)"
# Roles Anywhere: the key the aws step sealed, its request, the helper's settings.
csr=/etc/omnifile/roles-anywhere.csr
echo "ra_key=$(stat -c %a /var/lib/omnifile/identity/roles-anywhere.cred 2>&1) plain_key=$(grep -rlE 'BEGIN (EC |ENCRYPTED )?PRIVATE KEY' /etc /var/lib/omnifile /root --exclude-dir=ssh 2>/dev/null | wc -l) csr=$(openssl req -in $csr -noout -subject -nameopt RFC2253 2>&1 | sed 's/^subject=//') csr_ok=$(openssl req -in $csr -noout -verify 2>&1 | tr '\n' ' ')"
echo "ra_env=$(grep -c -E '^OMNIFILE_RA_(TRUST_ANCHOR|PROFILE|ROLE)_ARN=arn:' /etc/omnifile/aws-signing-helper.env) region=$(grep '^OMNIFILE_RA_REGION=' /etc/omnifile/aws-signing-helper.env) idle=$(systemctl show -p ConditionResult --value omnifile-aws-signing-helper.service)"
# The operator's CA (a test one here, in RAM) signs the request.
T=/run/ra-test; mkdir -p -m 0700 $T
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout $T/ca.key \
  -out $T/ca.pem -days 2 -subj "/CN=OmniFile test trust anchor" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign 2>/dev/null
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n' >$T/ext
openssl x509 -req -in $csr -CA $T/ca.pem -CAkey $T/ca.key -CAcreateserial -days 1 \
  -extfile $T/ext -out $T/edge.pem 2>/dev/null
cat $T/ca.pem >>$T/edge.pem
# A certificate for some other key is refused, and nothing is installed.
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout $T/other.key \
  -out $T/other.csr -subj "/CN=omnifile-edge-edge-test" 2>/dev/null
openssl x509 -req -in $T/other.csr -CA $T/ca.pem -CAkey $T/ca.key -CAcreateserial -days 1 \
  -extfile $T/ext -out $T/other.pem 2>/dev/null
/usr/local/bin/omnifile-setup aws-cert $T/other.pem 2>&1 | sed 's/^/  other: /'
echo "other_installed=$(test -e /etc/omnifile/roles-anywhere.pem && echo yes || echo no)"
# For this test only: the helper's endpoint is the stand-in on the host (the answers take
# https:// only; a drop-in, which the env file does not override, as it sets none).
mkdir -p /run/systemd/system/omnifile-aws-signing-helper.service.d
printf '[Service]\nEnvironment=OMNIFILE_RA_ENDPOINT=http://10.0.2.2:%s\n' "$RA_PORT" \
  >/run/systemd/system/omnifile-aws-signing-helper.service.d/50-test-endpoint.conf
systemctl daemon-reload
/usr/local/bin/omnifile-setup aws-cert $T/edge.pem 2>&1 | sed 's/^/  cert: /'
rm -f $T/ca.key $T/other.key
echo "ra_cert=$(stat -c %a /etc/omnifile/roles-anywhere.pem) chain=$(grep -c 'BEGIN CERTIFICATE' /etc/omnifile/roles-anywhere-chain.pem) intermediates=$(grep -c '^OMNIFILE_RA_INTERMEDIATES=' /etc/omnifile/aws-signing-helper.env) saved_path=$(grep -c '^certificate' /etc/omnifile/setup.toml)"
ra_serves
/usr/local/bin/omnifile-setup apply --only aws 2>&1 | sed 's/^/  ra-rerun: /'
/usr/local/bin/omnifile-setup apply --only hostname,network,time,data 2>&1 | sed 's/^/  rerun: /'
echo SETUP-END
systemctl --no-block poweroff
SH
vm_unit "$GUEST" >"$GUEST.unit"
vm_prepare "$WORK" "$DISK" 16G
rm -f "$WORK/data.qcow2"
qemu-img create -q -f qcow2 "$WORK/data.qcow2" 4G
mapfile -t EXTRA < <(vm_cred omnifile.setup "$ANSWERS"; vm_tpm "$WORK")
TPM=no
if printf '%s\n' "${EXTRA[@]}" | grep -q tpm-tis; then TPM=yes; fi
echo "booting $DISK with an empty data disk, TPM: $TPM (log: $WORK/serial.log)"
vm_run "$WORK" 1500 omnifile-setup-test.service "$GUEST.unit" \
  -drive "if=virtio,format=qcow2,file=$WORK/data.qcow2" "${EXTRA[@]}" || true
R=$(sed -n '/SETUP-BEGIN/,/SETUP-END/p' "$WORK/serial.log" | sed 's/.*SETUP-BEGIN/SETUP-BEGIN/' | tr -d '\r')
printf '%s\n' "$R"

fail=0
check() { # check NAME REGEX
  if grep -Eq "$2" <<<"$R"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
check "reports" '^SETUP-END'
check "hostname set" '^hostname=edge-test kernel=edge-test'
check "the run stops at the domain join, saying why" 'auto: .*stopped at ad: joining CORP.EXAMPLE needs the password of svc-join'
check "and the unit fails" '^auto=exit-code status=1'
check "steps before it ran" 'auto: +data +done: formatted, mount unit \(/dev/vdb, xfs'
check "no console questions when unattended" '^console=inactive'
check "data disk is xfs, labelled, mounted" '^data=xfs /dev/vdb label=omni-data'
check "mounted by UUID, enabled" '^mount_unit=enabled by_uuid=1'
check "network written" '^network=1 domains=Domains=corp.example'
check "time from the domain" '^ntp=NTP=corp.example'
check "answers saved 0600, not done" '^answers=600 done=no'
check "smb.conf passes testparm" '^smb=1'
check "edge.toml has the volume's UUID and private networking" '^edge_uuid=1 private=private_only = true'
check "the daemon accepts its config and the volume" '^edge_config=.*; rc=0$'
check "unattended, the break-glass account stays locked" 'apply: +recovery +skipped: omnifile-recovery stays locked'
check "its sessions are recorded, and it may sudo" '^recovery_pam=1 sudoers=440'
check "the host key for sealed credentials is made, and keys are sealed" '^cred_key=yes key_protection = "auto"'
check "apply says so" 'apply: +edge +done: .*the host key for sealed credentials'
if [ $TPM = yes ]; then
  check "the daemon seals its keys to the TPM, inside its sandbox" '^sealcheck=0 tpm=yes mode=tpm2'
else
  check "the daemon seals its keys, inside its sandbox" '^sealcheck=0 tpm=no mode=host'
fi
check "update feed" '^feed=Path=https://updates.invalid/omnifile-edge/'
check "restart window" '^window=.*OnCalendar=Sat \*-\*-\* 02:00'
check "ganesha keeps its grace period" '^ganesha=1'
check "both servers include the shares the daemon renders" '^shares_render=1 smb_inc=1 gan_inc=11 files=ganesha.conf,smb.conf,'
check "smb.conf with the share include parses" '^smb_parses=yes'
check "the daemon's sandbox may reload units, and leaves stopped ones stopped" '^reloadcheck=0 restarted=yes smbd=inactive ganesha=inactive'
check "the aws step seals a new key and asks for its certificate" 'apply: +aws +done: a new key \(omnifile-edge-edge-test\), sealed to the TPM, and its certificate request /etc/omnifile/roles-anywhere.csr.*waiting for its certificate'
check "the key is sealed (0600), no plain copy anywhere, the request names the filer and verifies" '^ra_key=600 plain_key=0 csr=CN=omnifile-edge-edge-test csr_ok=.*(OK|verify OK)'
check "the helper's settings are written, and it waits for its certificate" '^ra_env=3 region=OMNIFILE_RA_REGION=eu-west-1 idle=no'
check "a certificate for another key is refused" 'other: .*refused: it is for another key'
check "and nothing installed" '^other_installed=no'
check "the CA's certificate is installed with its chain, the path not kept" '^ra_cert=644 chain=1 intermediates=1 saved_path=0'
check "aws-cert says what it installed" 'cert: +aws +done: the certificate for CN=omnifile-edge-edge-test'
check "the helper serves credentials on 127.0.0.1:9911" \
  '^ra_serves=yes active=active token=yes role=[^ ]+ access_key=ASIASTUBROLESANYWHERE untokened=401'
check "a rerun of the aws step changes nothing" 'ra-rerun: +aws +already as asked'
check "a rerun changes nothing" 'rerun: +data +already as asked'
if printf '%s\n' "$R" | grep -q 'rerun: .*done:'; then echo "FAIL a rerun changed something"; fail=1; fi
R2=$(cat "$WORK/ra-stub.log" 2>/dev/null)
printf 'stub: %s\n' "$R2"
if printf '%s\n' "$R2" | grep -q '"algorithm": "AWS4-X509-ECDSA-SHA256".*"x509_subject": "CN=omnifile-edge-edge-test".*"roleArn": "arn:aws:iam::123456789012:role/omnifile-edge-s3-read".*"durationSeconds": 3600'; then
  echo "ok   the helper signed CreateSession with the wizard's key and certificate"
else
  echo "FAIL the helper signed CreateSession with the wizard's key and certificate"; fail=1
fi
exit $fail
