// SPDX-License-Identifier: LGPL-3.0-or-later
/*
 * Unit tests for FSAL_OMNI's marker codec and wire format
 * (fsal/FSAL_OMNI/omni_wire.c) against docs/contracts/fsal-socket.md.
 *
 * The golden marker is the one edge/crates/omnifile-marker asserts too, so
 * the C and Rust codecs are byte-identical.
 *
 *   cc -Wall -Wextra -Werror -I fsal/FSAL_OMNI fsal/tests/unit/test_wire.c \
 *      fsal/FSAL_OMNI/omni_wire.c -o test_wire && ./test_wire
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "omni_wire.h"

static int failures;

#define CHECK(cond, ...)                                     \
	do {                                                 \
		if (!(cond)) {                               \
			failures++;                          \
			printf("FAIL %s:%d: ", __FILE__, __LINE__); \
			printf(__VA_ARGS__);                 \
			printf("\n");                        \
		}                                            \
	} while (0)

static const char golden_hex[] =
	"4f4d46310102010008070605040302012a000000000000000000100000000000"
	"000001000000000007000000000000000300000000000000000000000f6500c4";

static void unhex(const char *h, uint8_t *out, size_t n)
{
	size_t i;
	unsigned int v;

	for (i = 0; i < n; i++) {
		sscanf(h + 2 * i, "%2x", &v);
		out[i] = (uint8_t)v;
	}
}

static struct omni_marker golden_marker(void)
{
	struct omni_marker m = { 0 };

	m.layout = 1;
	m.state = OMNI_MST_PARTIAL;
	m.flags = OMNI_MF_ONLINE;
	m.file_id = 0x0102030405060708ULL;
	m.version_id = 42;
	m.size = 1048576;
	m.resident_bytes = 65536;
	m.marker_seq = 7;
	m.share_id = 3;
	return m;
}

static void test_crc(void)
{
	CHECK(omni_crc32c("123456789", 9) == 0xE3069283u, "crc32c check value");
}

static void test_golden(void)
{
	uint8_t want[64], got[64];
	struct omni_marker m = golden_marker(), d;

	unhex(golden_hex, want, 64);
	omni_marker_encode(&m, got);
	CHECK(memcmp(want, got, 64) == 0, "encode matches the Rust golden vector");
	CHECK(omni_marker_decode(want, 64, &d) == OMNI_BM_OK, "golden decodes");
	CHECK(d.file_id == m.file_id && d.version_id == 42 && d.size == 1048576 &&
		      d.resident_bytes == 65536 && d.marker_seq == 7 &&
		      d.share_id == 3 && d.state == OMNI_MST_PARTIAL &&
		      d.flags == OMNI_MF_ONLINE && d.chunk_shift == 0,
	      "golden fields");
}

/* Re-encode m with one byte changed and the CRC fixed up. */
static enum omni_bad_marker mutate(size_t off, uint8_t v, bool fix_crc)
{
	uint8_t b[64];
	struct omni_marker m = golden_marker(), d;
	uint32_t c;

	omni_marker_encode(&m, b);
	b[off] = v;
	if (fix_crc) {
		c = omni_crc32c(b, 60);
		b[60] = c & 0xFF;
		b[61] = (c >> 8) & 0xFF;
		b[62] = (c >> 16) & 0xFF;
		b[63] = c >> 24;
	}
	return omni_marker_decode(b, 64, &d);
}

