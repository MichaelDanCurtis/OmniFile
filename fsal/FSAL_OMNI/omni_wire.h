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
 * The trusted.omnifile marker and the FSAL <-> daemon wire format, exactly
 * as docs/contracts/fsal-socket.md (sections 3-5 and 8) defines them.
 *
 * Pure C with no Ganesha dependency, so fsal/tests/unit builds it on its own
 * and checks it against the golden vectors the Rust side also asserts
 * (edge/crates/omnifile-marker).
 */

#ifndef OMNI_WIRE_H
#define OMNI_WIRE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ---- marker (contract section 8) ------------------------------------ */

#define OMNI_MARKER_XATTR "trusted.omnifile"
#define OMNI_MARKER_MAP_XATTR "trusted.omnifile.map"
#define OMNI_MARKER_LEN 64
#define OMNI_MARKER_MAGIC 0x31464D4Fu /* bytes 'O' 'M' 'F' '1' */
#define OMNI_MARKER_LAYOUT 1

enum omni_marker_state {
	OMNI_MST_RESIDENT = 1,
	OMNI_MST_PARTIAL = 2,
};

#define OMNI_MF_ONLINE 0x0001
#define OMNI_MF_ARCHIVED 0x0002
#define OMNI_MF_READONLY 0x0004
#define OMNI_MF_DIR_UNPOPULATED 0x0008
#define OMNI_MF_CHUNK_MAP 0x0010
#define OMNI_MF_WORM 0x0020

/* BAD_MARKER reasons (contract 5.4) */
enum omni_bad_marker {
	OMNI_BM_OK = 0,
	OMNI_BM_MAGIC = 1,
	OMNI_BM_CRC = 2,
	OMNI_BM_LAYOUT = 3,
	OMNI_BM_SIZE = 4,
	OMNI_BM_READ = 5,
};

struct omni_marker {
	uint8_t layout;
	uint8_t state;
	uint16_t flags;
	uint64_t file_id;
	uint64_t version_id;
	uint64_t size;
	uint64_t resident_bytes;
	uint64_t marker_seq;
	uint64_t share_id;
	uint8_t chunk_shift;
};

uint32_t omni_crc32c(const void *data, size_t len);

/* Returns OMNI_BM_OK, or the BAD_MARKER reason the value fails with. */
enum omni_bad_marker omni_marker_decode(const void *buf, size_t len,
					struct omni_marker *m);
void omni_marker_encode(const struct omni_marker *m,
			uint8_t out[OMNI_MARKER_LEN]);

/* ---- chunk map: trusted.omnifile.map (contract 8, M5) ------------------ */

/* The largest map an xattr value can hold (xfs: 64 KiB). */
#define OMNI_MAP_MAX 65536
/* Chunk size the FSAL assumes for asking, when a marker has no map yet. */
#define OMNI_CHUNK_SHIFT_DEFAULT 22
/* The chunk shift of a file without a map yet: `preferred`, raised until its
 * map fits OMNI_MAP_MAX bytes (the daemon's chunks::chunk_shift_for). 3 TiB
 * -> 23 (49,152-byte map); 2 TiB -> 22 (65,536). A file with a map always
 * uses the map's shift.
 */
uint8_t omni_chunk_shift_for(uint64_t size, uint8_t preferred);

/* Bytes the map of this marker must have: ceil(ceil(size / chunk) / 8).
 * 0 if the marker has no CHUNK_MAP flag, or if that many bytes cannot be an
 * xattr value (the map is then invalid).
 */
size_t omni_map_len(const struct omni_marker *m);
/* A map value is valid for the marker if it has exactly omni_map_len bytes
 * and no bit set past the last chunk.
 */
bool omni_map_valid(const struct omni_marker *m, const uint8_t *map,
		    size_t len);
/* M5, CHUNK_MAP: the chunks a pending entry asked for recently, so each
 * chunk a reader waits for costs one READ_INTENT (contract 6.3). Four
 * slots live in the entry; one that sees more distinct chunks within the
 * TTL grows to OMNI_ASKED_BIG slots, while fewer than *nbig_max entries have
 * grown (a global cap: 10^6 entries never cost 4 KiB each). Past the cap a
 * ring wraps, which only costs a repeated READ_INTENT.
 */
#define OMNI_ASKED_INLINE 4
#define OMNI_ASKED_BIG 256

struct omni_asked {
	uint64_t chunk;
	uint64_t ms;
};

