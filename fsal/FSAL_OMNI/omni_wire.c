// SPDX-License-Identifier: LGPL-3.0-or-later
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

/* Marker codec and wire builders/parser (docs/contracts/fsal-socket.md). */

#include <stdlib.h>
#include <string.h>

#include "omni_wire.h"

/* ---- CRC-32C (Castagnoli, reflected 0x82F63B78) ---------------------- */

static uint32_t crc_table[256];
static int crc_ready;

static void crc_init(void)
{
	uint32_t i, j, c;

	for (i = 0; i < 256; i++) {
		c = i;
		for (j = 0; j < 8; j++)
			c = (c & 1) ? (c >> 1) ^ 0x82F63B78u : c >> 1;
		crc_table[i] = c;
	}
	__atomic_store_n(&crc_ready, 1, __ATOMIC_RELEASE);
}

uint32_t omni_crc32c(const void *data, size_t len)
{
	const uint8_t *p = data;
	uint32_t c = 0xFFFFFFFFu;

	/* Building the table twice from two threads is harmless. */
	if (!__atomic_load_n(&crc_ready, __ATOMIC_ACQUIRE))
		crc_init();
	while (len--)
		c = crc_table[(c ^ *p++) & 0xFF] ^ (c >> 8);
	return c ^ 0xFFFFFFFFu;
}

/* ---- little-endian stores -------------------------------------------- */

static void st16(uint8_t *p, uint16_t v)
{
	p[0] = v & 0xFF;
	p[1] = v >> 8;
}

static void st32(uint8_t *p, uint32_t v)
{
	st16(p, v & 0xFFFF);
	st16(p + 2, v >> 16);
}

static void st64(uint8_t *p, uint64_t v)
{
	st32(p, (uint32_t)v);
	st32(p + 4, (uint32_t)(v >> 32));
}

/* ---- marker ----------------------------------------------------------- */

enum omni_bad_marker omni_marker_decode(const void *buf, size_t len,
					struct omni_marker *m)
{
	const uint8_t *p = buf;

	memset(m, 0, sizeof(*m));
	if (len != OMNI_MARKER_LEN)
		return OMNI_BM_LAYOUT;
	if (omni_ld32(p) != OMNI_MARKER_MAGIC)
		return OMNI_BM_MAGIC;
	if (omni_crc32c(p, 60) != omni_ld32(p + 60))
		return OMNI_BM_CRC;
	m->layout = p[4];
	m->state = p[5];
	m->flags = omni_ld16(p + 6);
	m->file_id = omni_ld64(p + 8);
	m->version_id = omni_ld64(p + 16);
	m->size = omni_ld64(p + 24);
	m->resident_bytes = omni_ld64(p + 32);
	m->marker_seq = omni_ld64(p + 40);
	m->share_id = omni_ld64(p + 48);
	m->chunk_shift = p[56];
	if (m->layout != OMNI_MARKER_LAYOUT)
		return OMNI_BM_LAYOUT;
	if (m->state != OMNI_MST_RESIDENT && m->state != OMNI_MST_PARTIAL)
		return OMNI_BM_LAYOUT;
	if (m->resident_bytes > m->size)
		return OMNI_BM_LAYOUT;
	if (m->state == OMNI_MST_RESIDENT && m->resident_bytes != m->size)
		return OMNI_BM_LAYOUT;
	if (m->flags & OMNI_MF_CHUNK_MAP) {
		if (m->chunk_shift < 12 || m->chunk_shift > 40)
			return OMNI_BM_LAYOUT;
	} else if (m->chunk_shift != 0) {
		return OMNI_BM_LAYOUT;
	}
	return OMNI_BM_OK;
}

void omni_marker_encode(const struct omni_marker *m,
			uint8_t out[OMNI_MARKER_LEN])
{
	memset(out, 0, OMNI_MARKER_LEN);
	st32(out, OMNI_MARKER_MAGIC);
	out[4] = m->layout;
	out[5] = m->state;
	st16(out + 6, m->flags);
	st64(out + 8, m->file_id);
	st64(out + 16, m->version_id);
	st64(out + 24, m->size);
	st64(out + 32, m->resident_bytes);
	st64(out + 40, m->marker_seq);
	st64(out + 48, m->share_id);
	out[56] = m->chunk_shift;
	st32(out + 60, omni_crc32c(out, 60));
}

/* ---- chunk map (contract 8, M5) ---------------------------------------- */

