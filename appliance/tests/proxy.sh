#!/usr/bin/env bash
# The update feed through a client's HTTP proxy (real-azure-checks.md, "the update feed through
# a client proxy"): squid between the appliance and its feed, and a proxy that rewrites what it
# relays, which the feed's signature must catch.
#
#   appliance/tests/proxy.sh --disk 0.1.0.raw --feed 0.2.0/feed --keys DIR [--squid PATH]
#
# KEYS holds update.pem and update-ring.pgp (the throwaway update key the image embeds,
# build.sh --pubring). On the host, all on 127.0.0.1, so the guest reaches them only through
# the proxy (qemu's user network: the guest's own 127.0.0.1 is itself):
#   - the feed's origin over HTTP and over HTTPS (a test CA, SAN 127.0.0.1);
#   - squid, the client's proxy (no cache). Its URL rewriter makes it a proxy that rewrites
#     content: under /t-payload/ it relays the feed with one byte of /usr changed, under
#     /t-sums/ with /usr changed and SHA256SUMS rewritten to match (the signature can't be).
# The box is pointed at the proxy the way an admin would (http_proxy/https_proxy on
# systemd-sysupdate.service, a drop-in), and at each feed with a transfer drop-in. In 0.1.0:
#   1. without the proxy the feed is unreachable;
#   2. through the proxy over HTTP, check-new sees 0.2.0 (the signed index verified);
#   3. through the rewriting proxy, a changed /usr is refused (its SHA-256 isn't SHA256SUMS'),
#      and nothing is installed;
#   4. a rewritten SHA256SUMS is refused (the signature fails), and nothing is installed;
#   5. through the proxy over HTTPS (CONNECT; the proxy sees no content), 0.2.0 installs,
#      and the box reboots into it.
# squid's access log must show the requests (GET for HTTP, CONNECT for HTTPS).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' FEED='' KEYS='' SQUID=${SQUID:-}
P_HTTP=${P_HTTP:-18081} P_EVIL=${P_EVIL:-18082} P_TLS=${P_TLS:-18843} P_PROXY=${P_PROXY:-13128}
WORK=${WORK:-$HERE/../build/proxy}
while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK=$2; shift ;;
    --feed) FEED=$2; shift ;;
    --keys) KEYS=$2; shift ;;
    --squid) SQUID=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DISK" ] && [ -n "$FEED" ] && [ -n "$KEYS" ] || { sed -n '2,26p' "$0"; exit 2; }
for s in "$SQUID" /opt/homebrew/opt/squid/sbin/squid /usr/sbin/squid "$(command -v squid || true)"; do
  if [ -n "$s" ] && [ -x "$s" ]; then SQUID=$s; break; fi
done
[ -x "$SQUID" ] || { echo "squid is needed (--squid PATH)" >&2; exit 2; }
KEYS=$(cd "$KEYS" && pwd)
rm -rf "$WORK" && mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
vm_init "$DISK"

# ---- the feeds: as released, and as a rewriting proxy would relay them -------------------------
O=$WORK/origin E=$WORK/evil
mkdir -p "$O/good" "$E/t-payload" "$E/t-sums"
find "$FEED" -maxdepth 1 -type f -name 'omnifile-edge_*' -exec cp {} "$O/good/" \;
(cd "$O/good" && sha256sum -- omnifile-edge_* >SHA256SUMS)
python3 "$HERE/../pgp_kv.py" sign --signer "fake:$KEYS/update.pem" --created 1792540800 \
  --ring "$KEYS/update-ring.pgp" --in "$O/good/SHA256SUMS" --out "$O/good/SHA256SUMS.gpg" >/dev/null
