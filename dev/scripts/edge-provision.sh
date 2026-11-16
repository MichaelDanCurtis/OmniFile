#!/usr/bin/env bash
# Provision the edge dev VM (runs as root INSIDE omnifile-edge).
#
# Idempotent. Reads the AD connection details from the git-ignored
# /work/dev/.secrets/ad.env (written by dev/up.sh from the DC VM).
#
#   1. packages (Rust/C build deps, Samba + winbind, Kerberos, NFS/SMB clients)
#   2. xfs volumes on loop files, mounted under /srv/omnifile
#   3. DNS: the realm resolves through the DC (systemd-networkd per-link route)
#   4. Kerberos + Samba member-server config (idmap_ad / RFC2307)
#   5. domain join, winbind, NSS/PAM
#   6. verification (getent / id / wbinfo / kinit), no secrets printed
#   7. the M5 exit test's tools (install-e2e-tools.sh)
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

ENV_FILE="${AD_ENV_FILE:-/work/dev/.secrets/ad.env}"
STATE=/var/lib/omnifile-dev
EDGE_SHORT="edge1"
DATA_SIZE="${DATA_SIZE:-64G}"
CACHE_SIZE="${CACHE_SIZE:-16G}"

log() { printf '[edge-provision] %s\n' "$*"; }
die() { printf '[edge-provision] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -r "$ENV_FILE" ] || die "$ENV_FILE not readable (run dev/up.sh, which fetches it from the DC)"
install -d -m 0755 "$STATE"

# shellcheck disable=SC1090
. "$ENV_FILE"
: "${AD_REALM:?}" "${AD_DOMAIN:?}" "${AD_DC_HOST:?}" "${AD_DC_IP:?}" "${AD_ADMIN_PASSWORD:?}"
REALM_LC="${AD_REALM,,}"
EDGE_FQDN="${EDGE_SHORT}.${REALM_LC}"

# 1. Packages ----------------------------------------------------------------------
if [ ! -f "$STATE/packages-installed" ]; then
  log "installing packages (first run takes a few minutes)"
  echo "krb5-config krb5-config/default_realm string $AD_REALM" | debconf-set-selections
  echo "krb5-config krb5-config/kerberos_servers string $AD_DC_HOST" | debconf-set-selections
  echo "krb5-config krb5-config/admin_server string $AD_DC_HOST" | debconf-set-selections
  apt-get update -qq
  apt-get install -y -qq -o DPkg::Lock::Timeout=600 \
    build-essential cmake bison flex pkg-config git curl ca-certificates \
    clang libclang-dev libssl-dev libkrb5-dev libcap-dev libblkid-dev \
    libdbus-1-dev liburcu-dev libjemalloc-dev libnsl-dev libacl1-dev uuid-dev \
    libnfsidmap-dev libsqlite3-dev libattr1-dev \
    python3 python3-venv python3-pip \
    xfsprogs acl attr fio procps psmisc strace \
    nfs-common rpcbind cifs-utils smbclient \
    samba winbind libnss-winbind libpam-winbind krb5-user ldb-tools dnsutils \
    >/dev/null
  touch "$STATE/packages-installed"
fi

# The spike (and later the product) run their own smbd/Ganesha units; a stock
# smbd/nmbd with no shares would only get in the way. winbind is enabled below.
systemctl disable --now smbd nmbd >/dev/null 2>&1 || true

# 2. Volumes (xfs on sparse loop files) -------------------------------------------------
install -d -m 0755 /var/lib/omnifile /srv/omnifile
mkvol() { # name size mountpoint
  local img="/var/lib/omnifile/$1.img" size="$2" mp="$3"
  install -d -m 0755 "$mp"
  [ -f "$img" ] || { truncate -s "$size" "$img"; mkfs.xfs -q -f -L "omni-$1" "$img"; }
  grep -q "^$img " /etc/fstab \
    || echo "$img $mp xfs loop,nofail,noatime 0 0" >>/etc/fstab
  mountpoint -q "$mp" || mount "$mp"
}
mkvol data "$DATA_SIZE" /srv/omnifile/data
mkvol cache "$CACHE_SIZE" /srv/omnifile/cache
systemctl daemon-reload

# 3. Hostname + DNS --------------------------------------------------------------------
cat >/etc/cloud/cloud.cfg.d/99-omnifile-dev.cfg <<'EOF'
preserve_hostname: true
manage_etc_hosts: false
EOF
hostnamectl set-hostname "$EDGE_SHORT"
sed -i '/# omnifile-dev$/d' /etc/hosts
printf '127.0.1.1 %s %s # omnifile-dev\n' "$EDGE_FQDN" "$EDGE_SHORT" >>/etc/hosts
printf '%s %s # omnifile-dev\n' "$AD_DC_IP" "$AD_DC_HOST" >>/etc/hosts

# Route only the realm to the DC through a *global* resolved scope: a per-link
# routing domain would not do, because the Lima resolver on the same link also
# answers (NXDOMAIN) for omni.test. Everything else keeps using the Lima
# resolver, so the edge still resolves the internet while the DC is stopped.
# Rewritten every run because the DC address comes from DHCP.
RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/omnifile-dev.conf
want="[Resolve]
DNS=$AD_DC_IP
Domains=~$REALM_LC"
install -d /etc/systemd/resolved.conf.d
if [ "$(cat "$RESOLVED_DROPIN" 2>/dev/null || true)" != "$want" ]; then
  printf '%s\n' "$want" >"$RESOLVED_DROPIN"
  systemctl restart systemd-resolved
fi
resolvectl flush-caches || true
for i in $(seq 1 30); do
  host -t SRV "_ldap._tcp.${REALM_LC}" >/dev/null 2>&1 && break
  sleep 2
  [ "$i" -lt 30 ] || die "realm $REALM_LC does not resolve through the DC ($AD_DC_IP); is omnifile-dc running?"
done

# 4. Kerberos + Samba member config ---------------------------------------------------------
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

# idmap_ad with RFC2307 attributes for the domain (uidNumber/gidNumber from AD,
# so every Linux client agrees on ids); a tdb range for anything without them.
[ -f /etc/samba/smb.conf.orig-omnifile ] || cp -a /etc/samba/smb.conf /etc/samba/smb.conf.orig-omnifile 2>/dev/null || true
# Once dev/scripts/install-samba.sh has installed the product's smb.conf
# (edge/config/samba/smb.conf.tmpl, with the shares include), keep it: it
# carries the same idmap and join settings, and rewriting it here would drop
# every share.
if grep -q '^include = /etc/samba/omnifile-shares.conf' /etc/samba/smb.conf 2>/dev/null; then
  log "keeping the product smb.conf (install-samba.sh)"
else
cat >/etc/samba/smb.conf <<EOF
# Managed by dev/scripts/edge-provision.sh (member server, no shares yet).
[global]
    workgroup = $AD_DOMAIN
    realm = $AD_REALM
    security = ads
    netbios name = EDGE1
    kerberos method = secrets and keytab
    dedicated keytab file = /etc/krb5.keytab

    winbind use default domain = yes
    winbind enum users = yes
    winbind enum groups = yes
    winbind nested groups = yes
    winbind refresh tickets = yes
    winbind offline logon = yes
    template shell = /bin/bash
    template homedir = /home/%U

    idmap config * : backend = tdb
    idmap config * : range = 3000-7999
    idmap config $AD_DOMAIN : backend = ad
    idmap config $AD_DOMAIN : schema_mode = rfc2307
    idmap config $AD_DOMAIN : range = 10000-999999
    idmap config $AD_DOMAIN : unix_nss_info = yes

    load printers = no
    printing = bsd
    printcap name = /dev/null
    disable spoolss = yes
EOF
fi
testparm -s >/dev/null 2>&1 || die "smb.conf does not parse"

# 5. Join + winbind + NSS/PAM --------------------------------------------------------------------
if ! net ads testjoin >/dev/null 2>&1; then
  log "joining $AD_REALM"
  systemctl stop winbind >/dev/null 2>&1 || true
  # A stale machine secret (DC rebuilt) would block a fresh join.
  rm -f /var/lib/samba/private/secrets.tdb /var/lib/samba/gencache.tdb /var/cache/samba/gencache.tdb
  # PASSWD keeps the password off the command line.
  PASSWD="$AD_ADMIN_PASSWORD" net ads join -U "${AD_ADMIN_USER:-Administrator}" >"$STATE/join.log" 2>&1 \
    || { tail -n 20 "$STATE/join.log" >&2; die "domain join failed"; }
  net ads dns register -P >/dev/null 2>&1 || true
fi

for db in passwd group; do
  grep -Eq "^${db}:.*\bwinbind\b" /etc/nsswitch.conf \
    || sed -i -E "s/^(${db}:[[:space:]]+.*)\$/\1 winbind/" /etc/nsswitch.conf
done
pam-auth-update --enable winbind mkhomedir >/dev/null 2>&1 || true
systemctl enable winbind >/dev/null 2>&1
systemctl restart winbind
for i in $(seq 1 30); do wbinfo -t >/dev/null 2>&1 && break; sleep 2; [ "$i" -lt 30 ] || die "winbind trust check never succeeded"; done

# 6. Verify (no secrets printed) -------------------------------------------------------------------
for i in $(seq 1 30); do getent passwd geo-reader >/dev/null 2>&1 && break; sleep 2; [ "$i" -lt 30 ] || die "getent passwd geo-reader found nothing"; done
uid="$(id -u geo-reader)"
[ "$uid" = 20001 ] || die "geo-reader uid is $uid, expected 20001 from RFC2307 (idmap_ad misconfigured?)"
id geo-nested | grep -qi 'proj-readers)' || die "nested group membership not resolved for geo-nested"

# 7. The M5 exit test's tools (libnfs, nftables, conntrack, vfs_omnifile_prevver) -----------------
bash "$(dirname "${BASH_SOURCE[0]}")/install-e2e-tools.sh"
log "ready: joined $AD_REALM as $EDGE_FQDN; geo-reader uid=$uid"