size_t omni_map_len(const struct omni_marker *m)
{
	uint64_t chunks, bytes;

	if (!(m->flags & OMNI_MF_CHUNK_MAP) || m->chunk_shift < 12 ||
	    m->chunk_shift > 40)
		return 0;
	chunks = (m->size >> m->chunk_shift) +
		 ((m->size & ((1ULL << m->chunk_shift) - 1)) != 0);
	bytes = (chunks + 7) / 8;
	if (bytes == 0 || bytes > OMNI_MAP_MAX)
		return 0;
	return (size_t)bytes;
}

bool omni_map_valid(const struct omni_marker *m, const uint8_t *map,
		    size_t len)
{
	size_t want = omni_map_len(m);
	uint64_t chunks;
	unsigned int used;

	if (want == 0 || map == NULL || len != want)
		return false;
	chunks = (m->size >> m->chunk_shift) +
		 ((m->size & ((1ULL << m->chunk_shift) - 1)) != 0);
	used = (unsigned int)(chunks % 8);
	/* bits past the last chunk must be zero */
	if (used != 0 && (map[len - 1] >> used) != 0)
		return false;
	return true;
}

bool omni_range_local(const struct omni_marker *m, const uint8_t *map,
		      size_t maplen, uint64_t off, uint64_t end)
{
	uint64_t c, last;

	if (end <= off || end <= m->resident_bytes)
		return true;
	if (map == NULL || maplen == 0 || !(m->flags & OMNI_MF_CHUNK_MAP))
		return false;
	/* below the prefix watermark everything is local */
	if (off < m->resident_bytes)
		off = m->resident_bytes;
	c = off >> m->chunk_shift;
	last = (end - 1) >> m->chunk_shift;
	if ((last >> 3) >= maplen)
		return false;
	while (c <= last) {
		/* whole bytes at a time where possible */
		if ((c & 7) == 0 && c + 7 <= last) {
			if (map[c >> 3] != 0xFF)
				return false;
			c += 8;
			continue;
		}
		if (!(map[c >> 3] & (1u << (c & 7))))
			return false;
		c++;
	}
	return true;
}

/* ---- builder ---------------------------------------------------------- */

static uint8_t *room(struct omni_buf *b, size_t n)
{
	uint8_t *p;

	if (b->overflow || b->len + n > OMNI_MAX_MSG) {
		b->overflow = true;
		return NULL;
	}
	p = b->data + b->len;
	b->len += n;
	return p;
}

void omni_buf_begin(struct omni_buf *b, uint16_t type, uint64_t req_id)
{
	b->len = 0;
	b->overflow = false;
	b->truncated = false;
	memset(b->data, 0, OMNI_HDR_LEN);
	st16(b->data, type);
	st64(b->data + 8, req_id);
	b->len = OMNI_HDR_LEN;
}

void omni_put_u8(struct omni_buf *b, uint8_t v)
{
	uint8_t *p = room(b, 1);

	if (p)
		*p = v;
}

void omni_put_u16(struct omni_buf *b, uint16_t v)
{
	uint8_t *p = room(b, 2);

	if (p)
		st16(p, v);
}

void omni_put_u32(struct omni_buf *b, uint32_t v)
{
	uint8_t *p = room(b, 4);

	if (p)
		st32(p, v);
}

void omni_put_u64(struct omni_buf *b, uint64_t v)
{
	uint8_t *p = room(b, 8);

	if (p)
		st64(p, v);
}

void omni_put_zero(struct omni_buf *b, size_t n)
{
	uint8_t *p = room(b, n);

	if (p)
		memset(p, 0, n);
}

void omni_put_obj(struct omni_buf *b, const struct omni_obj_ref *o)
{
	omni_put_u64(b, o->fsid);
	omni_put_u64(b, o->ino);
	omni_put_u32(b, o->ino_gen);
	omni_put_u32(b, o->export_id);
	omni_put_u64(b, o->file_id);
}

void omni_put_ident(struct omni_buf *b, const struct omni_identity *id)
{
	uint8_t *p = room(b, 48);

	if (p == NULL)
		return;
	memset(p, 0, 48);
	st32(p, id->uid);
	st32(p + 4, id->gid);
	p[8] = id->origin;
	p[9] = id->nfs_vers;
	p[10] = id->nfs_minor;
	p[11] = id->sec;
	p[12] = id->addr_family;
	memcpy(p + 16, id->addr, 16);
	st16(p + 32, id->port);
	st64(p + 40, id->clientid);
}

