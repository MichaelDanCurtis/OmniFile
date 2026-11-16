#!/usr/bin/env bash
# The Arc spike (docs/design/appliance-image.md, "Arc agent"): the Azure Connected Machine
# agent's package as a systemd-sysext image for the appliance's read-only /usr.
#
#   appliance/arc/make-sysext.sh AZCMAGENT.deb OUT.raw [--arch x86-64|arm64]
#
# Run on Linux (dpkg-deb, mkfs.erofs). The package's files are all under /opt (azcmagent,
# GC_Service, GC_Ext); its maintainer scripts are not run. What they would do at install time
# is declared instead, so the image needs no writable /usr:
#   - the units they copy into /lib/systemd/system (himdsd.service and its four sockets,
#     arcproxyd.service) go in the sysext's /usr/lib/systemd/system;
#   - the users they add (himds, arcproxy; himds in tss) as sysusers.d;
#   - /var/opt/azcmagent and its first agentconfig.json as tmpfiles.d;
#   - no auto-upgrade cron job: a new agent version is a new sysext.
# The Guest Configuration and extension services (gcad, extd), whose install scripts write
# their units at install time, are left out of the spike: extensions that install packages
# (the Azure Monitor Agent) need dpkg and a writable /usr, which the appliance has neither of.
# The image is erofs, unsigned: a release would add dm-verity and a signature, which
# systemd-sysext checks against the image policy.
set -euo pipefail
DEB=$1 OUT=$2 ARCH=x86-64
[ "${3:-}" = --arch ] && ARCH=$4
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
dpkg-deb -x "$DEB" "$T"
D=$T/opt/azcmagent/datafiles
mkdir -p "$T/usr/lib/systemd/system" "$T/usr/lib/sysusers.d" "$T/usr/lib/tmpfiles.d" \
  "$T/usr/lib/extension-release.d" "$T/usr/bin"
for u in himdsd.service himdsd.socket himdsd-ipv6.socket himdsd-https.socket himdsd-https-ipv6.socket arcproxyd.service; do
  cp "$D/$u" "$T/usr/lib/systemd/system/"
done
ln -s /opt/azcmagent/bin/azcmagent "$T/usr/bin/azcmagent"
cat >"$T/usr/lib/sysusers.d/azcmagent.conf" <<'EOF'
g himds - -
u himds -:himds - - /bin/false
u arcproxy -:himds - - /bin/false
g tss - -
m himds tss
EOF
cat >"$T/usr/lib/tmpfiles.d/azcmagent.conf" <<'EOF'
d /var/opt/azcmagent 0755 himds himds -
d /var/opt/azcmagent/log 0775 himds himds -
d /var/opt/azcmagent/certs 0700 himds himds -
d /var/opt/azcmagent/socks 0711 himds himds -
d /var/opt/azcmagent/tokens 0750 himds himds -
C /var/opt/azcmagent/agentconfig.json 0644 himds himds - /opt/azcmagent/datafiles/agentconfig.json
EOF
# systemd-sysext merges an extension only into the OS it names: Ubuntu 26.04, this architecture.
cat >"$T/usr/lib/extension-release.d/extension-release.azcmagent" <<EOF
ID=ubuntu
VERSION_ID=26.04
ARCHITECTURE=$ARCH
EOF
# mkfs.erofs writes to a local file (it fails on a virtiofs share), then the result moves.
mkfs.erofs --all-root "$T.raw" "$T" >/dev/null
mv "$T.raw" "$OUT"
ls -la "$OUT"
