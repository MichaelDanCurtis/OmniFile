#!/usr/bin/env bash
# Provision the permission-matrix VM (runs as root INSIDE omnifile-acl).
#
# Same shape as dev/scripts/edge-provision.sh, but with its own host name
# (acl1 / NetBIOS ACL1) so it never collides with omnifile-edge's machine
# account, plus what the matrix needs on top:
#   - an xfs test volume at /srv/acl (POSIX ACLs, pre-content events);
#   - nfs/acl1.omni.test in AD and in /etc/krb5.keytab (NFS krb5 exports);
#   - idmapd domain, gssd/cifs.upcall for krb5 NFS and CIFS client mounts.
# Idempotent. Prints no secrets (the admin password goes through $PASSWD).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

ENV_FILE="${AD_ENV_FILE:-/work/dev/.secrets/ad.env}"
STATE=/var/lib/omnifile-acl
SHORT=acl1
NB=ACL1

log() { printf '[acl-provision] %s\n' "$*"; }
die() { printf '[acl-provision] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -r "$ENV_FILE" ] || die "$ENV_FILE not readable"
install -d -m 0755 "$STATE"
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${AD_REALM:?}" "${AD_DOMAIN:?}" "${AD_DC_HOST:?}" "${AD_DC_IP:?}" "${AD_ADMIN_PASSWORD:?}"
REALM_LC="${AD_REALM,,}"
FQDN="${SHORT}.${REALM_LC}"

# 1. Packages
if [ ! -f "$STATE/packages-installed" ]; then
  log "installing packages"
  echo "krb5-config krb5-config/default_realm string $AD_REALM" | debconf-set-selections
  echo "krb5-config krb5-config/kerberos_servers string $AD_DC_HOST" | debconf-set-selections
  echo "krb5-config krb5-config/admin_server string $AD_DC_HOST" | debconf-set-selections
  apt-get update -qq
  apt-get install -y -qq -o DPkg::Lock::Timeout=600 \
    build-essential pkg-config git curl ca-certificates jq \
    xfsprogs acl attr procps psmisc strace python3 \
    nfs-common rpcbind cifs-utils keyutils smbclient \
    samba winbind libnss-winbind libpam-winbind krb5-user ldb-tools dnsutils \
    libjemalloc2 liburcu8t64 libnfsidmap1 libdbus-1-3 libblkid1 libcap2 libwbclient0 \
    >/dev/null || apt-get install -y -qq -o DPkg::Lock::Timeout=600 \
    build-essential pkg-config git curl ca-certificates jq \
    xfsprogs acl attr procps psmisc strace python3 \
    nfs-common rpcbind cifs-utils keyutils smbclient \
    samba winbind libnss-winbind libpam-winbind krb5-user ldb-tools dnsutils >/dev/null
  touch "$STATE/packages-installed"
fi
systemctl disable --now smbd nmbd >/dev/null 2>&1 || true

# 2. Test volume (xfs on a sparse loop file)
img=/var/lib/omnifile-acl/data.img mp=/srv/acl
install -d -m 0755 "$mp"
[ -f "$img" ] || { truncate -s 8G "$img"; mkfs.xfs -q -f -L omni-acl "$img"; }
grep -q "^$img " /etc/fstab || echo "$img $mp xfs loop,nofail,noatime 0 0" >>/etc/fstab
systemctl daemon-reload
mountpoint -q "$mp" || mount "$mp"

# 3. Host name + DNS (realm only, through the DC)
cat >/etc/cloud/cloud.cfg.d/99-omnifile-acl.cfg <<'EOF'
preserve_hostname: true
manage_etc_hosts: false
EOF
hostnamectl set-hostname "$SHORT"
sed -i '/# omnifile-acl$/d' /etc/hosts
printf '127.0.1.1 %s %s # omnifile-acl\n' "$FQDN" "$SHORT" >>/etc/hosts
printf '%s %s # omnifile-acl\n' "$AD_DC_IP" "$AD_DC_HOST" >>/etc/hosts
want="[Resolve]
DNS=$AD_DC_IP
Domains=~$REALM_LC"
install -d /etc/systemd/resolved.conf.d
if [ "$(cat /etc/systemd/resolved.conf.d/omnifile-acl.conf 2>/dev/null || true)" != "$want" ]; then
  printf '%s\n' "$want" >/etc/systemd/resolved.conf.d/omnifile-acl.conf
  systemctl restart systemd-resolved
fi
resolvectl flush-caches || true
for i in $(seq 1 30); do
  host -t SRV "_ldap._tcp.${REALM_LC}" >/dev/null 2>&1 && break
  sleep 2; [ "$i" -lt 30 ] || die "realm does not resolve through the DC"
done

# 4. Kerberos + Samba member config. The global section is the product
# template (edge/config/samba/smb.conf.tmpl), rendered by the matrix with the
# shares appended; here only the globals, so winbind can start.
cat >/etc/krb5.conf <<EOF
[libdefaults]
    default_realm = $AD_REALM
    dns_lookup_realm = false
    dns_lookup_kdc = true
    rdns = false
    forwardable = true
    ticket_lifetime = 24h
[realms]
    $AD_REALM = {
        kdc = $AD_DC_HOST
        admin_server = $AD_DC_HOST
        default_domain = $REALM_LC
    }
[domain_realm]
    .$REALM_LC = $AD_REALM
    $REALM_LC = $AD_REALM
EOF
sed -e "s/@WORKGROUP@/$AD_DOMAIN/g" -e "s/@REALM@/$AD_REALM/g" -e "s/@NETBIOS_NAME@/$NB/g" \
    -e "s/@IDMAP_RANGE@/10000-999999/g" -e "s/@AUDIT_FACILITY@/local5/g" \
    /work/edge/config/samba/smb.conf.tmpl >/etc/samba/smb.conf
[ -f /etc/samba/omnifile-shares.conf ] || : >/etc/samba/omnifile-shares.conf
testparm -s >/dev/null 2>&1 || die "smb.conf does not parse"

# 5. Join, NSS, winbind
if ! net ads testjoin >/dev/null 2>&1; then
  log "joining $AD_REALM as $NB"
  systemctl stop winbind >/dev/null 2>&1 || true
  rm -f /var/lib/samba/private/secrets.tdb /var/cache/samba/gencache.tdb /var/lib/samba/gencache.tdb
  PASSWD="$AD_ADMIN_PASSWORD" net ads join -U "${AD_ADMIN_USER:-Administrator}" >"$STATE/join.log" 2>&1 \
    || { tail -n 20 "$STATE/join.log" >&2; die "domain join failed"; }
  net ads dns register -P >/dev/null 2>&1 || true
fi
for db in passwd group; do
  grep -Eq "^${db}:.*\bwinbind\b" /etc/nsswitch.conf \
    || sed -i -E "s/^(${db}:[[:space:]]+.*)\$/\1 winbind/" /etc/nsswitch.conf
done
systemctl enable winbind >/dev/null 2>&1
# A fresh join can leave negative id-map entries cached from lookups made
# before the machine account worked (seen: geo-nested "no such user").
net cache flush >/dev/null 2>&1 || true
systemctl restart winbind
for i in $(seq 1 30); do wbinfo -t >/dev/null 2>&1 && break; sleep 2; [ "$i" -lt 30 ] || die "winbind trust check failed"; done

# 6. NFS service principal (nfs/acl1.omni.test) in AD and in the keytab.
if ! klist -k /etc/krb5.keytab 2>/dev/null | grep -q "nfs/$FQDN@"; then
  log "adding nfs/$FQDN to AD and /etc/krb5.keytab"
  PASSWD="$AD_ADMIN_PASSWORD" net ads setspn add "nfs/$FQDN" -U "${AD_ADMIN_USER:-Administrator}" >"$STATE/spn.log" 2>&1 || true
  PASSWD="$AD_ADMIN_PASSWORD" net ads setspn add "nfs/$SHORT" -U "${AD_ADMIN_USER:-Administrator}" >>"$STATE/spn.log" 2>&1 || true
  net ads keytab create -P >"$STATE/keytab.log" 2>&1 || true
  klist -k /etc/krb5.keytab 2>/dev/null | grep -q "nfs/$FQDN@" \
    || log "WARNING: nfs/$FQDN not in keytab (see $STATE/keytab.log); NFS krb5 cells will SKIP"
fi
sed -i -E 's/^#? *Domain *=.*/Domain = '"$REALM_LC"'/' /etc/idmapd.conf 2>/dev/null || true
grep -q "^Domain = $REALM_LC" /etc/idmapd.conf 2>/dev/null || printf '[General]\nDomain = %s\n' "$REALM_LC" >>/etc/idmapd.conf
systemctl enable --now rpc-gssd >/dev/null 2>&1 || true

getent passwd geo-reader >/dev/null || die "geo-reader not visible through winbind"
[ "$(id -u geo-reader)" = 20001 ] || die "geo-reader uid mismatch"
log "ready: $FQDN joined; keytab: $(klist -k /etc/krb5.keytab 2>/dev/null | grep -c nfs/ || true) nfs entries"