static void test_validity(void)
{
	uint8_t b[64];
	struct omni_marker m = golden_marker(), d;

	omni_marker_encode(&m, b);
	CHECK(omni_marker_decode(b, 63, &d) == OMNI_BM_LAYOUT, "short value");
	CHECK(omni_marker_decode(b, 65, &d) == OMNI_BM_LAYOUT, "long value");
	CHECK(mutate(0, 0x00, true) == OMNI_BM_MAGIC, "bad magic");
	CHECK(mutate(8, 0x09, false) == OMNI_BM_CRC, "bad crc");
	CHECK(mutate(4, 2, true) == OMNI_BM_LAYOUT, "unknown layout");
	CHECK(mutate(5, 0, true) == OMNI_BM_LAYOUT, "state 0");
	CHECK(mutate(5, 3, true) == OMNI_BM_LAYOUT, "state 3");
	/* resident_bytes (offset 32) above size */
	CHECK(mutate(32 + 3, 0x10, true) == OMNI_BM_LAYOUT,
	      "resident > size");
	/* RESIDENT with resident != size */
	CHECK(mutate(5, OMNI_MST_RESIDENT, true) == OMNI_BM_LAYOUT,
	      "RESIDENT with a partial watermark");
	/* chunk_shift without CHUNK_MAP */
	CHECK(mutate(56, 22, true) == OMNI_BM_LAYOUT, "chunk_shift w/o map");
	/* unknown flag bits are ignored */
	CHECK(mutate(7, 0x80, true) == OMNI_BM_OK, "unknown flag bit ignored");
	/* CHUNK_MAP with a bad shift, then a good one */
	m.flags |= OMNI_MF_CHUNK_MAP;
	m.chunk_shift = 11;
	omni_marker_encode(&m, b);
	CHECK(omni_marker_decode(b, 64, &d) == OMNI_BM_LAYOUT, "map shift 11");
	m.chunk_shift = 22;
	omni_marker_encode(&m, b);
	CHECK(omni_marker_decode(b, 64, &d) == OMNI_BM_OK, "map shift 22");
	/* a RESIDENT marker */
	m = golden_marker();
	m.state = OMNI_MST_RESIDENT;
	m.resident_bytes = m.size;
	omni_marker_encode(&m, b);
	CHECK(omni_marker_decode(b, 64, &d) == OMNI_BM_OK, "RESIDENT ok");
}

