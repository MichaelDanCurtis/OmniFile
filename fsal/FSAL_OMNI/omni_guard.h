/* SPDX-License-Identifier: LGPL-3.0-or-later */
/*
 * FSAL_OMNI, part of OmniFile Gateway.
 * Copyright (C) 2026 The OmniFile Gateway authors.
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 3 of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, see
 * <https://www.gnu.org/licenses/>.
 */

/*
 * The stub guard: internal interfaces between the ops (file.c, handle.c),
 * the marker/inode layer (omni_guard.c) and the daemon connection with its
 * pending table and parked reads (omni_conn.c).
 *
 * Rules (docs/contracts/fsal-socket.md section 1):
 *  - no Ganesha worker thread ever waits on the daemon: workers only do
 *    non-blocking sends and in-memory lookups;
 *  - the marker is the only proof that bytes are local;
 *  - anything unexpected fails closed (EIO for failfast, DELAY otherwise).
 */

#ifndef OMNI_GUARD_H
#define OMNI_GUARD_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "omni_wire.h"

struct omni_fsal_export;
struct omni_fsal_obj_handle;

/* Value of fsal_io_arg.fsal_resume that makes the protocol layer call
 * read2 again on a worker thread once a parked read's range is local.
 */
#define OMNI_FSAL_RESUME 0x4F4D

/* Parked-read limits (contract 6.3). */
#define OMNI_PARK_MAX_PER_EXPORT 256

/* Token-bucket-free rate limiter: one log line per interval, counting the
 * lines it suppressed in between.
 */
struct omni_rl {
	uint64_t last_ms;
	uint32_t suppressed;
};
bool omni_rl_ok(struct omni_rl *rl, uint64_t interval_ms, uint32_t *dropped);

uint64_t omni_now_ms(void);

/* ---- per-inode state (omni_guard.c) ---------------------------------- */

struct omni_mount;

/* A chunk map (trusted.omnifile.map, contract 8), read and cached with its
 * marker. Immutable once read; shared by reference count.
 */
struct omni_map {
	uint32_t refs; /* atomic */
	uint32_t len;
	uint8_t bits[];
};
void omni_map_put(struct omni_map *map);

/* One per (fsid, ino, ino_gen) with a live OMNI handle; shared by all the
 * handles (and exports) that name the inode.
 */
struct omni_inode {
	struct omni_inode *next; /* hash chain */
	uint64_t fsid; /* wire fsid (statfs f_fsid as u64) */
	uint64_t ino;
	uint32_t gen;
	uint32_t refs; /* live OMNI handles; protected by the shard lock */

	struct omni_mount *mnt; /* NULL: handle could not be decoded */
	int kh_type;
	uint32_t kh_len;
	uint8_t kh[48]; /* kernel file handle bytes */

	/* MDCACHE key and up-call vector, for INVALIDATE */
	const struct fsal_up_vector *up;
	uint32_t keylen;
	uint8_t key[64];

	pthread_mutex_t mu;
	pthread_cond_t cv;
	/* I/O pins: reads and writes that passed the marker check hold a pin
	 * until the sub-FSAL call returns, so an acknowledged INVALIDATE
	 * (step 4 of the stub handshake) is sent only when no I/O that saw the
	 * old marker is still running.
	 */
	uint32_t pins;
	uint32_t inval_waiting;
	/* Bumped by every INVALIDATE of the inode (and by the FSAL's own
	 * marker removal). A disk read of the marker is cached only if this
	 * did not change while it ran, so a read that saw the marker before an
	 * INVALIDATE can never store it after the INVALIDATE dropped the cache,
	 * whether or not the reader held a pin.
	 */
	uint64_t inval_gen;
	bool trunc_busy; /* a truncate to 0 is between begin and end */
	/* marker cache, valid only while connected (cache_epoch == epoch) */
	bool cached;
	bool cached_managed;
	uint64_t cache_epoch;
	struct omni_marker m;
	struct omni_map *map; /* the cached marker's map, or NULL */
	/* last POPULATE_DIR sent for this directory, ms; 0: none pending */
	uint64_t populate_ms;
	/* A directory known to be outside every rewind tree (found from a
	 * live one, or the export's root): its marker is never read. Atomic.
	 */
	bool dir_live;
	/* last opener (contract 6.4) */
	struct omni_identity last_open;
	uint64_t last_open_ms;
	struct omni_rl rl_bad;
};

struct omni_inode *omni_inode_get(struct omni_fsal_export *exp,
				  struct fsal_obj_handle *sub_handle);
void omni_inode_put(struct omni_inode *ino);

