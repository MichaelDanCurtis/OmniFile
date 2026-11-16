#!/usr/bin/env bash
# SMB and Kerberos NFS on the edge VM (M2), from the product templates.
# Runs as root INSIDE omnifile-edge, after dev/up.sh joined it to the dev AD:
#
#   limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-samba.sh
#
# - /etc/samba/smb.conf from edge/config/samba/smb.conf.tmpl (the appliance's
#   globals: winbind + idmap_ad, POSIX ACLs only, access-based enumeration,
#   full_audit). Shares go in /etc/samba/omnifile-shares.conf, rendered per
#   share by omniacl (tests/e2e/m2_exit_test.sh does it from the catalog).
# - nfs/edge1.omni.test in AD and in /etc/krb5.keytab, idmapd domain, rpc-gssd:
#   what a Kerberos NFS export and client mount need.
# - smbd is enabled but not started here: its drop-in (edge/systemd/
#   smbd.service.d/10-omnifile-guard.conf) binds it to omnifile-edge, so it
#   only ever runs with an armed smb-guard.
# Idempotent. Prints no secrets.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

ENV_FILE="${AD_ENV_FILE:-/work/dev/.secrets/ad.env}"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE=/var/lib/omnifile-dev
SHORT=edge1
NB=EDGE1

log() { printf '[install-samba] %s\n' "$*"; }
die() { printf '[install-samba] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -r "$ENV_FILE" ] || die "$ENV_FILE not readable (run dev/up.sh first)"
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${AD_REALM:?}" "${AD_DOMAIN:?}" "${AD_ADMIN_PASSWORD:?}"
REALM_LC="${AD_REALM,,}"
FQDN="${SHORT}.${REALM_LC}"
install -d -m 0755 "$STATE"

if [ ! -f "$STATE/samba-client-installed" ]; then
  log "installing SMB/NFS client tools"
  apt-get install -y -qq -o DPkg::Lock::Timeout=600 \
    smbclient cifs-utils keyutils nfs-common acl attr >/dev/null
  touch "$STATE/samba-client-installed"
fi

log "smb.conf from the product template"
sed -e "s/@WORKGROUP@/$AD_DOMAIN/g" -e "s/@REALM@/$AD_REALM/g" -e "s/@NETBIOS_NAME@/$NB/g" \
    -e "s/@IDMAP_RANGE@/10000-999999/g" -e "s/@AUDIT_FACILITY@/local5/g" \
    "$REPO/edge/config/samba/smb.conf.tmpl" >/etc/samba/smb.conf.new
[ -f /etc/samba/omnifile-shares.conf ] || : >/etc/samba/omnifile-shares.conf
mv /etc/samba/smb.conf.new /etc/samba/smb.conf
testparm -s >/dev/null 2>&1 || die "smb.conf does not parse"
if testparm -s 2>/dev/null | grep -qi 'acl_xattr\|acl_tdb'; then die "vfs_acl_xattr/acl_tdb configured"; fi
net ads testjoin >/dev/null 2>&1 || die "not joined (run dev/up.sh)"
systemctl restart winbind
for i in $(seq 1 30); do wbinfo -t >/dev/null 2>&1 && break; sleep 2; [ "$i" -lt 30 ] || die "winbind trust check failed"; done

if ! klist -k /etc/krb5.keytab 2>/dev/null | grep -q "nfs/$FQDN@"; then
  log "adding nfs/$FQDN to AD and /etc/krb5.keytab"
  PASSWD="$AD_ADMIN_PASSWORD" net ads setspn add "nfs/$FQDN" -U "${AD_ADMIN_USER:-Administrator}" >"$STATE/spn.log" 2>&1 || true
  PASSWD="$AD_ADMIN_PASSWORD" net ads setspn add "nfs/$SHORT" -U "${AD_ADMIN_USER:-Administrator}" >>"$STATE/spn.log" 2>&1 || true
  net ads keytab create -P >"$STATE/keytab.log" 2>&1 || true
  klist -k /etc/krb5.keytab 2>/dev/null | grep -q "nfs/$FQDN@" || die "nfs/$FQDN not in the keytab (see $STATE/keytab.log)"
fi
sed -i -E 's/^#? *Domain *=.*/Domain = '"$REALM_LC"'/' /etc/idmapd.conf 2>/dev/null || true
grep -q "^Domain = $REALM_LC" /etc/idmapd.conf 2>/dev/null || printf '[General]\nDomain = %s\n' "$REALM_LC" >>/etc/idmapd.conf
systemctl enable --now rpc-gssd >/dev/null 2>&1 || true
systemctl stop nmbd >/dev/null 2>&1 || true
systemctl disable nmbd >/dev/null 2>&1 || true
systemctl enable smbd >/dev/null 2>&1 || true
systemctl daemon-reload
log "ready: smb.conf from template, keytab has $(klist -k /etc/krb5.keytab | grep -c "nfs/") nfs entries; smbd bound to omnifile-edge ($(systemctl show -p BindsTo --value smbd))"
