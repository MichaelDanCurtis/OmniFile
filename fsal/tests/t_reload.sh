#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Shares added and removed in the portal (docs/design/share-provisioning.md):
# the edge daemon rewrites the EXPORT blocks it renders, in a file
# ganesha.conf includes, and reloads Ganesha (`systemctl reload`, which is
# SIGHUP). Checked here against a running ganesha.nfsd, over NFSv3 and v4
# with the userspace client:
# - an export added to the included file is served after SIGHUP;
# - the exports already there keep serving through it;
# - an export removed from the file is gone after the next SIGHUP;
# - Ganesha stays up throughout.
. "$(dirname "$0")/lib.sh"
setup_volumes

nfsc() { python3 "$T/nfsc.py" "$@"; }
INC=$B/rendered-exports.conf
block() { # <export id> <name> <policy>
	cat <<BLOCK
# share $2
EXPORT {
    Export_Id = $1;
    Path = "$DATA/$2";
    Pseudo = "/$2";
    Protocols = 3, 4;
    Transports = TCP;
    SecType = sys;
    Squash = No_Root_Squash;
    Access_Type = RW;
    FSAL { Name = OMNI; Policy = "$3"; FSAL { Name = VFS; } }
}
BLOCK
}
for s in alpha beta; do
	mkdir -p "$DATA/$s"
	echo "$s" >"$DATA/$s/$s.txt"
done
block 301 alpha wait >"$INC"

GANESHA_INCLUDE=$INC ganesha_start || exit 1
daemon_start || exit 1

lists() { # <v> <export path> <name>: the export serves and lists <name>
	local r
	r=$(nfsc "$1" "$2" ls / 2>&1)
	jtest "$r" "d.get('ok') and '$3' in d['names']"
}
path_of() { if [ "$1" = 3 ]; then echo "$DATA/$2"; else echo "/$2"; fi; }
hup() {
	kill -HUP "$(cat "$GPID_FILE")"
	sleep 2
}

for v in 3 4; do
	if lists $v "$(path_of $v alpha)" alpha.txt; then pass "v$v: the included export serves"; else fail "v$v: alpha not served"; fi
	if lists $v "$(path_of $v beta)" beta.txt; then fail "v$v: beta served before it was added"; else pass "v$v: an export not yet in the file isn't served"; fi
done

block 302 beta failfast >>"$INC"
hup
for v in 3 4; do
	ok=no
	for _ in $(seq 1 10); do lists $v "$(path_of $v beta)" beta.txt && { ok=yes; break; }; sleep 0.5; done
	if [ $ok = yes ]; then pass "v$v: an export added to the file is served after SIGHUP"; else fail "v$v: beta not served after SIGHUP"; fi
	if lists $v "$(path_of $v alpha)" alpha.txt; then pass "v$v: the export already there kept serving"; else fail "v$v: alpha lost on reload"; fi
done

block 302 beta failfast >"$INC"
hup
for v in 3 4; do
	gone=no
	for _ in $(seq 1 10); do lists $v "$(path_of $v alpha)" alpha.txt || { gone=yes; break; }; sleep 0.5; done
	if [ $gone = yes ]; then pass "v$v: an export removed from the file is gone after SIGHUP"; else fail "v$v: alpha still served"; fi
	if lists $v "$(path_of $v beta)" beta.txt; then pass "v$v: the other export still serves"; else fail "v$v: beta lost"; fi
done
if ganesha_alive; then pass "ganesha.nfsd stayed up through the reloads"; else fail "ganesha.nfsd died"; fi

daemon_stop
ganesha_stop
finish
