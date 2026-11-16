#!/usr/bin/env bash
# A guide on=edge-shell step, run on the appliance as an AD admin over ssh, with a Kerberos
# ticket from the run's DC (GSSAPI; no password on a command line). stdin: the block, exactly
# as written, base64 on the first line; anything after it is the block's own stdin (a step's
# input=NAME). The appliance's address comes from libvirt.
set -euo pipefail
R=/srv/ofreal
realm=$(python3 -c 'import json; print(json.load(open("/srv/ofreal/secrets.json"))["realm"].upper())')
python3 -c 'import json; print(json.load(open("/srv/ofreal/secrets.json"))["join"])' |
  kinit "ofreal-join@$realm" >/dev/null
edge=$(virsh list --name | head -1)
ip=$(virsh domifaddr --source lease "$edge" | awk '/ipv4/ {split($4, a, "/"); print a[1]}')
read -r b64
exec ssh -K -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$R/edge_known_hosts" \
  "ofreal-join@$ip" "bash -euo pipefail -c \"\$(echo $b64 | base64 -d)\""