void omni_put_str(struct omni_buf *b, const char *s, size_t len, size_t max,
		  bool cut_left)
{
	size_t avail;
	uint8_t *p;

	if (s == NULL)
		len = 0;
	if (len > max) {
		if (cut_left)
			s += len - max;
		len = max;
		b->truncated = true;
	}
	/* Never build a message over the maximum: cut the string to fit. */
	avail = b->len + 2 <= OMNI_MAX_MSG ? OMNI_MAX_MSG - b->len - 2 : 0;
	if (len > avail) {
		if (cut_left)
			s += len - avail;
		len = avail;
		b->truncated = true;
	}
	p = room(b, 2 + len);
	if (p == NULL)
		return;
	st16(p, (uint16_t)len);
	if (len)
		memcpy(p + 2, s, len);
}

bool omni_buf_end(struct omni_buf *b)
{
	if (b->overflow)
		return false;
	st16(b->data + 2, b->truncated ? OMNI_FLAG_TRUNCATED : 0);
	st32(b->data + 4, (uint32_t)b->len);
	return true;
}

static size_t slen(const char *s)
{
	return s ? strlen(s) : 0;
}

bool omni_build_hello(struct omni_buf *b, uint64_t req_id, uint32_t features,
		      uint32_t pid, uint64_t instance, const char *gversion,
		      const char *fsal_build)
{
	omni_buf_begin(b, OMNI_HELLO, req_id);
	omni_put_u32(b, OMNI_HELLO_MAGIC);
	omni_put_u16(b, OMNI_PROTO_MAJOR);
	omni_put_u16(b, OMNI_PROTO_MINOR);
	omni_put_u32(b, features);
	omni_put_u32(b, pid);
	omni_put_u64(b, instance);
	omni_put_zero(b, 8);
	omni_put_str(b, gversion, slen(gversion), 64, false);
	omni_put_str(b, fsal_build, slen(fsal_build), 64, false);
	return omni_buf_end(b);
}

bool omni_build_read_intent(struct omni_buf *b, uint64_t req_id,
			    const struct omni_obj_ref *obj, uint64_t version_id,
			    uint64_t offset, uint64_t length, uint64_t size,
			    uint64_t resident, uint8_t op, uint8_t policy,
			    uint16_t marker_flags, uint32_t quick_ms,
			    uint8_t parked, const struct omni_identity *id,
			    const char *principal, const char *path_hint)
{
	omni_buf_begin(b, OMNI_READ_INTENT, req_id);
	omni_put_obj(b, obj);
	omni_put_u64(b, version_id);
	omni_put_u64(b, offset);
	omni_put_u64(b, length);
	omni_put_u64(b, size);
	omni_put_u64(b, resident);
	omni_put_u8(b, op);
	omni_put_u8(b, policy);
	omni_put_u16(b, marker_flags);
	omni_put_u32(b, quick_ms);
	omni_put_u8(b, parked);
	omni_put_zero(b, 7);
	omni_put_ident(b, id);
	omni_put_str(b, principal, slen(principal), 256, false);
	omni_put_str(b, path_hint, slen(path_hint), OMNI_PATH_HINT_MAX, true);
	return omni_buf_end(b);
}

bool omni_build_populate_dir(struct omni_buf *b, uint64_t req_id,
			     const struct omni_obj_ref *dir,
			     const struct omni_identity *id,
			     const char *principal, const char *path_hint)
{
	omni_buf_begin(b, OMNI_POPULATE_DIR, req_id);
	omni_put_obj(b, dir);
	omni_put_ident(b, id);
	omni_put_str(b, principal, slen(principal), 256, false);
	omni_put_str(b, path_hint, slen(path_hint), OMNI_PATH_HINT_MAX, true);
	return omni_buf_end(b);
}

bool omni_build_bad_marker(struct omni_buf *b, const struct omni_obj_ref *obj,
			   uint32_t reason, uint32_t err)
{
	omni_buf_begin(b, OMNI_BAD_MARKER, 0);
	omni_put_obj(b, obj);
	omni_put_u32(b, reason);
	omni_put_u32(b, err);
	return omni_buf_end(b);
}

bool omni_build_write_closed(struct omni_buf *b,
			     const struct omni_obj_ref *obj,
			     uint64_t size_after,
			     const struct omni_identity *id)
{
	omni_buf_begin(b, OMNI_WRITE_CLOSED, 0);
	omni_put_obj(b, obj);
	omni_put_u64(b, size_after);
	omni_put_ident(b, id);
	return omni_buf_end(b);
}

