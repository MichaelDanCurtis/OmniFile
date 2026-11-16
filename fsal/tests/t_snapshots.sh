#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Rewind trees over NFS (docs/design/fsal-namespace.md section 7), with the
# userspace client fsal/tests/nfsc.py (libnfs: no kernel client needed, and
# it never retries, so every DELAY/JUKEBOX the server gives is seen):
# - .snapshots in a share's root is the share's area under .omni/snap, and
#   READDIR of the root does not list it;
# - a directory the daemon has not filled answers DELAY and sends
#   POPULATE_DIR, never an empty listing; once filled it lists and reads;
# - every change below .snapshots is EROFS, and so is creating .snapshots;
# - without a daemon that fills directories, the share's policy answers
#   (wait: DELAY, failfast: EIO).
. "$(dirname "$0")/lib.sh"
setup_volumes

SNAP=$DATA/.omni/snap
nfsc() { python3 "$T/nfsc.py" "$@"; }
exp_of() { # <3|4> <share>
	if [ "$1" = 3 ]; then echo "$DATA/$2"; else echo "/omni/$2"; fi
}
# rwdir <dir> <flags>: a rewind directory as the daemon makes it
rwdir() {
	mkdir -p "$1"
	chmod 0555 "$1"
	python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import omni_proto as P
P.write_marker(sys.argv[2], P.marker(5, 0, 0, 0, flags=int(sys.argv[3]), seq=1))' "$T" "$1" "$2"
}
RO=4 UNPOP=12 # READONLY; READONLY | DIR_UNPOPULATED

rm -rf "$DATA/.omni"
mkdir -p "$SNAP"
chmod 0711 "$SNAP"
for s in wait failfast; do
	rwdir "$SNAP/$s" $RO
	for l in 2026-09-01T0000Z 2026-09-02T0000Z 2026-09-03T0000Z; do rwdir "$SNAP/$s/$l" $UNPOP; done
done
echo live >"$DATA/wait/live.txt"

ganesha_start || exit 1
daemon_start --populate-delay 2