void omni_pin(struct omni_inode *ino);
void omni_unpin(struct omni_inode *ino);

enum omni_mres {
	OMNI_M_NONE, /* no marker: an ordinary local file */
	OMNI_M_OK, /* managed: *m holds it */
	OMNI_M_BAD, /* unreadable or invalid: fail closed */
};

/* Read the marker: from the cache when allowed and valid, else from disk
 * (open_by_handle_at O_PATH + fgetxattr + fstat; never O_RDWR). A disk read
 * is cached only if no INVALIDATE of the inode began while it ran.
 *
 * A RESIDENT marker whose size differs from st_size (a committed, all-local
 * file appended or resized by a local writer) is returned as RESIDENT with
 * size and resident_bytes set to st_size: no byte of it can be remote. A
 * PARTIAL marker with a size mismatch is bad (fails closed).
 */
enum omni_mres omni_marker_get(struct omni_inode *ino, bool use_cache,
			       struct omni_marker *m, bool *from_cache,
			       uint32_t *bad_reason, int *bad_errno);
/* As omni_marker_get, also returning the marker's chunk map in *mapp (a
 * reference the caller drops with omni_map_put; NULL when there is none or
 * CHUNK_MAP was not negotiated). With CHUNK_MAP negotiated, a marker whose
 * map is missing or invalid is bad (reason 3): fail closed.
 */
enum omni_mres omni_marker_map_get(struct omni_inode *ino, bool use_cache,
				   struct omni_marker *m, struct omni_map **mapp,
				   bool *from_cache, uint32_t *bad_reason,
				   int *bad_errno);
/* Does the marker (and map) cover what op needs (contract 6.1)? */
bool omni_covered(const struct omni_marker *m, const struct omni_map *map,
		  uint8_t op, uint64_t off, uint64_t len);

void omni_obj_ref_of(struct omni_inode *ino, const struct omni_marker *m,
		     uint32_t export_id, struct omni_obj_ref *obj);

/* Identity of the current op context (origin REQUEST). */
void omni_identity_from_ctx(struct omni_identity *id);

/* Open-state table (contract 6.4). */
void omni_state_opened(struct state_t *state, struct omni_inode *ino,
		       fsal_openflags_t flags);
/* NFSv4 share states open for write on (fsid, ino, gen) (contract 5.5). */
uint32_t omni_open_writes(const struct omni_obj_ref *obj);
/* reopen2 of a state: an OPEN upgrade or downgrade (contract 5.5). */
void omni_state_reopened(struct state_t *state, fsal_openflags_t flags);
bool omni_state_closed(struct state_t *state, struct omni_identity *id,
		       fsal_openflags_t *flags);
/* Identity for an op that runs with a blank op context (COPY). */
void omni_identity_for_state(struct state_t *state, struct omni_inode *ino,
			     struct omni_identity *id);

/* The guard for one data operation. */
enum omni_gres {
	OMNI_G_PASS, /* go to the sub-FSAL, then omni_unpin(ino) */
	OMNI_G_ANSWER, /* *st is the answer */
	OMNI_G_PARKED, /* done_cb will be called later by the FSAL */
};

/* What a parked read needs to complete later. */
struct omni_park_req {
	struct fsal_obj_handle *obj;
	struct fsal_io_arg *read_arg;
	fsal_async_cb done_cb;
	void *caller_arg;
	/* The protocol's ASYNC_PROC_* flags of this READ (contract 6.3,
	 * "Resume only once the request waits"); NULL: may not park.
	 */
	uint32_t *async_flags;
};

/* The ASYNC_PROC_* flags word of the NFSv3 READ or NFSv4 READ/READ_PLUS
 * whose read2 is running with read_arg, under the request's op context;
 * NULL if the request is not provably one of those.
 */
uint32_t *omni_read_async_flags(struct fsal_io_arg *read_arg);

/* Test hook: defined only by the test shim (fsal/tests/slowxattr.c), which
 * a test copy of the module links; called as a parking read2 returns.
 */
extern void omni_test_parked_return(void) __attribute__((weak));

enum omni_gres omni_guard(struct omni_fsal_export *exp,
			  struct omni_fsal_obj_handle *h, uint8_t op,
			  uint64_t off, uint64_t len,
			  const struct omni_identity *id,
			  const struct omni_park_req *park, fsal_status_t *st);

/* Policy default for non-local data: EIO (failfast) or DELAY. */
fsal_status_t omni_policy_default(const struct omni_fsal_export *exp);
uint8_t omni_policy_default_dec(uint8_t policy);