static void test_messages(void)
{
	struct omni_buf b;
	struct omni_obj_ref o = { 1, 2, 3, 4, 5 };
	struct omni_identity id = { .uid = 2001,
				    .gid = 3001,
				    .origin = OMNI_ORIGIN_OPEN_STATEID,
				    .nfs_vers = 4,
				    .nfs_minor = 2,
				    .sec = OMNI_SEC_SYS,
				    .addr_family = 4,
				    .addr = { 192, 168, 5, 15 },
				    .port = 700,
				    .clientid = 99 };
	struct omni_msg m;
	char longpath[3000];

	CHECK(omni_build_hello(&b, 1, 0x19, 123, 77, "V15.6", "abc"),
	      "hello builds");
	CHECK(b.len == 16 + 32 + 2 + 5 + 2 + 3, "hello length %zu", b.len);
	CHECK(omni_ld32(b.data + 4) == b.len, "header length field");
	CHECK(omni_ld32(b.data + 16) == OMNI_HELLO_MAGIC, "hello magic");

	CHECK(omni_build_read_intent(&b, 9, &o, 42, 8 << 20, 1 << 20, 64 << 20,
				     4 << 20, OMNI_OP_READ, OMNI_POL_WAIT,
				     OMNI_MF_ARCHIVED, 0, 0, &id, "", "/a/b"),
	      "read_intent builds");
	CHECK(b.len == 16 + 136 + 2 + 2 + 4, "read_intent length %zu", b.len);
	CHECK(omni_ld64(b.data + 16 + 32) == 42, "version at 32");
	CHECK(b.data[16 + 72] == OMNI_OP_READ, "op at 72");
	CHECK(omni_ld32(b.data + 16 + 88) == 2001, "identity uid at 88");
	CHECK(b.data[16 + 88 + 8] == OMNI_ORIGIN_OPEN_STATEID, "origin");
	CHECK(b.data[16 + 88 + 12] == 4 && b.data[16 + 88 + 16] == 192,
	      "addr family/bytes");
	CHECK(omni_ld64(b.data + 16 + 88 + 40) == 99, "clientid at 40");

	/* a 3000-byte path is cut from the left to 1024 and flagged */
	memset(longpath, 'x', sizeof(longpath) - 1);
	longpath[sizeof(longpath) - 1] = '\0';
	longpath[sizeof(longpath) - 2] = 'Z';
	omni_build_read_intent(&b, 9, &o, 42, 0, 1, 1, 0, OMNI_OP_READ, 1, 0,
			       0, 0, &id, "", longpath);
	CHECK(omni_ld16(b.data + 2) & OMNI_FLAG_TRUNCATED, "TRUNCATED set");
	CHECK(omni_ld16(b.data + 16 + 136 + 2) == 1024, "path cut to 1024");
	CHECK(b.data[b.len - 1] == 'Z', "path kept its right end");
	CHECK(b.len <= OMNI_MAX_MSG, "never over 4096");

	CHECK(omni_build_bad_marker(&b, &o, 2, 0) && b.len == 16 + 40,
	      "bad_marker 40");
	CHECK(omni_build_write_closed(&b, &o, 7, &id) && b.len == 16 + 88,
	      "write_closed 88");
	CHECK(omni_build_ns_hint(&b, OMNI_NS_SETATTR, OMNI_NS_WHAT_OWNER, NULL,
				 &o, &id, "", "", NULL) &&
		      b.len == 16 + 120 + 2 + 2,
	      "ns_hint 120 + 2 strings");
	CHECK(b.data[16] == OMNI_NS_SETATTR &&
		      b.data[17] == OMNI_NS_WHAT_OWNER && b.data[18] == 0,
	      "ns_hint op, what, then zeros");
	CHECK(omni_build_ns_hint(&b, OMNI_NS_RENAME, 0, &o, &o, &id, "", "a",
				 "b") &&
		      b.len == 16 + 120 + 2 + 3 + 3,
	      "ns_hint RENAME: 120 + 3 strings");
	CHECK(omni_build_inval_ack(&b, 0x8000000000000005ULL, 11) &&
		      b.len == 16 + 8,
	      "inval_ack 8");
	CHECK(omni_build_open_state(&b, 0x8000000000000006ULL, 3) &&
		      b.len == 16 + 8 &&
		      omni_ld16(b.data) == OMNI_OPEN_STATE &&
		      omni_ld64(b.data + 8) == 0x8000000000000006ULL &&
		      omni_ld32(b.data + 16) == 3 &&
		      omni_ld32(b.data + 20) == 0,
	      "open_state 8: write_opens, then zero");
	/* parse: a daemon OPEN_STATE_QUERY (contract 5.5) */
	{
		uint8_t r[16 + 32] = { 0x44, 0x00, 0, 0, sizeof(r) };

		r[8] = 7;
		r[15] = 0x80;
		r[16] = 9;	/* fsid */
		r[16 + 8] = 0x21; /* ino */
		r[16 + 16] = 4; /* ino_gen */
		CHECK(omni_parse(r, sizeof(r), &m) == 0 && m.known &&
			      m.hdr.type == OMNI_OPEN_STATE_QUERY &&
			      m.u.open_q.obj.fsid == 9 &&
			      m.u.open_q.obj.ino == 0x21 &&
			      m.u.open_q.obj.ino_gen == 4,
		      "parse OPEN_STATE_QUERY");
		r[4] = sizeof(r) - 1;
		CHECK(omni_parse(r, sizeof(r) - 1, &m) != 0,
		      "OPEN_STATE_QUERY shorter than 32 is malformed");
	}
	CHECK(omni_build_populate_dir(&b, 12, &o, &id, "u@X", "") &&
		      b.len == 16 + 80 + 2 + 3 + 2,
	      "populate_dir 80 + 2 strings, length %zu", b.len);
	CHECK(omni_ld16(b.data) == OMNI_POPULATE_DIR &&
		      omni_ld64(b.data + 8) == 12,
	      "populate_dir type and req_id");
	CHECK(omni_ld64(b.data + 16 + 8) == 2 &&
		      omni_ld32(b.data + 16 + 32) == 2001,
	      "populate_dir: obj ino at 8, identity uid at 32");
	CHECK(omni_ld16(b.data + 16 + 80) == 3 &&
		      memcmp(b.data + 16 + 82, "u@X", 3) == 0,
	      "populate_dir principal");

	/* parse: a daemon POPULATE_RESULT */
	{
		uint8_t r[16 + 8] = { 0x11, 0x80, 0, 0, sizeof(r) };

		r[8] = 12;
		r[16] = OMNI_POP_EIO;
		r[16 + 4] = 0x10;
		CHECK(omni_parse(r, sizeof(r), &m) == 0 && m.known &&
			      m.u.populate.result == OMNI_POP_EIO &&
			      m.u.populate.eta_ms == 0x10 &&
			      m.hdr.req_id == 12,
		      "parse POPULATE_RESULT");
	}

	/* parse: a daemon READ_DECISION */
	{
		uint8_t r[16 + 40] = { 0 };

		r[0] = 0x10;
		r[1] = 0x80;
		r[4] = sizeof(r);
		r[8] = 9;
		r[16] = OMNI_DEC_DELAY;
		r[16 + 8] = 0xE8;
		r[16 + 9] = 0x03; /* ttl 1000 */
		CHECK(omni_parse(r, sizeof(r), &m) == 0 && m.known &&
			      m.u.decision.decision == OMNI_DEC_DELAY &&
			      m.u.decision.ttl_ms == 1000 && m.hdr.req_id == 9,
		      "parse READ_DECISION");
		CHECK(omni_parse(r, sizeof(r) - 1, &m) != 0,
		      "length mismatch is malformed");
		r[4] = sizeof(r) - 8;
		CHECK(omni_parse(r, sizeof(r) - 8, &m) != 0,
		      "short fixed part is malformed");
	}
	/* parse: a longer fixed part (newer minor) is accepted */
	{
		uint8_t r[16 + 48] = { 0 };

		r[0] = 0x10;
		r[1] = 0x80;
		r[4] = sizeof(r);
		r[16] = OMNI_DEC_EIO;
		CHECK(omni_parse(r, sizeof(r), &m) == 0 &&
			      m.u.decision.decision == OMNI_DEC_EIO,
		      "longer fixed part accepted");
	}
	/* parse: RESYNC_BEGIN/END are header-only */
	{
		uint8_t r[16] = { 0x42, 0, 0, 0, 16 };

		CHECK(omni_parse(r, 16, &m) == 0 && m.known,
		      "RESYNC_BEGIN header-only");
	}
	/* parse: unknown type is not malformed */
	{
		uint8_t r[20] = { 0x99, 0x01, 0, 0, 20 };

		CHECK(omni_parse(r, 20, &m) == 0 && !m.known, "unknown type");
	}
}

