#!/usr/bin/env bash
# Check that names resolve to private addresses only (docs/guide/README.md, step 23).
#
#   docs/guide/tools/check-private-dns.sh NAME [NAME ...]
#
# Each NAME may carry a port or path (it is cut off). Resolves each one with this machine's resolver and
# prints its addresses. A name that doesn't resolve, or resolves to any address outside RFC 1918, shared
# address space (100.64.0.0/10) or IPv6 unique-local, is a failure: the edge would refuse it, since it
# never falls back to a public path (decision 22; docs/design/setup-wizard.md, "Private networking").
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: check-private-dns.sh NAME [NAME ...]" >&2; exit 2; }

python3 -I - "$@" <<'PY'
import ipaddress, socket, sys

PRIVATE = [ipaddress.ip_network(n) for n in
           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")]
bad = 0
for raw in sys.argv[1:]:
    host = raw.split("://")[-1].split("/")[0]
    if host.count(":") == 1:
        host = host.split(":")[0]
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except socket.gaierror as e:
        print(f"  {host}: does not resolve ({e.strerror})")
        bad += 1
        continue
    public = [a for a in addrs if not any(ipaddress.ip_address(a) in n for n in PRIVATE)]
    print(f"  {host}: {', '.join(addrs)}" + (f"  PUBLIC: {', '.join(public)}" if public else ""))
    bad += bool(public)
if bad:
    print(f"{bad} name(s) not private: link the private DNS zones to the hub and add the forwarders (section 6)")
    sys.exit(1)
print("all private")
PY