/* Truncate to size 0 (contract 6.1, "Truncate to 0" and "Fill fence").
 *
 * omni_trunc0_begin serialises truncates of the inode, pins it and applies
 * the fill fence: it answers DELAY (and holds nothing) when a PARTIAL file
 * may be being filled, or when the marker cannot be read. Otherwise, if the
 * file has a marker, it holds back every other pin on the inode and removes
 * the marker BEFORE the caller truncates, so the smb-guard's fanotify group
 * sees an unmarked file and allows the truncate (a marked one would wait for
 * a fill). On NO_ERROR the caller truncates, then calls omni_trunc0_end with
 * the outcome: on success the daemon is told (NS_HINT); on failure the
 * marker is put back. omni_trunc0_end releases everything begin took.
 */
struct omni_trunc {
	struct omni_inode *ino;
	bool excl; /* other pins drained, new ones held back */
	bool unmarked; /* the marker was removed (restore it on failure) */
	ssize_t len;
	uint8_t marker[OMNI_MARKER_LEN + 1];
	struct omni_marker m;
};
fsal_status_t omni_trunc0_begin(struct omni_fsal_export *exp,
				struct omni_inode *ino, struct omni_trunc *t);
void omni_trunc0_end(struct omni_fsal_export *exp, struct omni_trunc *t,
		     bool truncated);
/* After an open-with-truncate the FSAL could not fence beforehand (a file
 * linked in after its lookup): drop the marker of a now-empty file.
 */
/* NS_HINT (contract 5.4): who made a namespace change over NFS, sent after
 * the sub-FSAL call succeeded. The daemon only attributes the change to the
 * caller (control-api 4.1 `actor`); its namespace sync decides what changed.
 * Never blocks: dropped when the daemon is away or its queue is full (the
 * change is then attributed to nobody).
 */
void omni_ns_hint(struct omni_fsal_export *exp, uint8_t op, uint8_t what,
		  struct omni_fsal_obj_handle *parent,
		  struct omni_fsal_obj_handle *target, const char *name,
		  const char *new_name);

void omni_after_truncate0(struct omni_fsal_export *exp,
			  struct omni_inode *ino);

/* Called by the connection for INVALIDATE; returns once no pinned I/O can
 * still be using the old marker.
 */
void omni_inval_apply(const struct omni_obj_ref *obj, uint32_t what);

/* CAP_SYS_ADMIN check (trusted.* xattrs). Logs loudly when missing. */
bool omni_cap_check(const char *when);
extern bool omni_cap_ok;

/* ---- daemon connection (omni_conn.c) -------------------------------- */

/* park_per_client: parked reads one client address may hold (M5,
 * OMNI { Park_Max_Per_Client }); 0 keeps the default, 16.
 */
void omni_conn_configure(const char *sock_path, uint32_t completion_threads,
			 uint32_t park_per_client);
void omni_conn_start(void);
void omni_conn_stop(void);
bool omni_conn_connected(void);
/* Marker caches are valid only for the current epoch, which changes on
 * every connect, disconnect and unmatched INVALIDATE.
 */
uint64_t omni_conn_epoch(void);
void omni_conn_bump_epoch(void);
uint32_t omni_conn_features(void);
/* Non-blocking send of a finished message; 0 or an errno. */
int omni_conn_send(struct omni_buf *b);
/* Send POPULATE_DIR for a rewind directory; 0, or ENOTCONN when there is
 * no daemon, ENOTSUP when it did not negotiate POPULATE_DIR, or a send
 * errno. The reply is never waited for.
 */
int omni_conn_populate(const struct omni_obj_ref *dir,
		       const struct omni_identity *id);

struct omni_ask {
	struct omni_fsal_export *exp;
	struct omni_obj_ref obj;
	struct omni_marker m;
	uint64_t off, len;
	uint8_t op;
	/* With CHUNK_MAP: the first chunk of the range that is not local,
	 * so each chunk a reader waits for is asked for once (contract 6.3);
	 * UINT64_MAX otherwise.
	 */
	uint64_t chunk;
	struct omni_identity id;
	const struct omni_park_req *park; /* NULL: may not park */
};

/* Steps 3-5 of contract 6.1 for a range the marker does not cover. */
enum omni_gres omni_decide(struct omni_ask *a, fsal_status_t *st);

/* Why a truncate to 0 of a PARTIAL file must wait, or NULL (contract 6.1
 * "Fill fence"): disconnected, resyncing, or a FETCHING entry.
 */
const char *omni_conn_fill_fence(const struct omni_obj_ref *obj,
				 uint64_t version_id);

/* Complete every parked read of an export (unexport). */
void omni_conn_unpark_export(struct omni_fsal_export *exp);

#endif /* OMNI_GUARD_H */