struct omni_asked_set {
	struct omni_asked in[OMNI_ASKED_INLINE];
	struct omni_asked *big; /* OMNI_ASKED_BIG slots, or NULL */
	uint32_t next;
};

/* True if `chunk` was not asked within `ttl` ms (and records it). `nbig`
 * counts grown sets (updated atomically).
 */
bool omni_asked_new(struct omni_asked_set *s, uint64_t chunk, uint64_t now,
		    uint64_t ttl, uint32_t *nbig, uint32_t nbig_max);
void omni_asked_free(struct omni_asked_set *s, uint32_t *nbig);

/* Is every byte of [off, end) local: below resident_bytes, or in a chunk
 * whose bit is set? map may be NULL (prefix only). end <= size.
 */
bool omni_range_local(const struct omni_marker *m, const uint8_t *map,
		      size_t maplen, uint64_t off, uint64_t end);

/* ---- wire (contract sections 2-5) ----------------------------------- */

#define OMNI_MAX_MSG 4096
#define OMNI_HDR_LEN 16
#define OMNI_PROTO_MAJOR 0
/* v0.2 (M5): CHUNK_MAP is used, and only with a peer that acks minor >= 2.
 * POPULATE_DIR stays usable at minor 1 (A's M5 rewind trees).
 */
#define OMNI_PROTO_MINOR 2
#define OMNI_HELLO_MAGIC 0x53464D4Fu /* bytes 'O' 'M' 'F' 'S' */
#define OMNI_REPLY_BIT 0x8000
#define OMNI_FLAG_TRUNCATED 0x0001
#define OMNI_DAEMON_REQ_BIT (1ULL << 63)
#define OMNI_PATH_HINT_MAX 1024

enum omni_msg_type {
	OMNI_HELLO = 0x0001,
	OMNI_HELLO_ACK = 0x8001,
	OMNI_READ_INTENT = 0x0010,
	OMNI_READ_DECISION = 0x8010,
	OMNI_POPULATE_DIR = 0x0011,
	OMNI_POPULATE_RESULT = 0x8011,
	OMNI_WRITE_CLOSED = 0x0020,
	OMNI_NS_HINT = 0x0021,
	OMNI_BAD_MARKER = 0x0022,
	OMNI_PING = 0x0030,
	OMNI_PONG = 0x8030,
	OMNI_RECALL_STATE = 0x0040,
	OMNI_INVALIDATE = 0x0041,
	OMNI_INVALIDATE_ACK = 0x8041,
	OMNI_RESYNC_BEGIN = 0x0042,
	OMNI_RESYNC_END = 0x0043,
	OMNI_OPEN_STATE_QUERY = 0x0044,
	OMNI_OPEN_STATE = 0x8044,
	OMNI_ERROR = 0x7FFF,
};

/* HELLO feature bits */
#define OMNI_FEAT_PARK 0x01
#define OMNI_FEAT_CHUNK_MAP 0x02
#define OMNI_FEAT_POPULATE_DIR 0x04
#define OMNI_FEAT_NS_HINT 0x08
#define OMNI_FEAT_WRITE_CLOSED 0x10
#define OMNI_FEAT_OPEN_STATE 0x20

/* READ_INTENT op */
enum omni_op {
	OMNI_OP_READ = 1,
	OMNI_OP_READ_PLUS = 2,
	OMNI_OP_COPY_SRC = 3,
	OMNI_OP_WRITE = 4,
	OMNI_OP_SETATTR_SIZE = 5,
	OMNI_OP_FALLOCATE = 6,
};

/* export policy byte */
enum omni_policy {
	OMNI_POL_WAIT = 1,
	OMNI_POL_FAILFAST = 2,
	OMNI_POL_WAITQUICK = 3,
};

/* READ_DECISION.decision and RECALL_STATE.answer */
enum omni_decision {
	OMNI_DEC_RECHECK = 1,
	OMNI_DEC_DELAY = 2,
	OMNI_DEC_EIO = 3,
	OMNI_DEC_PARK = 4,
};

enum omni_recall_state {
	OMNI_RS_QUEUED = 1,
	OMNI_RS_RESTORING = 2,
	OMNI_RS_COPYING = 3,
	OMNI_RS_FETCHING = 4,
	OMNI_RS_DONE = 5,
	OMNI_RS_FAILED = 6,
	OMNI_RS_CANCELLED = 7,
	OMNI_RS_BLOCKED = 8,
};

/* POPULATE_RESULT.result */
enum omni_populate {
	OMNI_POP_DONE = 1,
	OMNI_POP_IN_PROGRESS = 2,
	OMNI_POP_EIO = 3,
};