/* The chunk-map golden vector (contract 8, M5), also asserted by
 * omnifile-marker, omni_proto.py and the smb-guard's classify tests.
 */
static const char map_golden_hex[] =
	"4f4d46310102110008070605040302012a000000000000000000a00000000000"
	"0000000000000000080000000000000003000000000000001600000" "0a2fb3ce0";

static void test_map(void)
{
	uint8_t want[64], got[64], map[2] = { 0x05, 0 };
	struct omni_marker m = { 0 }, d;
	const uint64_t MiB = 1 << 20;

	m.layout = 1;
	m.state = OMNI_MST_PARTIAL;
	m.flags = OMNI_MF_ONLINE | OMNI_MF_CHUNK_MAP;
	m.file_id = 0x0102030405060708ULL;
	m.version_id = 42;
	m.size = 10 * MiB;
	m.resident_bytes = 0;
	m.marker_seq = 8;
	m.share_id = 3;
	m.chunk_shift = 22;
	unhex(map_golden_hex, want, 64);
	omni_marker_encode(&m, got);
	CHECK(memcmp(want, got, 64) == 0, "map marker matches the golden vector");
	CHECK(omni_marker_decode(want, 64, &d) == OMNI_BM_OK &&
		      d.chunk_shift == 22 && d.flags == 0x0011,
	      "map marker decodes");
	CHECK(omni_map_len(&m) == 1, "10 MiB at 4 MiB: 3 chunks, 1 byte");
	CHECK(omni_map_valid(&m, map, 1), "map 05 valid");
	CHECK(!omni_map_valid(&m, map, 2), "map of the wrong length invalid");
	map[0] = 0x0D;
	CHECK(!omni_map_valid(&m, map, 1), "a bit past the last chunk invalid");
	map[0] = 0x05;
	CHECK(omni_range_local(&m, map, 1, 0, 4 * MiB), "[0, 4 MiB) local");
	CHECK(omni_range_local(&m, map, 1, 8 * MiB, 10 * MiB),
	      "[8 MiB, 10 MiB) local");
	CHECK(!omni_range_local(&m, map, 1, 4 * MiB, 8 * MiB),
	      "[4 MiB, 8 MiB) not local");
	CHECK(!omni_range_local(&m, map, 1, 3 * MiB, 5 * MiB),
	      "a range touching chunk 1 not local");
	CHECK(!omni_range_local(&m, NULL, 0, 0, 4 * MiB),
	      "without the map only the prefix counts");
	m.resident_bytes = 8 * MiB;
	CHECK(omni_range_local(&m, map, 1, 0, 10 * MiB),
	      "prefix 8 MiB + chunk 2 covers the file");
	m.resident_bytes = 0;
	m.flags = OMNI_MF_ONLINE; /* flag off: the map is ignored */
	CHECK(!omni_range_local(&m, map, 1, 0, 4 * MiB),
	      "a map without the marker's flag is ignored");
	/* a big map: 1 TiB at 4 MiB = 32 KiB; 1 TiB at 4 KiB does not fit */
	m.flags = OMNI_MF_ONLINE | OMNI_MF_CHUNK_MAP;
	m.size = 1ULL << 40;
	CHECK(omni_map_len(&m) == 32768, "1 TiB map is 32 KiB");
	m.chunk_shift = 12;
	CHECK(omni_map_len(&m) == 0, "a map over 64 KiB is invalid");
	CHECK(OMNI_PROTO_MINOR == 2, "this FSAL speaks v0.2 (CHUNK_MAP)");
}

