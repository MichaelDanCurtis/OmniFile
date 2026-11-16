#!/usr/bin/env bash
# The certificate authority that issued a server's TLS certificate (docs/guide/README.md, section 9.1).
#
#   docs/guide/tools/tls-issuer.sh --host FQDN|https://FQDN[:PORT] [--port 443] [--out issuer.pem]
#                                  [--ca-file BUNDLE]
#
# Connects once (openssl s_client) and verifies what the server presents: the chain against this
# machine's trusted CAs (or --ca-file), and the certificate's name against the host. If either check
# fails it stops, writes nothing and exits 1: a pin taken from an unverified chain (an interception
# proxy, a wrong DNS answer) would send every enrolling edge to the wrong server. Otherwise it writes the
# certificate that issued the server's own one (the second in the chain) to --out, prints its subject,
# and prints the SHA-256 of its DER as the last line: the pin enrolment codes carry
# (`enrolServerCaSha256`). Needs OpenSSL 3 or later (not LibreSSL).
set -euo pipefail

HOST='' PORT=443 OUT=issuer.pem CA=''
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift ;;
    --port) PORT=$2; shift ;;
    --out) OUT=$2; shift ;;
    --ca-file) CA=$2; shift ;;
    *) echo "usage: tls-issuer.sh --host FQDN [--port 443] [--out issuer.pem] [--ca-file BUNDLE]" >&2; exit 2 ;;
  esac
  shift
done
HOST=${HOST#https://}
HOST=${HOST%%/*}
if [[ $HOST == *:* ]]; then PORT=${HOST##*:}; HOST=${HOST%%:*}; fi
[ -n "$HOST" ] || { echo "tls-issuer.sh: --host is required" >&2; exit 2; }
openssl version | grep -Eq '^OpenSSL ([3-9]|[1-9][0-9]+)\.' ||
  { echo "tls-issuer.sh: needs OpenSSL 3 or later for the name check ($(openssl version))" >&2; exit 2; }

chain=$(mktemp)
trap 'rm -f "$chain" "$OUT.new"' EXIT
verify=(-verify_return_error -verify_hostname "$HOST")
if [ -n "$CA" ]; then verify+=(-CAfile "$CA"); fi
if ! openssl s_client -connect "$HOST:$PORT" -servername "$HOST" -showcerts "${verify[@]}" </dev/null >"$chain" 2>&1; then
  echo "tls-issuer.sh: $HOST:$PORT did not present a certificate this machine trusts for $HOST:" >&2
  grep -E 'verify error|Verify return code|errno|connect:' "$chain" | head -n 3 >&2 || true
  exit 1
fi
grep -q 'Verify return code: 0 (ok)' "$chain" ||
  { echo "tls-issuer.sh: $HOST:$PORT: the chain did not verify" >&2; exit 1; }
# Keep the second certificate block: the issuer of the server's certificate.
awk '/-----BEGIN CERTIFICATE-----/ {n++} n == 2 {print} /-----END CERTIFICATE-----/ && n == 2 {n++}' "$chain" >"$OUT.new"
if ! openssl x509 -in "$OUT.new" -noout 2>/dev/null; then
  echo "tls-issuer.sh: $HOST:$PORT sent no issuing certificate after its own" >&2
  exit 1
fi
mv "$OUT.new" "$OUT"
openssl x509 -in "$OUT" -noout -subject
openssl x509 -in "$OUT" -outform DER | openssl dgst -sha256 | awk '{print $NF}'