/* identity.origin */
enum omni_origin {
	OMNI_ORIGIN_REQUEST = 0,
	OMNI_ORIGIN_OPEN_STATEID = 1,
	OMNI_ORIGIN_LAST_OPENER = 2,
	OMNI_ORIGIN_UNKNOWN = 3,
};

/* identity.sec */
enum omni_sec {
	OMNI_SEC_UNKNOWN = 0,
	OMNI_SEC_SYS = 1,
	OMNI_SEC_KRB5 = 2,
	OMNI_SEC_KRB5I = 3,
	OMNI_SEC_KRB5P = 4,
};

/* NS_HINT op (contract 5.4) */
enum omni_ns_op {
	OMNI_NS_CREATE = 1,
	OMNI_NS_UNLINK = 2,
	OMNI_NS_RENAME = 3,
	OMNI_NS_SETATTR = 4,
	OMNI_NS_SETACL = 5,
	OMNI_NS_LINK = 6, /* reserved: LINK is refused (decision 42) */
	OMNI_NS_MKDIR = 7,
	OMNI_NS_RMDIR = 8,
};

/* NS_HINT `what` (SETATTR only; the first reserved byte, contract 5.4) */
#define OMNI_NS_WHAT_SIZE 0x01 /* a truncate to 0 */
#define OMNI_NS_WHAT_OWNER 0x02 /* the owner or group changed */

/* ERROR codes */
enum omni_err {
	OMNI_ERR_VERSION = 1,
	OMNI_ERR_UNKNOWN_REQUEST = 2,
	OMNI_ERR_MALFORMED = 3,
	OMNI_ERR_PEER = 4,
	OMNI_ERR_SHUTDOWN = 5,
};

/* INVALIDATE.what / .flags */
#define OMNI_INV_MARKER 0x1
#define OMNI_INV_ATTRS 0x2
#define OMNI_INV_CONTENT 0x4
#define OMNI_INV_DIR 0x8
#define OMNI_INV_ACK_REQUIRED 0x1

#define OMNI_UID_UNKNOWN 0xFFFFFFFFu
#define OMNI_ETA_UNKNOWN 0xFFFFFFFFu

/* Fixed part sizes, in bytes after the header (contract section 5). */
#define OMNI_FIX_HELLO 32
#define OMNI_FIX_HELLO_ACK 32
#define OMNI_FIX_READ_INTENT 136
#define OMNI_FIX_READ_DECISION 40
#define OMNI_FIX_POPULATE_DIR 80
#define OMNI_FIX_POPULATE_RESULT 8
#define OMNI_FIX_WRITE_CLOSED 88
#define OMNI_FIX_NS_HINT 120
#define OMNI_FIX_BAD_MARKER 40
#define OMNI_FIX_PING 8
#define OMNI_FIX_RECALL_STATE 72
#define OMNI_FIX_INVALIDATE 48
#define OMNI_FIX_INVALIDATE_ACK 8
#define OMNI_FIX_OPEN_STATE_QUERY 32
#define OMNI_FIX_OPEN_STATE 8
#define OMNI_FIX_ERROR 4
#define OMNI_FIX_RESYNC 0

struct omni_obj_ref {
	uint64_t fsid;
	uint64_t ino;
	uint32_t ino_gen;
	uint32_t export_id;
	uint64_t file_id;
};

struct omni_identity {
	uint32_t uid;
	uint32_t gid;
	uint8_t origin;
	uint8_t nfs_vers;
	uint8_t nfs_minor;
	uint8_t sec;
	uint8_t addr_family; /* 0, 4 or 6 */
	uint8_t addr[16];
	uint16_t port;
	uint64_t clientid;
	/* Not part of the 48-byte wire struct: sent as the message's
	 * principal string. Empty with sec=sys.
	 */
	char principal[256];
};

struct omni_hdr {
	uint16_t type;
	uint16_t flags;
	uint32_t length;
	uint64_t req_id;
};

/* A message being built. Never grows past OMNI_MAX_MSG. */
struct omni_buf {
	uint8_t data[OMNI_MAX_MSG];
	size_t len;
	bool overflow;
	bool truncated;
};