bool omni_build_ns_hint(struct omni_buf *b, uint8_t op, uint8_t what,
			const struct omni_obj_ref *parent,
			const struct omni_obj_ref *target,
			const struct omni_identity *id, const char *principal,
			const char *name, const char *new_name)
{
	static const struct omni_obj_ref zero;

	omni_buf_begin(b, OMNI_NS_HINT, 0);
	omni_put_u8(b, op);
	omni_put_u8(b, what);
	omni_put_zero(b, 6);
	omni_put_obj(b, parent ? parent : &zero);
	omni_put_obj(b, target ? target : &zero);
	omni_put_ident(b, id);
	omni_put_str(b, principal, slen(principal), 256, false);
	omni_put_str(b, name, slen(name), 1024, false);
	if (op == OMNI_NS_RENAME)
		omni_put_str(b, new_name, slen(new_name), 1024, false);
	return omni_buf_end(b);
}

bool omni_build_ping(struct omni_buf *b, uint16_t type, uint64_t req_id,
		     uint64_t mono_ns)
{
	omni_buf_begin(b, type, req_id);
	omni_put_u64(b, mono_ns);
	return omni_buf_end(b);
}

/* OPEN_STATE (contract 5.5): the reply to OPEN_STATE_QUERY. */
bool omni_build_open_state(struct omni_buf *b, uint64_t req_id,
			   uint32_t write_opens)
{
	omni_buf_begin(b, OMNI_OPEN_STATE, req_id);
	omni_put_u32(b, write_opens);
	omni_put_u32(b, 0);
	return omni_buf_end(b);
}

bool omni_build_inval_ack(struct omni_buf *b, uint64_t req_id,
			  uint64_t inval_seq)
{
	omni_buf_begin(b, OMNI_INVALIDATE_ACK, req_id);
	omni_put_u64(b, inval_seq);
	return omni_buf_end(b);
}

bool omni_build_error(struct omni_buf *b, uint32_t code, const char *msg)
{
	omni_buf_begin(b, OMNI_ERROR, 0);
	omni_put_u32(b, code);
	omni_put_str(b, msg, slen(msg), 512, false);
	return omni_buf_end(b);
}

/* ---- parser ----------------------------------------------------------- */

static void get_obj(const uint8_t *p, struct omni_obj_ref *o)
{
	o->fsid = omni_ld64(p);
	o->ino = omni_ld64(p + 8);
	o->ino_gen = omni_ld32(p + 16);
	o->export_id = omni_ld32(p + 20);
	o->file_id = omni_ld64(p + 24);
}

/* The fixed part a known daemon->FSAL type must have, or -1 if the FSAL
 * does not expect this type from the daemon.
 */
static int fixed_size(uint16_t type)
{
	switch (type) {
	case OMNI_HELLO_ACK:
		return OMNI_FIX_HELLO_ACK;
	case OMNI_READ_DECISION:
		return OMNI_FIX_READ_DECISION;
	case OMNI_POPULATE_RESULT:
		return OMNI_FIX_POPULATE_RESULT;
	case OMNI_PING:
	case OMNI_PONG:
		return OMNI_FIX_PING;
	case OMNI_RECALL_STATE:
		return OMNI_FIX_RECALL_STATE;
	case OMNI_INVALIDATE:
		return OMNI_FIX_INVALIDATE;
	case OMNI_OPEN_STATE_QUERY:
		return OMNI_FIX_OPEN_STATE_QUERY;
	case OMNI_RESYNC_BEGIN:
	case OMNI_RESYNC_END:
		return OMNI_FIX_RESYNC;
	case OMNI_ERROR:
		return OMNI_FIX_ERROR;
	default:
		return -1;
	}
}