for v in 3 4; do
	E=$(exp_of $v wait)
	L=2026-09-0$((v - 2))T0000Z # one label per version: each starts unfilled

	r=$(nfsc $v "$E" ls /)
	if jtest "$r" 'd.get("ok") and "live.txt" in d["names"] and ".snapshots" not in d["names"]'; then
		pass "v$v: READDIR of the share root does not list .snapshots"
	else
		fail "v$v: share root listing: $r"
	fi
	r=$(nfsc $v "$E" ls /.snapshots)
	if jtest "$r" 'd.get("names") == ["2026-09-01T0000Z", "2026-09-02T0000Z", "2026-09-03T0000Z"]'; then
		pass "v$v: .snapshots lists the share's labels"
	else
		fail "v$v: ls .snapshots: $r"
	fi

	# An unfilled label: DELAY (never "no such file", never an empty
	# listing) and POPULATE_DIR, then the entries once the daemon has
	# filled it (after --populate-delay).
	t0=$(now)
	look=$(nfsc $v "$E" cat "/.snapshots/$L/v.txt")
	first=$(nfsc $v "$E" ls "/.snapshots/$L")
	ok=""
	for _ in $(seq 1 40); do
		r=$(nfsc $v "$E" ls "/.snapshots/$L")
		jtest "$r" 'd.get("ok")' && { ok=1; break; }
		sleep 0.25
	done
	ino=$(stat -c %i "$SNAP/wait/$L")
	n=$(daemon_count "$t0" 'm.get("type") == "POPULATE_DIR" and m["obj"]["ino"] == '"$ino")
	if jtest "$look" 'd.get("name") == "EAGAIN"' &&
		jtest "$first" 'd.get("name") == "EAGAIN"' && [ -n "$ok" ] &&
		jtest "$r" 'd.get("names") == ["d", "v.txt"]' && [ "$n" -ge 1 ]; then
		pass "v$v: LOOKUP and READDIR in an unfilled directory answer DELAY and send POPULATE_DIR ($n sent, rate-limited); then it lists"
	else
		fail "v$v: unfilled directory: lookup $look, first readdir $first, last $r, POPULATE_DIR sent $n"
	fi
	r=$(nfsc $v "$E" cat "/.snapshots/$L/v.txt")
	if jtest "$r" 'd.get("data") == "as of .omni/snap/wait/'"$L"'\n"'; then
		pass "v$v: a file in a filled rewind directory reads"
	else
		fail "v$v: read in .snapshots: $r"
	fi
	first=$(nfsc $v "$E" ls "/.snapshots/$L/d")
	if jtest "$first" 'not d.get("ok") and d.get("name") == "EAGAIN"'; then
		pass "v$v: its unfilled subdirectory answers DELAY too"
	else
		fail "v$v: unfilled subdirectory: $first"
	fi

	# Nothing below .snapshots changes, whatever the permission bits say
	# (the client is root, not squashed).
	bad=""
	for op in "creat /.snapshots/$L/new" "creat /.snapshots/$L/v.txt" "mkdir /.snapshots/$L/new" \
		"unlink /.snapshots/$L/v.txt" "rename /.snapshots/$L/v.txt /.snapshots/$L/w.txt" \
		"chmod /.snapshots/$L/v.txt 644" "chmod /.snapshots/$L 755" \
		"trunc /.snapshots/$L/v.txt" "write /.snapshots/$L/v.txt x" \
		"mkdir /.snapshots/new" "rmdir /.snapshots/$L" \
		"rename /live.txt /.snapshots/$L/live.txt" "mkdir /.snapshots" \
		"rmdir /.snapshots"; do
		# shellcheck disable=SC2086
		r=$(nfsc $v "$E" $op)
		# libnfs 5.0.2 reports a failed NFSv3 WRITE as EFAULT, with the
		# server's errno as the message's one character (30: EROFS).
		# Creating an existing name: NFSv4 clients create GUARDED, and
		# EEXIST changes nothing either (the content is checked below).
		jtest "$r" 'd.get("name") == "EROFS" or (not d.get("ok") and d.get("error", "").endswith(chr(30) + chr(34))) or (d.get("name") == "EEXIST" and "'"$op"'".endswith("/v.txt"))' ||
			bad="$bad [$op: $r]"
	done
	if [ -z "$bad" ] && [ "$(cat "$SNAP/wait/$L/v.txt")" = "as of .omni/snap/wait/$L" ] &&
		[ -f "$DATA/wait/live.txt" ] && [ ! -e "$SNAP/wait/$L/new" ]; then
		pass "v$v: create, mkdir, unlink, rename, chmod, truncate and write below .snapshots, and creating .snapshots, are EROFS"
	else
		fail "v$v: changes below .snapshots:$bad"
	fi

	# The live share is untouched by all this.
	r1=$(nfsc $v "$E" creat /new$v.txt)
	r2=$(nfsc $v "$E" mkdir /dir$v)
	r3=$(nfsc $v "$E" rename /new$v.txt /dir$v/new.txt)
	r4=$(nfsc $v "$E" unlink /dir$v/new.txt)
	r5=$(nfsc $v "$E" rmdir /dir$v)
	if jtest "$r1" 'd.get("ok")' && jtest "$r2" 'd.get("ok")' && jtest "$r3" 'd.get("ok")' &&
		jtest "$r4" 'd.get("ok")' && jtest "$r5" 'd.get("ok")'; then
		pass "v$v: the live share still creates, renames and removes"
	else
		fail "v$v: live share: $r1 $r2 $r3 $r4 $r5"
	fi
done

# A daemon that does not fill directories: the share's policy answers.
daemon_start --no-populate
for v in 3 4; do
	rw=$(nfsc $v "$(exp_of $v wait)" ls "/.snapshots/2026-09-03T0000Z")
	rf=$(nfsc $v "$(exp_of $v failfast)" ls "/.snapshots/2026-09-03T0000Z")
	if jtest "$rw" 'd.get("name") == "EAGAIN"' && jtest "$rf" 'd.get("name") == "EIO"'; then
		pass "v$v: without POPULATE_DIR, an unfilled directory is DELAY on a wait share and EIO on a failfast one"
	else
		fail "v$v: without POPULATE_DIR: wait $rw, failfast $rf"
	fi
done
finish