V2=$(find "$O/good" -name 'omnifile-edge_*.efi' | sed -n 's|.*/omnifile-edge_\(.*\)\.efi$|\1|p')
USR=$(cd "$O/good" && ls omnifile-edge_"$V2".usr-*.raw.zst | grep -v verity)
cp "$O/good"/* "$E/t-payload/"
# One byte of the compressed /usr, past its header: still a file the proxy relays whole.
python3 - "$E/t-payload/$USR" <<'PY'
import sys
p = sys.argv[1]
with open(p, "r+b") as f:
    f.seek(4096)
    b = f.read(1)
    f.seek(4096)
    f.write(bytes([b[0] ^ 0xff]))
PY
cp "$O/good"/* "$E/t-sums/"
cp "$E/t-payload/$USR" "$E/t-sums/$USR"
(cd "$E/t-sums" && sha256sum -- omnifile-edge_* >SHA256SUMS)
echo "feeds: good, t-payload (one byte of $USR), t-sums (that /usr and a SHA256SUMS to match)"

# ---- the origin (HTTP and HTTPS), the rewriter's source, and squid --------------------------------
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$WORK/ca.key" -out "$WORK/ca.pem" \
  -days 2 -subj "/CN=OmniFile proxy test CA" -addext basicConstraints=critical,CA:TRUE \
  -addext keyUsage=critical,keyCertSign 2>/dev/null
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$WORK/tls.key" -out "$WORK/tls.csr" \
  -subj "/CN=127.0.0.1" 2>/dev/null
printf 'subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' >"$WORK/tls.ext"
openssl x509 -req -in "$WORK/tls.csr" -CA "$WORK/ca.pem" -CAkey "$WORK/ca.key" -CAcreateserial -days 1 \
  -extfile "$WORK/tls.ext" -out "$WORK/tls.pem" 2>/dev/null
cat >"$WORK/serve.py" <<'PY'
import functools, http.server, ssl, sys
port, root = int(sys.argv[1]), sys.argv[2]
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
s = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
if len(sys.argv) > 3:
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.load_cert_chain(sys.argv[3], sys.argv[4])
    s.socket = c.wrap_socket(s.socket, server_side=True)
s.serve_forever()
PY
# squid takes no spaces in its paths: its config, logs and rewriter live in a short /tmp path.
SQ=$(mktemp -d /tmp/omnifile-squid.XXXXXX)
cat >"$SQ/rewrite.py" <<PY
import sys
# squid's URL rewriter (url_rewrite_program, concurrency 0): one request per line, the URL first.
for line in sys.stdin:
    url = line.split(" ", 1)[0]
    for p in ("t-payload", "t-sums"):
        pre = "http://127.0.0.1:$P_HTTP/%s/" % p
        if url.startswith(pre):
            sys.stdout.write('OK rewrite-url="http://127.0.0.1:$P_EVIL/%s/%s"\n' % (p, url[len(pre):]))
            break
    else:
        sys.stdout.write("ERR\n")
    sys.stdout.flush()
PY
cat >"$SQ/squid.conf" <<EOF
http_port 127.0.0.1:$P_PROXY
acl localhost src 127.0.0.1/32
acl tls_feed port $P_TLS
acl CONNECT method CONNECT
http_access deny CONNECT !tls_feed
http_access allow localhost
http_access deny all
cache deny all
url_rewrite_program $(command -v python3) $SQ/rewrite.py
url_rewrite_children 1 startup=1 idle=1 concurrency=0
access_log stdio:$SQ/access.log
cache_log $SQ/cache.log
pid_filename $SQ/squid.pid
coredump_dir $SQ
shutdown_lifetime 1 seconds
EOF
PIDS=()
python3 "$WORK/serve.py" "$P_HTTP" "$O" >"$WORK/origin.log" 2>&1 & PIDS+=($!)
python3 "$WORK/serve.py" "$P_EVIL" "$E" >"$WORK/evil.log" 2>&1 & PIDS+=($!)
python3 "$WORK/serve.py" "$P_TLS" "$O" "$WORK/tls.pem" "$WORK/tls.key" >"$WORK/tls.log" 2>&1 & PIDS+=($!)
"$SQUID" -N -f "$SQ/squid.conf" >"$WORK/squid.out" 2>&1 & PIDS+=($!)
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT
sleep 3
echo "squid $("$SQUID" -v | head -1 | sed 's/.*Version //'), proxy 127.0.0.1:$P_PROXY; origin :$P_HTTP (HTTP), :$P_TLS (HTTPS); rewritten from :$P_EVIL"

# ---- the guest ---------------------------------------------------------------------------------------
cat >"$WORK/guest.sh" <<SH
P_HTTP=$P_HTTP P_TLS=$P_TLS P_PROXY=$P_PROXY V2=$V2
SH
cat >>"$WORK/guest.sh" <<'SH'
d=/var/lib/omnifile-proxy; mkdir -p $d
n=$(( $(cat $d/n 2>/dev/null || echo 0) + 1 )); echo $n >$d/n
. /usr/lib/os-release
echo "PROXY boot=$n version=$IMAGE_VERSION"
feed() { # feed URL: the transfers read URL
  for t in 10-uki 20-usr 21-usr-verity; do
    mkdir -p /etc/sysupdate.d/$t.transfer.d
    printf '[Source]\nPath=%s\n' "$1" >/etc/sysupdate.d/$t.transfer.d/50-test.conf
  done
}
# What is installed, read off the disk (no feed needed): V2's UKI on the ESP and its /usr slot.
installed() {
  local esp; esp=$(bootctl --print-boot-path 2>/dev/null || echo /boot)
  echo $(( $(ls "$esp/EFI/Linux" | grep -c "^omnifile-edge_$V2") + $(lsblk -nro PARTLABEL | grep -cx "omnifile-edge_$V2") ))
}
uenv() { # the environment systemd-sysupdate.service gives it (the proxy, once set)
  systemctl show -p Environment --value systemd-sysupdate.service
}
if [ "$n" -gt 1 ] && [ "$IMAGE_VERSION" != "$V2" ]; then
  # Rebooted without the update installed: report and stop, never loop.
  echo "PROXY-END version=$IMAGE_VERSION (no update installed)"
  systemctl --no-block poweroff
elif [ "$IMAGE_VERSION" != "$V2" ]; then
  # The client's CA, for the HTTPS case only (a corporate CA a client may run).
  cat /run/credentials/@system/proxy.ca >>/etc/ssl/certs/ca-certificates.crt
  feed "http://127.0.0.1:$P_HTTP/good/"
  timeout 120 env $(uenv) /usr/lib/systemd/systemd-sysupdate --no-pager check-new >$d/1.log 2>&1; rc=$?
  echo "PROXY 1 direct: rc=$rc $(tail -1 $d/1.log | cut -c1-160)"
  mkdir -p /etc/systemd/system/systemd-sysupdate.service.d
  printf '[Service]\nEnvironment=http_proxy=http://10.0.2.2:%s https_proxy=http://10.0.2.2:%s\n' "$P_PROXY" "$P_PROXY" \
    >/etc/systemd/system/systemd-sysupdate.service.d/50-proxy.conf
  systemctl daemon-reload
  echo "PROXY env: $(systemctl show -p Environment --value systemd-sysupdate.service)"
  timeout 300 env $(uenv) /usr/lib/systemd/systemd-sysupdate --no-pager check-new >$d/2.log 2>&1; rc=$?
  echo "PROXY 2 http via proxy: rc=$rc new=$(grep -c "$V2" $d/2.log) $(tail -1 $d/2.log | cut -c1-120)"
  feed "http://127.0.0.1:$P_HTTP/t-payload/"
  timeout 900 env $(uenv) /usr/lib/systemd/systemd-sysupdate --no-pager update >$d/3.log 2>&1; rc=$?
  echo "PROXY 3 rewritten usr: rc=$rc installed=$(installed) $(grep -iE 'hash|checksum|sha256|mismatch|verif' $d/3.log | tail -2 | tr '\n' ' ' | cut -c1-240)"
  feed "http://127.0.0.1:$P_HTTP/t-sums/"
  timeout 900 env $(uenv) /usr/lib/systemd/systemd-sysupdate --no-pager update >$d/4.log 2>&1; rc=$?
  echo "PROXY 4 rewritten SHA256SUMS: rc=$rc installed=$(installed) $(grep -iE 'signature|gpg|verif' $d/4.log | tail -2 | tr '\n' ' ' | cut -c1-240)"
  feed "https://127.0.0.1:$P_TLS/good/"
  timeout 900 env $(uenv) /usr/lib/systemd/systemd-sysupdate --no-pager update >$d/5.log 2>&1; rc=$?
  echo "PROXY 5 https via proxy: rc=$rc installed=$(installed) $(tail -1 $d/5.log | cut -c1-160)"
  sync; systemctl --no-block reboot
else
  echo "PROXY-END version=$IMAGE_VERSION boot_complete=$(systemctl is-active boot-complete.target)"
  systemctl --no-block poweroff
fi
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
vm_prepare "$WORK" "$DISK" 16G
mapfile -t EXTRA < <(vm_cred proxy.ca "$(cat "$WORK/ca.pem")")
echo "booting $DISK (log: $WORK/serial.log)"
vm_run "$WORK" "${PROXY_TIMEOUT:-3600}" omnifile-proxy.service "$WORK/guest.unit" "${EXTRA[@]}" || true
R=$(grep -ao 'PROXY.*' "$WORK/serial.log" | tr -d '\r')
printf '%s\n' "$R"
cp "$SQ"/*.log "$WORK/" 2>/dev/null || true
A=$(cat "$SQ/access.log" 2>/dev/null)
echo "squid access log: $(wc -l <<<"$A") lines"; grep -oE '(GET|CONNECT) [^ ]+' <<<"$A" | sort | uniq -c | sed 's/^/  /'
fail=0
check() { # check NAME REGEX [TEXT]
  if grep -Eq "$2" <<<"${3:-$R}"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
check "without the proxy, the feed is unreachable" '^PROXY 1 direct: rc=[1-9]'
check "the box takes the proxy from systemd-sysupdate's environment" "^PROXY env: .*https_proxy=http://10\\.0\\.2\\.2:$P_PROXY"
check "through the proxy over HTTP, the signed index shows $V2" '^PROXY 2 http via proxy: rc=0 new=[1-9]'
check "a /usr the proxy rewrote is refused, nothing installed" '^PROXY 3 rewritten usr: rc=[1-9][0-9]* installed=0'
check "a SHA256SUMS the proxy rewrote is refused by the signature, nothing installed" '^PROXY 4 rewritten SHA256SUMS: rc=[1-9][0-9]* installed=0'
check "through the proxy over HTTPS, $V2 installs" '^PROXY 5 https via proxy: rc=0 installed=[1-9]'
check "and the box boots it" "^PROXY-END version=$V2 boot_complete=active"
check "squid relayed the HTTP feed" "GET http://127\\.0\\.0\\.1:$P_HTTP/good/SHA256SUMS" "$A"
# squid logs a rewritten request under the URL it fetched instead.
check "squid relayed the rewritten feeds (from the rewriter's source)" "GET http://127\\.0\\.0\\.1:$P_EVIL/t-payload/.*\\.usr-.*\\.raw\\.zst" "$A"
check "and the rewritten SHA256SUMS" "GET http://127\\.0\\.0\\.1:$P_EVIL/t-sums/SHA256SUMS" "$A"
check "squid tunnelled the HTTPS feed" "CONNECT 127\\.0\\.0\\.1:$P_TLS" "$A"
exit $fail