/* S8: an entry that asks one chunk costs no heap; a 256-chunk storm on one
 * entry asks each chunk once; growth stops at the global cap.
 */
/* Shared with the Rust and Python chunk-shift rule. */
static void test_chunk_shift(void)
{
	uint64_t tib = 1ULL << 40;

	CHECK(omni_chunk_shift_for(2 * tib, 22) == 22, "2 TiB keeps 22");
	CHECK(omni_chunk_shift_for(2 * tib + 1, 22) == 23, "just over 2 TiB is 23");
	CHECK(omni_chunk_shift_for(3 * tib, 22) == 23, "3 TiB is 23");
	CHECK(((((3 * tib) >> 23) + 7) / 8) == 49152, "3 TiB map is 49,152 bytes");
	CHECK(omni_chunk_shift_for(4 * tib, 22) == 23, "4 TiB is 23");
	CHECK(omni_chunk_shift_for(100ULL << 30, 22) == 22, "100 GiB keeps 22");
	CHECK(omni_chunk_shift_for(UINT64_MAX, 22) == 40, "capped at 40");
}

static void test_asked(void)
{
	enum { N = 100000 };
	struct omni_asked_set *sets = calloc(N, sizeof(*sets));
	struct omni_asked_set one, a, b, c;
	uint32_t nbig = 0, i, asks = 0;

	for (i = 0; i < N; i++) {
		CHECK(omni_asked_new(&sets[i], 7, 1000, 1000, &nbig, 1024),
		      "first ask of a chunk is new");
		CHECK(!omni_asked_new(&sets[i], 7, 1500, 1000, &nbig, 1024),
		      "asked again within the TTL: not new");
	}
	CHECK(nbig == 0, "10^5 one-chunk entries allocated %u grown sets", nbig);
	for (i = 0; i < N; i++)
		CHECK(sets[i].big == NULL, "entry %u stays inline", i);
	free(sets);

	memset(&one, 0, sizeof(one));
	for (i = 0; i < OMNI_ASKED_BIG; i++)
		asks += omni_asked_new(&one, i, 1000 + i, 1000, &nbig, 1024);
	for (i = 0; i < OMNI_ASKED_BIG; i++)
		asks += omni_asked_new(&one, i, 1300, 1000, &nbig, 1024);
	CHECK(asks == OMNI_ASKED_BIG, "a 256-chunk storm asks %u times", asks);
	CHECK(nbig == 1 && one.big != NULL, "the busy entry grew (%u)", nbig);
	CHECK(omni_asked_new(&one, 3, 5000, 1000, &nbig, 1024),
	      "past the TTL a chunk is asked again");
	omni_asked_free(&one, &nbig);
	CHECK(nbig == 0 && one.big == NULL, "free returns the grown set");

	memset(&a, 0, sizeof(a));
	memset(&b, 0, sizeof(b));
	memset(&c, 0, sizeof(c));
	for (i = 0; i < 8; i++) {
		(void)omni_asked_new(&a, i, 1000, 1000, &nbig, 2);
		(void)omni_asked_new(&b, i, 1000, 1000, &nbig, 2);
		(void)omni_asked_new(&c, i, 1000, 1000, &nbig, 2);
	}
	CHECK(nbig == 2 && c.big == NULL, "the cap of 2 grown sets holds (%u)",
	      nbig);
	CHECK(!omni_asked_new(&c, 7, 1001, 1000, &nbig, 2),
	      "an inline set still dedupes its recent chunks");
	omni_asked_free(&a, &nbig);
	omni_asked_free(&b, &nbig);
	omni_asked_free(&c, &nbig);
	CHECK(nbig == 0, "all grown sets freed");
}

int main(void)
{
	test_crc();
	test_golden();
	test_validity();
	test_messages();
	test_map();
	test_asked();
	test_chunk_shift();
	if (failures) {
		printf("test_wire: %d failure(s)\n", failures);
		return 1;
	}
	printf("test_wire: all checks passed\n");
	return 0;
}
