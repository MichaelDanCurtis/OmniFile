#!/usr/bin/env bash
# The Azure run's test VM (Ubuntu 24.04 on a Dv5 size with nested virtualisation): a KVM host
# for the appliance, Docker for the Samba DC container, the NFS and SMB client tools, and a
# Python venv for the VM-side checks. Run as root by the harness (tests/real/harness/vm.py).
# Idempotent.
set -euo pipefail
R=/srv/ofreal
if [ ! -e /dev/kvm ]; then
  echo "no /dev/kvm: this size or image has no nested virtualisation (VERIFY: Dv5 with" \
    "--security-type Standard)" >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q qemu-kvm qemu-utils libvirt-daemon-system virtinst ovmf swtpm swtpm-tools \
  docker.io nfs-common cifs-utils smbclient krb5-user python3-venv jq zstd mtools sbsigntool \
  openssl >/dev/null
usermod -aG libvirt,kvm omnitest
systemctl enable --now libvirtd docker

# The appliance's network: NAT behind this VM (so it reaches the internal environment and the
# private endpoints from the VM's subnet, which enrolAllowedCidrs and webAllowedCidrs list),
# with the run's AD realm forwarded to the DC container.
docker network inspect ofreal >/dev/null 2>&1 ||
  docker network create --subnet 172.30.0.0/24 ofreal >/dev/null
if ! virsh net-info ofreal >/dev/null 2>&1; then
  cat >/tmp/ofreal-net.xml <<'XML'
<network>
  <name>ofreal</name>
  <forward mode='nat'/>
  <bridge name='virbr-ofreal'/>
  <dns><forwarder domain='ofreal.test' addr='172.30.0.10'/></dns>
  <ip address='192.168.150.1' netmask='255.255.255.0'>
    <dhcp><range start='192.168.150.10' end='192.168.150.100'/></dhcp>
  </ip>
</network>
XML
  virsh net-define /tmp/ofreal-net.xml
  virsh net-autostart ofreal
  virsh net-start ofreal
fi
# Docker's FORWARD policy would drop the appliance's traffic to the DC container.
iptables -C DOCKER-USER -i virbr-ofreal -o br-+ -j ACCEPT 2>/dev/null ||
  iptables -I DOCKER-USER -i virbr-ofreal -j ACCEPT
iptables -C DOCKER-USER -o virbr-ofreal -j ACCEPT 2>/dev/null ||
  iptables -I DOCKER-USER -o virbr-ofreal -j ACCEPT

mkdir -p "$R/release" /mnt/ofreal
if [ ! -x "$R/venv/bin/python" ]; then
  python3 -m venv "$R/venv"
  "$R/venv/bin/pip" install -q "azure-storage-blob[ext-checksums]>=12.31,<13" \
    "azure-storage-blob-changefeed>=12.0.0b5" "azure-identity>=1.26,<2" \
    "psycopg[binary]>=3.2,<4" "httpx>=0.28" "cryptography>=45" "pyjwt[crypto]>=2.10,<3"
fi
chown -R omnitest: "$R"
echo '{"ok": true, "kvm": true}'
