#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# A parked read resumed again while the read2 that re-parked it is still
# running (contract 6.3, "Resume only once the request waits"). A resumed read
# re-checks the marker; if the range is still missing (after a RECHECK, or a
# RECALL_STATE watermark ahead of the marker) it parks again. If the next
# resume completes it before Ganesha's resume handler (V15.6
# nfs3_read_resume, nfs4_op_read_resume) has returned from that read2 and set
# ASYNC_PROC_EXIT, the handler completes the READ with the callback's status
# and never looks at fsal_resume. A resume delivered as success then went
# out as a 0-byte READ without EOF, which the Linux client reports as EIO. A
# resume must never turn into an error or a short read.
#
# Deterministic: the slowxattr shim, linked into a copy of the module, holds
# every second READ_INTENT for 400 ms (with a 1 ms answer TTL, re-parks ask
# again, so this keeps a re-parked read inside read2), while the fake daemon
# pushes FETCHING every 20 ms with resident_bytes = the whole file and the
# marker left at 4 MiB (each push resumes the parked read, which re-parks).
# The fill lands after 3 s and its DONE serves the read.
. "$(dirname "$0")/lib.sh"
setup_volumes
build_slow_module || { fail "cannot build the module with the slowxattr shim"; finish; }
slow_marker off
slow_intent off
# park_ms 20 s (the daemon allows up to 30 s): a fill slowed by a loaded host
# must not end a park with its legitimate DELAY, which the userspace client
# below would count as a failure.
PARK_MS=20000 PLUGINS_DIR=$SLOW_PLUGINS ganesha_start || exit 1
daemon_start --fetch-delay 3 --answer-ttl-ms 1 --park-ms-max 30000 || exit 1
fid=9600
for v in 3 4.0 4.1 4.2; do
	fid=$((fid + 1))
	f=$DATA/park/rr$fid.segy
	mkstub "$f" $fid $SIZE $RES --online
	mount_v "$v" park "$MNT/p" || { fail "v$v mount"; continue; }
	nres=$(grep -c 'parked read completes: resume' "$LOG")
	t=$(now)
	slow_intent 400
	daemon_ctl "{\"cmd\": \"push\", \"path\": \"$f\", \"n\": 150, \"every_ms\": 20}" >/dev/null
	r=$(timeout 60 python3 "$T/reader.py" one "$MNT/p/rr$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)) --direct)
	slow_intent off
	sleep 0.5 # the daemon logs on its worker
	ni=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
	nres=$(($(grep -c 'parked read completes: resume' "$LOG") - nres))
	if jtest "$r" 'd.get("ok") and d["n"] == 1048576' && [ "$ni" -ge 4 ] && [ "$nres" -ge 6 ]; then
		pass "v$v: a read re-parked and resumed under a push every 20 ms, with the re-park's READ_INTENT held: correct bytes, no EIO, no short read ($nres resumes, $ni READ_INTENTs, $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s)"
	else
		fail "v$v: resume race: $r resumes=$nres intents=$ni"
	fi
	umount -f -l "$MNT/p"
	rm -f "$f"
done

# The worker descheduled between read2's return and the protocol marking
# the request as waiting (ASYNC_PROC_EXIT): the shim's hook sleeps 50 ms as
# every parking read2 returns. Pushes come in pairs 30 ms apart, every
# 300 ms: the first resumes the waiting read into Ganesha's resume handler,
# whose read2 parks again and sleeps in the hook; the second lands in that
# window. A resume delivered there would be completed inline by the resume
# handler (a DELAY, or before the fix a 0-byte READ); a timer after read2's
# return (the first version of this port, 10 ms) does deliver it there. A
# resume now waits for ASYNC_PROC_EXIT itself, so it is held however long
# the window. The userspace client never retries, so any DELAY, error or
# short read fails the check.
slow_park_return 50
for v in 3 4; do
	fid=$((fid + 1))
	f=$DATA/park/rd$fid.segy
	mkstub "$f" $fid $SIZE $RES --online
	if [ $v = 3 ]; then E=$DATA/park; else E=/omni/park; fi
	want=$(sha256sum "$BACKEND/$fid" | cut -d' ' -f1)
	nh=$(grep -c 'resume (held until the request waited)' "$LOG")
	daemon_ctl "{\"cmd\": \"push\", \"path\": \"$f\", \"n\": 10, \"every_ms\": 300, \"gap_ms\": 30}" >/dev/null
	r=$(timeout 60 python3 "$T/nfsc.py" $v "$E" sum "/rd$fid.segy")
	nh=$(($(grep -c 'resume (held until the request waited)' "$LOG") - nh))
	if jtest "$r" "d.get('ok') and d['bytes'] == $SIZE and d['sha256'] == '$want'" && [ "$nh" -ge 1 ]; then
		pass "v$v (userspace client, no retries): parking read2 descheduled 50 ms before the request waits, a second push inside that window: every byte correct, no DELAY, no short read ($nh resumes held until the request waited)"
	else
		fail "v$v: resume in the descheduled window: $r held=$nh; other completions: $(grep 'parked read completes:' "$LOG" | grep -v resume | sort | uniq -c | tr '\n' ' ')"
	fi
	rm -f "$f"
done
slow_park_return off
ganesha_alive || fail "ganesha.nfsd died"
finish