int omni_parse(const uint8_t *p, size_t n, struct omni_msg *m)
{
	const uint8_t *f;
	int fix;

	memset(m, 0, sizeof(*m));
	if (n < OMNI_HDR_LEN || n > OMNI_MAX_MSG)
		return -1;
	m->hdr.type = omni_ld16(p);
	m->hdr.flags = omni_ld16(p + 2);
	m->hdr.length = omni_ld32(p + 4);
	m->hdr.req_id = omni_ld64(p + 8);
	if (m->hdr.length != n)
		return -1;
	fix = fixed_size(m->hdr.type);
	if (fix < 0)
		return 0; /* unknown: the caller decides (request or not) */
	/* A longer fixed part (a newer minor version) is accepted and its
	 * extra bytes ignored; a shorter one is malformed.
	 */
	if (n - OMNI_HDR_LEN < (size_t)fix)
		return -1;
	f = p + OMNI_HDR_LEN;
	m->known = true;
	switch (m->hdr.type) {
	case OMNI_HELLO_ACK:
		m->u.hello_ack.proto_major = omni_ld16(f);
		m->u.hello_ack.proto_minor = omni_ld16(f + 2);
		m->u.hello_ack.features = omni_ld32(f + 4);
		m->u.hello_ack.instance = omni_ld64(f + 8);
		m->u.hello_ack.max_inflight = omni_ld32(f + 16);
		m->u.hello_ack.park_ms_max = omni_ld32(f + 20);
		m->u.hello_ack.answer_ttl_ms = omni_ld32(f + 24);
		break;
	case OMNI_READ_DECISION:
		m->u.decision.decision = f[0];
		m->u.decision.reason = f[1];
		m->u.decision.eta_s = omni_ld32(f + 4);
		m->u.decision.ttl_ms = omni_ld32(f + 8);
		m->u.decision.recall_id = omni_ld64(f + 16);
		m->u.decision.resident_bytes = omni_ld64(f + 24);
		break;
	case OMNI_POPULATE_RESULT:
		m->u.populate.result = f[0];
		m->u.populate.eta_ms = omni_ld32(f + 4);
		break;
	case OMNI_PING:
	case OMNI_PONG:
		m->u.ping.mono_ns = omni_ld64(f);
		break;
	case OMNI_RECALL_STATE:
		get_obj(f, &m->u.recall.obj);
		m->u.recall.version_id = omni_ld64(f + 32);
		m->u.recall.recall_id = omni_ld64(f + 40);
		m->u.recall.state = f[48];
		m->u.recall.answer = f[49];
		m->u.recall.eta_s = omni_ld32(f + 52);
		m->u.recall.resident_bytes = omni_ld64(f + 56);
		m->u.recall.valid_ms = omni_ld32(f + 64);
		break;
	case OMNI_INVALIDATE:
		get_obj(f, &m->u.inval.obj);
		m->u.inval.what = omni_ld32(f + 32);
		m->u.inval.flags = omni_ld32(f + 36);
		m->u.inval.inval_seq = omni_ld64(f + 40);
		break;
	case OMNI_OPEN_STATE_QUERY:
		get_obj(f, &m->u.open_q.obj);
		break;
	case OMNI_ERROR: {
		/* The fixed part is 4 bytes in v0.1; strings follow it. */
		const uint8_t *s = f + OMNI_FIX_ERROR;
		size_t rest = n - OMNI_HDR_LEN - OMNI_FIX_ERROR, sl;

		m->u.error.code = omni_ld32(f);
		if (rest >= 2) {
			sl = omni_ld16(s);
			if (sl > rest - 2)
				return -1;
			if (sl >= sizeof(m->u.error.message))
				sl = sizeof(m->u.error.message) - 1;
			memcpy(m->u.error.message, s + 2, sl);
		}
		break;
	}
	default:
		break;
	}
	return 0;
}

bool omni_asked_new(struct omni_asked_set *s, uint64_t chunk, uint64_t now,
		    uint64_t ttl, uint32_t *nbig, uint32_t nbig_max)
{
	struct omni_asked *r = s->big ? s->big : s->in;
	uint32_t n = s->big ? OMNI_ASKED_BIG : OMNI_ASKED_INLINE, i;

	for (i = 0; i < n; i++)
		if (r[i].ms != 0 && r[i].chunk == chunk && now - r[i].ms < ttl)
			return false;
	i = s->next % n;
	if (s->big == NULL && r[i].ms != 0 && now - r[i].ms < ttl) {
		/* every inline slot is live: grow, under the global cap */
		if (__atomic_add_fetch(nbig, 1, __ATOMIC_RELAXED) <= nbig_max)
			s->big = calloc(OMNI_ASKED_BIG, sizeof(*s->big));
		if (s->big == NULL) {
			__atomic_sub_fetch(nbig, 1, __ATOMIC_RELAXED);
		} else {
			memcpy(s->big, s->in, sizeof(s->in));
			r = s->big;
			i = OMNI_ASKED_INLINE;
			n = OMNI_ASKED_BIG;
		}
	}
	r[i].chunk = chunk;
	r[i].ms = now ? now : 1;
	s->next = (i + 1) % n;
	return true;
}

void omni_asked_free(struct omni_asked_set *s, uint32_t *nbig)
{
	if (s->big != NULL) {
		free(s->big);
		s->big = NULL;
		__atomic_sub_fetch(nbig, 1, __ATOMIC_RELAXED);
	}
}

uint8_t omni_chunk_shift_for(uint64_t size, uint8_t preferred)
{
	uint8_t sh = preferred < 12 ? 12 : preferred;

	for (; sh < 40; sh++) {
		uint64_t chunks = (size >> sh) + ((size & ((1ULL << sh) - 1)) != 0);

		if ((chunks + 7) / 8 <= OMNI_MAP_MAX)
			break;
	}
	return sh;
}
