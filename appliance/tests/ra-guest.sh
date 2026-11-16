# shellcheck shell=bash
# Guest-side helpers for the Roles Anywhere checks (smoke.sh, setup.sh): appended to a
# guest script, which then calls ra_serves. Bash only: the image needs no curl or python.

# ra_http METHOD PATH [HEADER...]: one HTTP/1.0 request (never chunked) to the helper on 127.0.0.1:9911; prints
# the status line, a blank line, then the body.
ra_http() {
  local method=$1 path=$2 h req
  shift 2
  req="$method $path HTTP/1.0\r\nHost: 127.0.0.1:9911\r\nConnection: close\r\nContent-Length: 0\r\n"
  for h in "$@"; do req="$req$h\r\n"; done
  exec 3<>/dev/tcp/127.0.0.1/9911 || return 1
  printf "$req\r\n" >&3
  timeout 20 cat <&3 | tr -d '\r' | sed -n '1p; /^$/,$p'
  exec 3<&- 3>&-
}

# ra_serves: waits for the helper, then does what the edge's S3 reader does (IMDSv2: a
# token, the role's name, its credentials) and prints one line:
#   ra_serves=<yes|no> active=<state> token=<yes|no> role=<name> access_key=<id>
ra_serves() {
  local i state token role creds key
  for i in $(seq 1 60); do
    state=$(systemctl is-active omnifile-aws-signing-helper.service)
    [ "$state" = active ] && (exec 3<>/dev/tcp/127.0.0.1/9911) 2>/dev/null && break
    sleep 1
  done
  token=$(ra_http PUT /latest/api/token "X-aws-ec2-metadata-token-ttl-seconds: 300" | sed -n '3,$p' | tr -d '\n')
  role=$(ra_http GET /latest/meta-data/iam/security-credentials/ "X-aws-ec2-metadata-token: $token" |
    sed -n '3,$p' | head -1)
  creds=$(ra_http GET "/latest/meta-data/iam/security-credentials/$role" "X-aws-ec2-metadata-token: $token" |
    sed -n '3,$p' | tr -d '\n')
  key=$(printf '%s' "$creds" | grep -o '"AccessKeyId" *: *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/')
  # Without the token, IMDSv2 answers nothing.
  local bare
  bare=$(ra_http GET "/latest/meta-data/iam/security-credentials/$role" | head -1 | cut -d' ' -f2)
  echo "ra_serves=$([ -n "$key" ] && echo yes || echo no) active=$state token=$([ -n "$token" ] && echo yes || echo no) role=${role:-none} access_key=${key:-none} untokened=$bare"
  if [ -z "$key" ]; then
    journalctl -b -u omnifile-aws-signing-helper -o cat --no-pager | tail -8 | sed 's/^/  helper: /'
  fi
}