void omni_buf_begin(struct omni_buf *b, uint16_t type, uint64_t req_id);
void omni_put_u8(struct omni_buf *b, uint8_t v);
void omni_put_u16(struct omni_buf *b, uint16_t v);
void omni_put_u32(struct omni_buf *b, uint32_t v);
void omni_put_u64(struct omni_buf *b, uint64_t v);
void omni_put_zero(struct omni_buf *b, size_t n);
void omni_put_obj(struct omni_buf *b, const struct omni_obj_ref *o);
void omni_put_ident(struct omni_buf *b, const struct omni_identity *id);
/* A string: u16 length, then bytes. Cut to max bytes, from the left when
 * cut_left (paths), and set TRUNCATED.
 */
void omni_put_str(struct omni_buf *b, const char *s, size_t len, size_t max,
		  bool cut_left);
/* Patch length and flags into the header. Returns false on overflow. */
bool omni_buf_end(struct omni_buf *b);

/* Builders for every message the FSAL sends. */
bool omni_build_hello(struct omni_buf *b, uint64_t req_id, uint32_t features,
		      uint32_t pid, uint64_t instance, const char *gversion,
		      const char *fsal_build);
bool omni_build_read_intent(struct omni_buf *b, uint64_t req_id,
			    const struct omni_obj_ref *obj, uint64_t version_id,
			    uint64_t offset, uint64_t length, uint64_t size,
			    uint64_t resident, uint8_t op, uint8_t policy,
			    uint16_t marker_flags, uint32_t quick_ms,
			    uint8_t parked, const struct omni_identity *id,
			    const char *principal, const char *path_hint);
bool omni_build_populate_dir(struct omni_buf *b, uint64_t req_id,
			     const struct omni_obj_ref *dir,
			     const struct omni_identity *id,
			     const char *principal, const char *path_hint);
bool omni_build_bad_marker(struct omni_buf *b, const struct omni_obj_ref *obj,
			   uint32_t reason, uint32_t err);
bool omni_build_write_closed(struct omni_buf *b,
			     const struct omni_obj_ref *obj,
			     uint64_t size_after,
			     const struct omni_identity *id);
bool omni_build_ns_hint(struct omni_buf *b, uint8_t op, uint8_t what,
			const struct omni_obj_ref *parent,
			const struct omni_obj_ref *target,
			const struct omni_identity *id, const char *principal,
			const char *name, const char *new_name);
bool omni_build_ping(struct omni_buf *b, uint16_t type, uint64_t req_id,
		     uint64_t mono_ns);
bool omni_build_open_state(struct omni_buf *b, uint64_t req_id,
			   uint32_t write_opens);
bool omni_build_inval_ack(struct omni_buf *b, uint64_t req_id,
			  uint64_t inval_seq);
bool omni_build_error(struct omni_buf *b, uint32_t code, const char *msg);

/* A parsed message from the daemon. */
struct omni_msg {
	struct omni_hdr hdr;
	bool known; /* type recognised */
	union {
		struct {
			uint16_t proto_major, proto_minor;
			uint32_t features;
			uint64_t instance;
			uint32_t max_inflight, park_ms_max, answer_ttl_ms;
		} hello_ack;
		struct {
			uint8_t decision, reason;
			uint32_t eta_s, ttl_ms;
			uint64_t recall_id, resident_bytes;
		} decision;
		struct {
			struct omni_obj_ref obj;
			uint64_t version_id, recall_id;
			uint8_t state, answer;
			uint32_t eta_s;
			uint64_t resident_bytes;
			uint32_t valid_ms;
		} recall;
		struct {
			struct omni_obj_ref obj;
			uint32_t what, flags;
			uint64_t inval_seq;
		} inval;
		struct {
			struct omni_obj_ref obj;
		} open_q;
		struct {
			uint8_t result; /* enum omni_populate */
			uint32_t eta_ms;
		} populate;
		struct {
			uint64_t mono_ns;
		} ping;
		struct {
			uint32_t code;
			char message[256];
		} error;
	} u;
};

/* Parse one received record. Returns 0, or -1 if it is malformed (bad
 * length, short fixed part for a known type, bad string). An unknown type
 * parses with known = false.
 */
int omni_parse(const uint8_t *p, size_t n, struct omni_msg *m);

/* Little-endian helpers, also used by the FSAL for the handle decode. */
static inline uint16_t omni_ld16(const uint8_t *p)
{
	return (uint16_t)(p[0] | (p[1] << 8));
}

static inline uint32_t omni_ld32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
	       ((uint32_t)p[3] << 24);
}

static inline uint64_t omni_ld64(const uint8_t *p)
{
	return (uint64_t)omni_ld32(p) | ((uint64_t)omni_ld32(p + 4) << 32);
}

#endif /* OMNI_WIRE_H */
