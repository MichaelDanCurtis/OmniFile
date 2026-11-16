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

/*
 * The daemon connection (docs/contracts/fsal-socket.md sections 2, 5-7):
 *
 *  - one SOCK_SEQPACKET connection per Ganesha process, owned by one socket
 *    thread that connects, does the HELLO handshake, reads every message,
 *    applies pushes to the pending table and runs the timers;
 *  - workers only send with MSG_DONTWAIT and look things up in memory;
 *  - a completion pool calls done_cb for parked reads, and an invalidation
 *    thread applies INVALIDATE (it may wait for pinned local I/O to drain,
 *    never for the daemon) and sends INVALIDATE_ACK.
 */

#include "config.h"

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/eventfd.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/un.h>

#include "fsal.h"
#include "fsal_convert.h"
#include "FSAL/fsal_commonlib.h"
#include "export_mgr.h"
#include "omni_methods.h"

#ifndef OMNI_FSAL_BUILD
#define OMNI_FSAL_BUILD "unknown"
#endif

/* What this FSAL offers in HELLO and keeps from HELLO_ACK. */
#define OMNI_FSAL_FEATURES                                              \
	(OMNI_FEAT_PARK | OMNI_FEAT_CHUNK_MAP | OMNI_FEAT_POPULATE_DIR | \
	 OMNI_FEAT_NS_HINT | OMNI_FEAT_WRITE_CLOSED | OMNI_FEAT_OPEN_STATE)

#define OMNI_SHARDS 64
#define OMNI_BUCKETS 1024 /* per shard */
#define OMNI_PEND_MAX 1000000
#define OMNI_REQ_BUCKETS 4096
/* M5: entries whose asked set may grow past its inline slots (4 KiB each). */
#define OMNI_ASKED_BIG_MAX 1024
/* M5: per-client-address parked-read counters (hashed; a collision only
 * makes the cap stricter).
 */
#define OMNI_CLIENT_SLOTS 4096
#define OMNI_PARK_PER_CLIENT_DEFAULT 16

#define T_CONNECT_MIN_MS 50
#define T_CONNECT_MAX_MS 2000
#define T_HELLO_MS 1000
#define T_REQUEST_MS 2000
#define T_PING_IDLE_MS 1000
#define T_DEAD_MS 3000
#define T_ASK_GAP_MS 100
#define T_TICK_MS 50

enum conn_state { CS_DOWN, CS_HELLO, CS_UP };

/* ---- time and rate limiting ----------------------------------------- */

uint64_t omni_now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

bool omni_rl_ok(struct omni_rl *rl, uint64_t interval_ms, uint32_t *dropped)
{
	uint64_t now = omni_now_ms();
	uint64_t last = __atomic_load_n(&rl->last_ms, __ATOMIC_RELAXED);

	if (last != 0 && now - last < interval_ms) {
		__atomic_add_fetch(&rl->suppressed, 1, __ATOMIC_RELAXED);
		return false;
	}
	if (!__atomic_compare_exchange_n(&rl->last_ms, &last, now, false,
					 __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
		__atomic_add_fetch(&rl->suppressed, 1, __ATOMIC_RELAXED);
		return false;
	}
	*dropped = __atomic_exchange_n(&rl->suppressed, 0, __ATOMIC_RELAXED);
	return true;
}

/* ---- pending table (contract 6.2) ------------------------------------ */

struct omni_pkey {
	uint64_t fsid, ino, version;
	uint32_t gen;
};

struct omni_park;

struct omni_pend {
	struct omni_pend *next;
	struct omni_pkey k;
	uint64_t recall_id;
	uint64_t resident;
	uint8_t state; /* 0 "asked", else RECALL_STATE.state */
	uint8_t answer; /* OMNI_DEC_DELAY or OMNI_DEC_EIO */
	uint64_t expires_ms;
	uint64_t last_asked_ms;
	uint32_t resync_gen;
	uint32_t nreq;
	struct {
		uint32_t uid;
		uint32_t addr;
	} req[8];
	/* M5, CHUNK_MAP: the chunks recently asked for (omni_wire.h) */
	struct omni_asked_set asked;
	struct omni_park *parked; /* protected by park_lock */
};

struct omni_shard {
	pthread_mutex_t mu;
	struct omni_pend *b[OMNI_BUCKETS];
};

/* A parked read (contract 6.3). */
struct omni_park {
	struct omni_park *gnext, *gprev; /* global list, by park time */
	struct omni_park *enext; /* the pending entry's list */
	struct omni_pend *pend;
	struct fsal_obj_handle *obj;
	struct fsal_io_arg *read_arg;
	fsal_async_cb done_cb;
	void *caller_arg;
	struct gsh_export *gexp;
	struct omni_fsal_export *exp;
	uint64_t end; /* offset + length */
	uint64_t deadline_ms;
	uint32_t cslot; /* C.client_parked slot */
	/* The protocol's ASYNC_PROC_* flags of the request (contract 6.3,
	 * "Resume only once the request waits"). A resume before
	 * ASYNC_PROC_EXIT is held: detached from the pending entry and sent
	 * by the timer once the flag is set.
	 */
	uint32_t *async_flags;
	bool held;
};

/* Outstanding request: req_id -> pending key */
struct omni_req {
	struct omni_req *next;
	uint64_t req_id;
	struct omni_pkey k;
	uint64_t sent_ms;
};

/* Work for the completion pool and the invalidation thread. */
enum job_kind { JOB_PARK, JOB_INVAL };
enum park_how { PK_RESUME, PK_DELAY, PK_EIO, PK_DEFAULT };

struct omni_job {
	struct omni_job *next;
	enum job_kind kind;
	struct omni_park *p;
	enum park_how how;
	struct omni_obj_ref obj;
	uint32_t what, flags;
	uint64_t req_id, seq, epoch;
};

struct omni_queue {
	pthread_mutex_t mu;
	pthread_cond_t cv;
	struct omni_job *head, *tail;
};

static struct {
	char sock_path[108];
	uint32_t ncompl;

	pthread_mutex_t life; /* start/stop */
	bool started;
	bool stopping;
	pthread_t sock_thr;
	pthread_t inval_thr;
	pthread_t *compl_thr;
	int evfd;

	/* connection; fd guarded by fd_lock so workers never send on a
	 * descriptor the socket thread has closed and reused
	 */
	pthread_rwlock_t fd_lock;
	int fd;
	int state; /* enum conn_state, atomic */
	uint64_t epoch; /* atomic; marker-cache epoch */
	uint64_t conn_gen; /* atomic; changes on every connect/disconnect */
	uint32_t features, max_inflight, park_ms_max, answer_ttl_ms;
	uint64_t daemon_instance;
	bool have_instance;
	uint64_t instance;
	uint64_t next_req; /* atomic */
	uint32_t inflight; /* atomic */
	uint32_t resync_gen;
	bool resyncing; /* atomic: HELLO_ACK .. RESYNC_END (fill fence) */
	uint64_t last_rx_ms, last_ping_ms, hello_ms, next_connect_ms;
	uint64_t backoff_ms;
	struct omni_rl rl_conn, rl_unknown, rl_hello, rl_populate;

	struct omni_shard shard[OMNI_SHARDS];
	uint32_t npend; /* atomic */
	uint32_t nasked_big; /* grown asked sets; atomic */
	/* M5: parked reads per client address (atomic) and the cap */
	uint32_t park_per_client;
	uint32_t client_parked[OMNI_CLIENT_SLOTS];

	pthread_mutex_t req_mu;
	struct omni_req *req[OMNI_REQ_BUCKETS];

	pthread_mutex_t park_lock;
	struct omni_park *park_head, *park_tail;

	struct omni_queue done_q, inval_q;
} C = {
	.sock_path = "/run/omnifile/fsal.sock",
	.ncompl = 4,
	.life = PTHREAD_MUTEX_INITIALIZER,
	.fd_lock = PTHREAD_RWLOCK_INITIALIZER,
	.fd = -1,
	.evfd = -1,
	.req_mu = PTHREAD_MUTEX_INITIALIZER,
	.park_lock = PTHREAD_MUTEX_INITIALIZER,
	.done_q = { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER },
	.inval_q = { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER },
};

void omni_conn_configure(const char *sock_path, uint32_t completion_threads,
			 uint32_t park_per_client)
{
	if (park_per_client > 0)
		C.park_per_client = park_per_client;
	if (sock_path != NULL && sock_path[0] != '\0')
		snprintf(C.sock_path, sizeof(C.sock_path), "%s", sock_path);
	if (completion_threads > 0)
		C.ncompl = completion_threads;
}

bool omni_conn_connected(void)
{
	return __atomic_load_n(&C.state, __ATOMIC_ACQUIRE) == CS_UP;
}

uint64_t omni_conn_epoch(void)
{
	return __atomic_load_n(&C.epoch, __ATOMIC_ACQUIRE);
}

void omni_conn_bump_epoch(void)
{
	__atomic_add_fetch(&C.epoch, 1, __ATOMIC_ACQ_REL);
}

uint32_t omni_conn_features(void)
{
	return omni_conn_connected() ? C.features : 0;
}

static void wake(void)
{
	uint64_t one = 1;

	if (C.evfd >= 0)
		(void)!write(C.evfd, &one, sizeof(one));
}

/* ---- sending ---------------------------------------------------------- */

int omni_conn_send(struct omni_buf *b)
{
	ssize_t n;
	int rc = 0;

	if (b->overflow)
		return EMSGSIZE;
	pthread_rwlock_rdlock(&C.fd_lock);
	if (C.fd < 0) {
		rc = ENOTCONN;
	} else {
		n = send(C.fd, b->data, b->len, MSG_DONTWAIT | MSG_NOSIGNAL);
		if (n < 0)
			rc = errno;
		else if ((size_t)n != b->len)
			rc = EIO;
	}
	pthread_rwlock_unlock(&C.fd_lock);
	if (rc == EAGAIN || rc == EWOULDBLOCK) {
		uint32_t d;

		if (omni_rl_ok(&C.rl_conn, 1000, &d))
			LogWarn(COMPONENT_FSAL,
				"FSAL_OMNI: daemon socket congested (send would block); failing closed (%u similar suppressed)",
				d);
	}
	return rc;
}

int omni_conn_populate(const struct omni_obj_ref *dir,
		       const struct omni_identity *id)
{
	struct omni_buf *b;
	uint64_t req_id;
	int rc;

	if (!omni_conn_connected())
		return ENOTCONN;
	if (!(omni_conn_features() & OMNI_FEAT_POPULATE_DIR))
		return ENOTSUP;
	/* A request: non-zero, and never with the daemon's request bit. The
	 * reply is matched by nobody (handle_msg logs a failure).
	 */
	req_id = __atomic_add_fetch(&C.next_req, 1, __ATOMIC_RELAXED) &
		 ~OMNI_DAEMON_REQ_BIT;
	b = gsh_malloc(sizeof(*b), MEM_COMP_FSAL);
	omni_build_populate_dir(b, req_id, dir, id, id->principal, "");
	rc = omni_conn_send(b);
	gsh_free(b, MEM_COMP_FSAL);
	return rc;
}

/* Socket-thread-only send (the HELLO, PONG, ERROR before close). */
static int send_raw(int fd, struct omni_buf *b)
{
	ssize_t n = send(fd, b->data, b->len, MSG_DONTWAIT | MSG_NOSIGNAL);

	return n == (ssize_t)b->len ? 0 : (n < 0 ? errno : EIO);
}

/* ---- queues ----------------------------------------------------------- */

static void q_push(struct omni_queue *q, struct omni_job *j)
{
	j->next = NULL;
	pthread_mutex_lock(&q->mu);
	if (q->tail)
		q->tail->next = j;
	else
		q->head = j;
	q->tail = j;
	pthread_cond_signal(&q->cv);
	pthread_mutex_unlock(&q->mu);
}

static struct omni_job *q_pop(struct omni_queue *q)
{
	struct omni_job *j;

	pthread_mutex_lock(&q->mu);
	while (q->head == NULL && !__atomic_load_n(&C.stopping,
						   __ATOMIC_ACQUIRE))
		pthread_cond_wait(&q->cv, &q->mu);
	j = q->head;
	if (j) {
		q->head = j->next;
		if (q->head == NULL)
			q->tail = NULL;
	}
	pthread_mutex_unlock(&q->mu);
	return j;
}

/* ---- parked reads ----------------------------------------------------- */

/* Unlink p from both lists. park_lock held. */
static void park_unlink(struct omni_park *p)
{
	struct omni_park **pp;

	if (p->gprev)
		p->gprev->gnext = p->gnext;
	else
		C.park_head = p->gnext;
	if (p->gnext)
		p->gnext->gprev = p->gprev;
	else
		C.park_tail = p->gprev;
	p->gnext = p->gprev = NULL;
	if (p->pend) {
		for (pp = &p->pend->parked; *pp; pp = &(*pp)->enext) {
			if (*pp == p) {
				*pp = p->enext;
				break;
			}
		}
		p->pend = NULL;
	}
	p->enext = NULL;
}

/* Has the protocol marked the parked request as waiting? Ganesha V15.6's
 * NFS READ code (nfs3_read, nfs4_op_read and their resume handlers
 * nfs3_read_resume, nfs4_op_read_resume, nfs4_op_read_plus_resume) clears
 * ASYNC_PROC_EXIT before each read2 and sets it once read2 has returned. A
 * done_cb after that reschedules the request (svc_resume), and the resume
 * handler calls read2 again because fsal_resume is set. A done_cb before it
 * is completed inline by the code after read2, and the resume handlers then
 * ignore fsal_resume: a resume delivered there went out as a 0-byte READ
 * without EOF, which the Linux client reports as EIO. The flag is clear while
 * this park's read2 runs (it was cleared before the call), so seeing it set
 * proves the call returned and the request waits. park_lock held.
 */
static bool park_waiting(const struct omni_park *p)
{
	return __atomic_load_n(p->async_flags, __ATOMIC_ACQUIRE) &
	       ASYNC_PROC_EXIT;
}

/* Hold a resume until the request waits: the park leaves its pending entry
 * (which a DONE may free), stays on the global list (deadline, unexport and
 * disconnect still apply) and park_sweep resumes it once park_waiting.
 * park_lock held.
 */
static void park_hold_resume(struct omni_park *p)
{
	struct omni_park **pp;

	if (p->pend) {
		for (pp = &p->pend->parked; *pp; pp = &(*pp)->enext) {
			if (*pp == p) {
				*pp = p->enext;
				break;
			}
		}
		p->pend = NULL;
		p->enext = NULL;
	}
	p->held = true;
}

/* Hand an unlinked park to the completion pool. */
static void park_finish(struct omni_park *p, enum park_how how)
{
	struct omni_job *j = gsh_calloc(1, sizeof(*j), MEM_COMP_FSAL);

	j->kind = JOB_PARK;
	j->p = p;
	j->how = how;
	q_push(&C.done_q, j);
}

/* Complete every park of an entry. park_lock held. */
static void park_finish_entry(struct omni_pend *e, enum park_how how,
			      uint64_t covered)
{
	struct omni_park *p = e->parked, *nx;

	for (; p; p = nx) {
		nx = p->enext;
		if (how == PK_RESUME && covered != UINT64_MAX &&
		    p->end > covered)
			continue; /* not yet local */
		if (how == PK_RESUME && !park_waiting(p)) {
			park_hold_resume(p);
			continue;
		}
		park_unlink(p);
		park_finish(p, how);
	}
}

static void run_park_job(struct omni_job *j)
{
	struct omni_park *p = j->p;
	struct req_op_context ctx;
	fsal_status_t st;
	enum park_how how = j->how;

	if (how == PK_DEFAULT)
		how = p->exp->policy == OMNI_POL_FAILFAST ? PK_EIO : PK_DELAY;
	switch (how) {
	case PK_RESUME:
		/* Only once the request waits (park_waiting): the protocol
		 * layer reschedules it and calls read2 again on a worker, which
		 * re-reads the marker before serving any byte. The status is
		 * DELAY, not success, as a second line: were a handler ever to
		 * complete the request without honouring fsal_resume, the client
		 * would retry, never see a 0-byte READ.
		 */
		p->read_arg->fsal_resume = OMNI_FSAL_RESUME;
		st = fsalstat(ERR_FSAL_DELAY, EAGAIN);
		break;
	case PK_EIO:
		st = fsalstat(ERR_FSAL_IO, EIO);
		break;
	default:
		st = fsalstat(ERR_FSAL_DELAY, EAGAIN);
		break;
	}
	LogInfo(COMPONENT_FSAL, "FSAL_OMNI: parked read completes: %s%s",
		 how == PK_RESUME ? "resume" : fsal_err_txt(st),
		 p->held ? " (held until the request waited)" : "");
	/* The request holds a reference to its export while it is parked;
	 * the op context takes one more and release_op_context drops it.
	 */
	/* The read stops counting against the caps before its reply goes out:
	 * the client's next read may arrive before done_cb returns.
	 */
	__atomic_sub_fetch(&p->exp->parked, 1, __ATOMIC_RELAXED);
	__atomic_sub_fetch(&C.client_parked[p->cslot], 1, __ATOMIC_RELAXED);
	get_gsh_export_ref(p->gexp);
	init_op_context_simple(&ctx, p->gexp, &p->exp->export);
	p->done_cb(p->obj, st, p->read_arg, p->caller_arg);
	release_op_context();
	gsh_free(p, MEM_COMP_FSAL);
}

/* Complete every held resume fail-closed (disconnect): they have left their
 * pending entries, so pend_clear does not see them. park_lock held.
 */
static void park_finish_held(void)
{
	struct omni_park *p, *nx;

	for (p = C.park_head; p; p = nx) {
		nx = p->gnext;
		if (p->held) {
			park_unlink(p);
			park_finish(p, PK_DEFAULT);
		}
	}
}

void omni_conn_unpark_export(struct omni_fsal_export *exp)
{
	struct omni_park *p, *nx;

	pthread_mutex_lock(&C.park_lock);
	for (p = C.park_head; p; p = nx) {
		nx = p->gnext;
		if (p->exp == exp) {
			park_unlink(p);
			park_finish(p, PK_DEFAULT);
		}
	}
	pthread_mutex_unlock(&C.park_lock);
}

/* ---- pending table helpers ------------------------------------------- */

static uint64_t hkey(const struct omni_pkey *k)
{
	uint64_t h = k->fsid * 0x9E3779B97F4A7C15ULL;

	h ^= k->ino + 0x632BE59BD9B4E019ULL + (h << 6) + (h >> 2);
	h ^= k->version + 0x85EBCA77C2B2AE63ULL + (h << 6) + (h >> 2);
	h ^= k->gen + (h << 6) + (h >> 2);
	return h;
}

static struct omni_shard *shard_of(const struct omni_pkey *k, uint32_t *bkt)
{
	uint64_t h = hkey(k);

	*bkt = (uint32_t)((h >> 6) % OMNI_BUCKETS);
	return &C.shard[h % OMNI_SHARDS];
}

static bool key_eq(const struct omni_pkey *a, const struct omni_pkey *b)
{
	return a->fsid == b->fsid && a->ino == b->ino &&
	       a->version == b->version && a->gen == b->gen;
}

static struct omni_pend *pend_find(struct omni_shard *s, uint32_t bkt,
				   const struct omni_pkey *k)
{
	struct omni_pend *e;

	for (e = s->b[bkt]; e; e = e->next)
		if (key_eq(&e->k, k))
			return e;
	return NULL;
}

/* May this entry be evicted or swept? Not while reads are parked on it,
 * and never a FETCHING entry: it is the fill fence (contract 6.1) and lasts
 * until DONE, FAILED or CANCELLED replaces it, or a resync drops it.
 */
static bool pend_droppable(const struct omni_pend *e)
{
	return e->parked == NULL && e->state != OMNI_RS_FETCHING;
}

static void pend_remove(struct omni_shard *s, uint32_t bkt,
			struct omni_pend *e);

/* At OMNI_PEND_MAX (contract 6.2): evict from this shard (its lock held)
 * every expired droppable entry, or else the oldest droppable one. An
 * evicted entry costs one more READ_INTENT, nothing else. Returns how many
 * went (0: nothing in the shard can go).
 */
static uint32_t pend_evict(struct omni_shard *s, uint64_t now)
{
	struct omni_pend *e, *nx, *old = NULL;
	uint32_t b, ob = 0, n = 0;

	for (b = 0; b < OMNI_BUCKETS; b++) {
		for (e = s->b[b]; e; e = nx) {
			nx = e->next;
			if (!pend_droppable(e))
				continue;
			if (e->expires_ms <= now) {
				pend_remove(s, b, e);
				n++;
			} else if (old == NULL ||
				   e->expires_ms < old->expires_ms) {
				old = e;
				ob = b;
			}
		}
	}
	if (n == 0 && old != NULL) {
		pend_remove(s, ob, old);
		n = 1;
	}
	return n;
}

/* Create an entry. At the cap the shard evicts first. A FETCHING push
 * (force) is never refused, because the fill fence must not be lost.
 */
static struct omni_pend *pend_create(struct omni_shard *s, uint32_t bkt,
				     const struct omni_pkey *k, bool force)
{
	static struct omni_rl rl_full;
	struct omni_pend *e;
	uint32_t n, d;

	if (__atomic_load_n(&C.npend, __ATOMIC_RELAXED) >= OMNI_PEND_MAX) {
		n = pend_evict(s, omni_now_ms());
		if (n == 0 && !force) {
			if (omni_rl_ok(&rl_full, 10000, &d))
				LogWarn(COMPONENT_FSAL,
					"FSAL_OMNI: pending table at its cap of %u entries and nothing in the shard can be evicted (parked reads or fills); answering without an entry (%u similar suppressed)",
					OMNI_PEND_MAX, d);
			return NULL;
		}
		if (omni_rl_ok(&rl_full, 10000, &d))
			LogInfo(COMPONENT_FSAL,
				"FSAL_OMNI: pending table at its cap of %u entries: evicted %u of the oldest (%u similar suppressed)",
				OMNI_PEND_MAX, n, d);
	}
	e = gsh_calloc(1, sizeof(*e), MEM_COMP_FSAL);
	e->k = *k;
	e->next = s->b[bkt];
	s->b[bkt] = e;
	__atomic_add_fetch(&C.npend, 1, __ATOMIC_RELAXED);
	return e;
}

/* Remove an entry. Shard lock held; the caller has completed its parks. */
static void pend_remove(struct omni_shard *s, uint32_t bkt,
			struct omni_pend *e)
{
	struct omni_pend **pp;

	for (pp = &s->b[bkt]; *pp; pp = &(*pp)->next) {
		if (*pp == e) {
			*pp = e->next;
			break;
		}
	}
	__atomic_sub_fetch(&C.npend, 1, __ATOMIC_RELAXED);
	omni_asked_free(&e->asked, &C.nasked_big);
	gsh_free(e, MEM_COMP_FSAL);
}

static uint32_t addr_hash(const struct omni_identity *id)
{
	return omni_crc32c(id->addr, sizeof(id->addr)) ^ id->addr_family;
}

/* Is this caller already a reported requester? Adds it if there is room.
 * Returns true if a READ_INTENT should be sent for it.
 */
static bool requester_new(struct omni_pend *e, const struct omni_identity *id)
{
	uint32_t a = addr_hash(id), i;

	for (i = 0; i < e->nreq; i++)
		if (e->req[i].uid == id->uid && e->req[i].addr == a)
			return false;
	if (e->nreq >= 8)
		return false;
	e->req[e->nreq].uid = id->uid;
	e->req[e->nreq].addr = a;
	e->nreq++;
	return true;
}

/* M5: has this chunk been asked for within the answer TTL? Records it if
 * not. Returns true if a READ_INTENT should be sent for it.
 */
static bool chunk_new(struct omni_pend *e, uint64_t chunk, uint64_t now)
{
	if (chunk == UINT64_MAX)
		return false;
	return omni_asked_new(&e->asked, chunk, now, C.answer_ttl_ms ?: 1000,
			      &C.nasked_big, OMNI_ASKED_BIG_MAX);
}

static void pkey_of(const struct omni_obj_ref *o, uint64_t version,
		    struct omni_pkey *k)
{
	k->fsid = o->fsid;
	k->ino = o->ino;
	k->gen = o->ino_gen;
	k->version = version;
}

static fsal_status_t dec_status(uint8_t dec)
{
	if (dec == OMNI_DEC_EIO)
		return fsalstat(ERR_FSAL_IO, EIO);
	return fsalstat(ERR_FSAL_DELAY, EAGAIN);
}

static void log_closed(struct omni_ask *a, const char *why)
{
	uint32_t d;
	fsal_status_t st = omni_policy_default(a->exp);

	if (!omni_rl_ok(&a->exp->rl_closed, 1000, &d))
		return;
	LogWarn(COMPONENT_FSAL,
		"FSAL_OMNI fail-closed: %s; export=%u ino=%" PRIu64
		" file_id=%" PRIu64 " off=%" PRIu64 " len=%" PRIu64
		" resident=%" PRIu64 " uid=%u policy=%s -> %s (%u similar suppressed)",
		why, a->exp->export_id, a->obj.ino, a->obj.file_id, a->off,
		a->len, a->m.resident_bytes, a->id.uid, a->exp->policy_str,
		st.major == ERR_FSAL_IO ? "EIO" : "DELAY", d);
}

/* Register an outstanding request and send READ_INTENT. 0 or errno. */
static int send_intent(struct omni_ask *a, const struct omni_pkey *k,
		       bool parked)
{
	struct omni_buf *b;
	struct omni_req *r;
	uint64_t id;
	uint32_t bkt;
	int rc;

	if (__atomic_add_fetch(&C.inflight, 1, __ATOMIC_ACQ_REL) >
	    C.max_inflight) {
		__atomic_sub_fetch(&C.inflight, 1, __ATOMIC_ACQ_REL);
		return EBUSY;
	}
	id = __atomic_add_fetch(&C.next_req, 1, __ATOMIC_RELAXED) &
	     ~OMNI_DAEMON_REQ_BIT;
	b = gsh_malloc(sizeof(*b), MEM_COMP_FSAL);
	omni_build_read_intent(b, id, &a->obj, a->m.version_id, a->off,
			       a->len, a->m.size, a->m.resident_bytes, a->op,
			       a->exp->policy, a->m.flags, a->exp->quick_ms,
			       parked ? 1 : 0, &a->id, a->id.principal, "");
	r = gsh_calloc(1, sizeof(*r), MEM_COMP_FSAL);
	r->req_id = id;
	r->k = *k;
	r->sent_ms = omni_now_ms();
	bkt = id % OMNI_REQ_BUCKETS;
	pthread_mutex_lock(&C.req_mu);
	r->next = C.req[bkt];
	C.req[bkt] = r;
	pthread_mutex_unlock(&C.req_mu);

	rc = omni_conn_send(b);
	gsh_free(b, MEM_COMP_FSAL);
	if (rc != 0) {
		struct omni_req **pp;

		pthread_mutex_lock(&C.req_mu);
		for (pp = &C.req[bkt]; *pp; pp = &(*pp)->next) {
			if (*pp == r) {
				*pp = r->next;
				break;
			}
		}
		pthread_mutex_unlock(&C.req_mu);
		gsh_free(r, MEM_COMP_FSAL);
		__atomic_sub_fetch(&C.inflight, 1, __ATOMIC_ACQ_REL);
	}
	LogDebug(COMPONENT_FSAL,
		 "FSAL_OMNI READ_INTENT req=%" PRIu64 " ino=%" PRIu64
		 " file_id=%" PRIu64 " op=%u off=%" PRIu64 " len=%" PRIu64
		 " uid=%u gid=%u origin=%u nfs=%u.%u parked=%d rc=%d",
		 id, a->obj.ino, a->obj.file_id, a->op, a->off, a->len,
		 a->id.uid, a->id.gid, a->id.origin, a->id.nfs_vers,
		 a->id.nfs_minor, parked, rc);
	return rc;
}

static bool can_park(struct omni_ask *a)
{
	struct fsal_io_arg *ra;
	uintptr_t tag;

	if (a->park == NULL || a->exp->park_ms == 0)
		return false;
	/* No provable handshake (omni_read_async_flags): never park, so a
	 * resume can never be delivered before the request waits.
	 */
	if (a->park->async_flags == NULL)
		return false;
	if (a->op != OMNI_OP_READ && a->op != OMNI_OP_READ_PLUS)
		return false;
	if (!(C.features & OMNI_FEAT_PARK))
		return false;
	if (!(a->m.flags & OMNI_MF_ONLINE) || (a->m.flags & OMNI_MF_ARCHIVED))
		return false;
	if (op_ctx == NULL || op_ctx->ctx_export == NULL)
		return false;
	/* A read resumed from a park that is parked again keeps its first
	 * deadline: fsal_io_arg.cbi (unused by MDCACHE and VFS) carries the
	 * first park time, tagged.
	 */
	ra = a->park->read_arg;
	tag = (uintptr_t)ra->cbi;
	if ((tag >> 48) == 0x0F5A &&
	    omni_now_ms() - (tag & 0xFFFFFFFFFFFFULL) >= a->exp->park_ms)
		return false;
	return true;
}

/* An ONLINE range read through the parking path (a fetch of seconds). */
static bool online_fetch(struct omni_ask *a)
{
	return (a->op == OMNI_OP_READ || a->op == OMNI_OP_READ_PLUS) &&
	       (C.features & OMNI_FEAT_PARK) && (a->m.flags & OMNI_MF_ONLINE) &&
	       !(a->m.flags & OMNI_MF_ARCHIVED);
}

/* Park a read on entry e. Shard lock held. Returns false if over a cap
 * (256 per export, Park_Max_Per_Client per client address).
 */
static bool park_add(struct omni_pend *e, struct omni_ask *a)
{
	struct omni_park *p;
	struct fsal_io_arg *ra = a->park->read_arg;
	uintptr_t tag = (uintptr_t)ra->cbi;
	uint64_t now = omni_now_ms(), start = now;
	uint32_t park_ms = a->exp->park_ms;
	uint32_t cslot, ccap = C.park_per_client ?: OMNI_PARK_PER_CLIENT_DEFAULT;

	if (__atomic_add_fetch(&a->exp->parked, 1, __ATOMIC_RELAXED) >
	    OMNI_PARK_MAX_PER_EXPORT) {
		static struct omni_rl rl_cap;
		uint32_t d;

		__atomic_sub_fetch(&a->exp->parked, 1, __ATOMIC_RELAXED);
		if (omni_rl_ok(&rl_cap, 1000, &d))
			LogWarn(COMPONENT_FSAL,
				"FSAL_OMNI: export %u has %u parked reads (the cap); this read answers DELAY (%u similar suppressed)",
				a->exp->export_id, OMNI_PARK_MAX_PER_EXPORT, d);
		return false;
	}
	/* M5: one client address may hold only Park_Max_Per_Client parked
	 * reads, well below a v4.1 session's slots, so its metadata and
	 * resident reads never queue behind its own parked reads.
	 */
	cslot = addr_hash(&a->id) % OMNI_CLIENT_SLOTS;
	if (__atomic_add_fetch(&C.client_parked[cslot], 1, __ATOMIC_RELAXED) >
	    ccap) {
		static struct omni_rl rl_ccap;
		uint32_t d;

		__atomic_sub_fetch(&C.client_parked[cslot], 1,
				   __ATOMIC_RELAXED);
		__atomic_sub_fetch(&a->exp->parked, 1, __ATOMIC_RELAXED);
		if (omni_rl_ok(&rl_ccap, 1000, &d))
			LogWarn(COMPONENT_FSAL,
				"FSAL_OMNI: a client has %u parked reads (the per-client cap); this read answers DELAY (%u similar suppressed)",
				ccap, d);
		return false;
	}
	if ((tag >> 48) == 0x0F5A)
		start = tag & 0xFFFFFFFFFFFFULL;
	else
		ra->cbi = (void *)(((uintptr_t)0x0F5A << 48) |
				   (now & 0xFFFFFFFFFFFFULL));
	if (C.park_ms_max && park_ms > C.park_ms_max)
		park_ms = C.park_ms_max;

	p = gsh_calloc(1, sizeof(*p), MEM_COMP_FSAL);
	p->obj = a->park->obj;
	p->read_arg = ra;
	p->done_cb = a->park->done_cb;
	p->caller_arg = a->park->caller_arg;
	p->gexp = op_ctx->ctx_export;
	p->exp = a->exp;
	p->end = a->off + a->len;
	p->deadline_ms = start + park_ms;
	p->cslot = cslot;
	p->async_flags = a->park->async_flags;

	pthread_mutex_lock(&C.park_lock);
	p->pend = e;
	p->enext = e->parked;
	e->parked = p;
	p->gprev = C.park_tail;
	if (C.park_tail)
		C.park_tail->gnext = p;
	else
		C.park_head = p;
	C.park_tail = p;
	pthread_mutex_unlock(&C.park_lock);
	return true;
}

/* Undo a park whose READ_INTENT could not be sent. Returns false if the
 * park was already completed by the socket thread (its done_cb is on the
 * way, so the caller must not answer too).
 */
static bool park_undo(struct omni_ask *a)
{
	struct omni_park *p;
	bool found = false;

	pthread_mutex_lock(&C.park_lock);
	for (p = C.park_head; p; p = p->gnext) {
		if (p->read_arg == a->park->read_arg) {
			park_unlink(p);
			__atomic_sub_fetch(&a->exp->parked, 1,
					   __ATOMIC_RELAXED);
			__atomic_sub_fetch(&C.client_parked[p->cslot], 1,
					   __ATOMIC_RELAXED);
			gsh_free(p, MEM_COMP_FSAL);
			found = true;
			break;
		}
	}
	pthread_mutex_unlock(&C.park_lock);
	return found;
}

/* ---- the decision (contract 6.1, steps 3-5) -------------------------- */

enum omni_gres omni_decide(struct omni_ask *a, fsal_status_t *st)
{
	struct omni_pkey k;
	struct omni_shard *s;
	struct omni_pend *e;
	uint32_t bkt;
	uint64_t now = omni_now_ms();
	bool send = false, parked = false;
	uint8_t ans = omni_policy_default_dec(a->exp->policy);
	bool want_park, failed = false;
	int rc;

	if (!omni_conn_connected()) {
		log_closed(a, "daemon not connected");
		*st = omni_policy_default(a->exp);
		return OMNI_G_ANSWER;
	}

	want_park = can_park(a);
	pkey_of(&a->obj, a->m.version_id, &k);
	s = shard_of(&k, &bkt);
	pthread_mutex_lock(&s->mu);
	e = pend_find(s, bkt, &k);
	if (e && e->expires_ms > now) {
		/* A valid answer: from memory, plus one READ_INTENT per new
		 * requester so every waiting person gets a card.
		 */
		ans = e->answer;
		send = requester_new(e, &a->id);
		/* M5: a read waiting for another chunk asks for it, even
		 * from a known requester
		 */
		if (chunk_new(e, a->chunk, now))
			send = true;
		if (want_park)
			parked = park_add(e, a);
		if (send)
			e->last_asked_ms = now;
		/* a failed fetch's EIO stands; DELAY otherwise */
		if (e->state == OMNI_RS_FAILED) {
			want_park = false;
			failed = true;
		}
	} else {
		if (e == NULL)
			e = pend_create(s, bkt, &k, false);
		if (e != NULL && now - e->last_asked_ms < T_ASK_GAP_MS &&
		    !chunk_new(e, a->chunk, now)) {
			/* asked under 100 ms ago (for this chunk, with a
			 * chunk map): answer the default
			 */
		} else {
			send = true;
			if (e != NULL) {
				e->state = 0;
				e->answer = ans;
				e->expires_ms =
					now + (C.answer_ttl_ms ?: 1000);
				e->last_asked_ms = now;
				e->nreq = 0;
				(void)requester_new(e, &a->id);
				(void)chunk_new(e, a->chunk, now);
			}
		}
		if (e != NULL && want_park)
			parked = park_add(e, a);
	}
	pthread_mutex_unlock(&s->mu);

	if (send) {
		rc = send_intent(a, &k, parked);
		if (rc != 0) {
			if (parked && !park_undo(a))
				return OMNI_G_PARKED;
			log_closed(a, rc == EBUSY ? "too many requests in flight"
						  : "READ_INTENT send failed");
			/* M5: an ONLINE read is a fetch of seconds: DELAY
			 * (the client retries), not the share's EIO
			 */
			*st = want_park || (online_fetch(a) && !failed)
				      ? fsalstat(ERR_FSAL_DELAY, EAGAIN)
				      : omni_policy_default(a->exp);
			return OMNI_G_ANSWER;
		}
	}
	if (parked)
		return OMNI_G_PARKED;
	/* M5: an ONLINE read that could have parked but found its export or
	 * its client at the cap answers DELAY, whatever the share's policy
	 * (contract 6.3): it is a fetch of seconds, not an archive recall. So
	 * does one past its park_ms that asks again before the daemon
	 * answered: only a failed fetch is EIO.
	 */
	if (want_park || (online_fetch(a) && !failed))
		ans = OMNI_DEC_DELAY;
	*st = dec_status(ans);
	return OMNI_G_ANSWER;
}

/* ---- fill fence (contract 6.1) ---------------------------------------- */

const char *omni_conn_fill_fence(const struct omni_obj_ref *obj,
				 uint64_t version_id)
{
	struct omni_pkey k;
	struct omni_shard *s;
	struct omni_pend *e;
	uint32_t bkt;
	bool fetching;

	if (!omni_conn_connected())
		return "daemon not connected";
	if (__atomic_load_n(&C.resyncing, __ATOMIC_ACQUIRE))
		return "daemon resync in progress";
	pkey_of(obj, version_id, &k);
	s = shard_of(&k, &bkt);
	pthread_mutex_lock(&s->mu);
	e = pend_find(s, bkt, &k);
	/* A FETCHING entry counts until DONE, FAILED or CANCELLED replaces
	 * it, or a resync drops it, whatever its valid_ms: stricter.
	 */
	fetching = e != NULL && e->state == OMNI_RS_FETCHING;
	pthread_mutex_unlock(&s->mu);
	return fetching ? "the daemon is filling this file" : NULL;
}

/* ---- handling daemon messages (socket thread) ------------------------ */

static void on_decision(struct omni_msg *m)
{
	struct omni_req *r = NULL, **pp;
	struct omni_shard *s;
	struct omni_pend *e;
	uint32_t bkt;
	uint64_t now = omni_now_ms(), ttl;
	uint8_t d = m->u.decision.decision;

	pthread_mutex_lock(&C.req_mu);
	for (pp = &C.req[m->hdr.req_id % OMNI_REQ_BUCKETS]; *pp;
	     pp = &(*pp)->next) {
		if ((*pp)->req_id == m->hdr.req_id) {
			r = *pp;
			*pp = r->next;
			break;
		}
	}
	pthread_mutex_unlock(&C.req_mu);
	if (r == NULL)
		return; /* late reply to a forgotten request: dropped */
	__atomic_sub_fetch(&C.inflight, 1, __ATOMIC_ACQ_REL);

	s = shard_of(&r->k, &bkt);
	pthread_mutex_lock(&s->mu);
	e = pend_find(s, bkt, &r->k);
	if (e == NULL)
		goto out;
	ttl = m->u.decision.ttl_ms ?: (C.answer_ttl_ms ?: 1000);
	e->recall_id = m->u.decision.recall_id;
	e->resident = m->u.decision.resident_bytes;
	pthread_mutex_lock(&C.park_lock);
	switch (d) {
	case OMNI_DEC_RECHECK:
		/* Never permission by itself: parked reads resume, and the
		 * worker re-reads the marker before serving any byte.
		 */
		park_finish_entry(e, PK_RESUME, UINT64_MAX);
		e->expires_ms = now + ttl;
		break;
	case OMNI_DEC_DELAY:
	case OMNI_DEC_EIO:
		e->answer = d;
		e->expires_ms = now + ttl;
		park_finish_entry(e, d == OMNI_DEC_EIO ? PK_EIO : PK_DELAY,
				  UINT64_MAX);
		break;
	case OMNI_DEC_PARK:
		/* keep the parked reads; reads that cannot park get DELAY */
		e->answer = OMNI_DEC_DELAY;
		e->expires_ms = now + ttl;
		break;
	default:
		/* unknown decision code: policy default */
		park_finish_entry(e, PK_DEFAULT, UINT64_MAX);
		e->expires_ms = now + ttl;
		break;
	}
	pthread_mutex_unlock(&C.park_lock);
out:
	pthread_mutex_unlock(&s->mu);
	gsh_free(r, MEM_COMP_FSAL);
}

static void on_recall_state(struct omni_msg *m)
{
	struct omni_pkey k;
	struct omni_shard *s;
	struct omni_pend *e;
	uint32_t bkt;
	uint64_t now = omni_now_ms();
	uint8_t st = m->u.recall.state, ans = m->u.recall.answer;

	pkey_of(&m->u.recall.obj, m->u.recall.version_id, &k);
	s = shard_of(&k, &bkt);
	pthread_mutex_lock(&s->mu);
	e = pend_find(s, bkt, &k);
	if (e == NULL) {
		if (st == OMNI_RS_DONE || st == OMNI_RS_CANCELLED)
			goto out;
		e = pend_create(s, bkt, &k, st == OMNI_RS_FETCHING);
		if (e == NULL)
			goto out;
	}
	if (ans != OMNI_DEC_DELAY && ans != OMNI_DEC_EIO)
		ans = OMNI_DEC_EIO; /* invalid: the stricter answer */
	e->state = st;
	e->answer = st == OMNI_RS_FAILED ? OMNI_DEC_EIO : ans;
	e->recall_id = m->u.recall.recall_id;
	e->resident = m->u.recall.resident_bytes;
	e->expires_ms = now + (m->u.recall.valid_ms ?: (C.answer_ttl_ms ?: 1000));
	e->resync_gen = C.resync_gen;

	pthread_mutex_lock(&C.park_lock);
	switch (st) {
	case OMNI_RS_DONE:
		park_finish_entry(e, PK_RESUME, UINT64_MAX);
		break;
	case OMNI_RS_FAILED:
		park_finish_entry(e, PK_EIO, UINT64_MAX);
		break;
	case OMNI_RS_CANCELLED:
		park_finish_entry(e, PK_DEFAULT, UINT64_MAX);
		break;
	default:
		/* Resume the parked reads the durable watermark now covers.
		 * M5, CHUNK_MAP: the daemon pushes FETCHING after each widening
		 * of the map, which this thread cannot read (no disk I/O here),
		 * so every parked read resumes and its worker re-checks the
		 * marker and the map; a read still not covered parks again,
		 * keeping its first park time (contract 6.3).
		 */
		if (m->u.recall.state == OMNI_RS_FETCHING &&
		    (C.features & OMNI_FEAT_CHUNK_MAP))
			park_finish_entry(e, PK_RESUME, UINT64_MAX);
		else
			park_finish_entry(e, PK_RESUME, e->resident);
		break;
	}
	pthread_mutex_unlock(&C.park_lock);
	/* DONE and CANCELLED drop the entry; FAILED keeps answering EIO for
	 * valid_ms.
	 */
	if (st == OMNI_RS_DONE || st == OMNI_RS_CANCELLED)
		pend_remove(s, bkt, e);
out:
	pthread_mutex_unlock(&s->mu);
}

/* Drop every pending entry (or, with only_gen, those not refreshed in the
 * resync), completing their parked reads fail-closed.
 */
static void pend_clear(bool only_stale)
{
	struct omni_pend *e, *nx;
	uint32_t i, b;

	for (i = 0; i < OMNI_SHARDS; i++) {
		struct omni_shard *s = &C.shard[i];

		pthread_mutex_lock(&s->mu);
		for (b = 0; b < OMNI_BUCKETS; b++) {
			for (e = s->b[b]; e; e = nx) {
				nx = e->next;
				if (only_stale && e->resync_gen == C.resync_gen)
					continue;
				if (only_stale && e->parked)
					continue; /* live readers: keep */
				pthread_mutex_lock(&C.park_lock);
				park_finish_entry(e, PK_DEFAULT, UINT64_MAX);
				pthread_mutex_unlock(&C.park_lock);
				pend_remove(s, b, e);
			}
		}
		pthread_mutex_unlock(&s->mu);
	}
}

/* Drop entries 60 s past their expiry (timer). Not entries with parked
 * reads, and not FETCHING ones (pend_droppable): a fill fence lasts until a
 * terminal state or a resync, whatever its valid_ms.
 */
static void pend_sweep(uint64_t now)
{
	struct omni_pend *e, *nx;
	uint32_t i, b;

	for (i = 0; i < OMNI_SHARDS; i++) {
		struct omni_shard *s = &C.shard[i];

		pthread_mutex_lock(&s->mu);
		for (b = 0; b < OMNI_BUCKETS; b++) {
			for (e = s->b[b]; e; e = nx) {
				nx = e->next;
				if (e->expires_ms + 60000 > now ||
				    !pend_droppable(e))
					continue;
				pend_remove(s, b, e);
			}
		}
		pthread_mutex_unlock(&s->mu);
	}
}

static void park_sweep(uint64_t now)
{
	struct omni_park *p, *nx;

	pthread_mutex_lock(&C.park_lock);
	for (p = C.park_head; p; p = nx) {
		nx = p->gnext;
		if (p->deadline_ms <= now) {
			park_unlink(p);
			park_finish(p, PK_DELAY);
		} else if (p->held && park_waiting(p)) {
			park_unlink(p);
			park_finish(p, PK_RESUME);
		}
	}
	pthread_mutex_unlock(&C.park_lock);
}

static void req_sweep(uint64_t now, bool all)
{
	struct omni_req *r, **pp;
	uint32_t i;

	pthread_mutex_lock(&C.req_mu);
	for (i = 0; i < OMNI_REQ_BUCKETS; i++) {
		for (pp = &C.req[i]; *pp;) {
			r = *pp;
			if (all || now - r->sent_ms >= T_REQUEST_MS) {
				/* No reply in 2 s: forget it; the entry
				 * answers the default until its ttl passes.
				 */
				*pp = r->next;
				gsh_free(r, MEM_COMP_FSAL);
				__atomic_sub_fetch(&C.inflight, 1,
						   __ATOMIC_ACQ_REL);
			} else {
				pp = &r->next;
			}
		}
	}
	pthread_mutex_unlock(&C.req_mu);
}

/* ---- connection lifecycle -------------------------------------------- */

static void disconnect(const char *why, int err)
{
	int fd;
	bool was_up = __atomic_load_n(&C.state, __ATOMIC_ACQUIRE) == CS_UP;
	uint32_t d;

	pthread_rwlock_wrlock(&C.fd_lock);
	fd = C.fd;
	C.fd = -1;
	__atomic_store_n(&C.state, CS_DOWN, __ATOMIC_RELEASE);
	__atomic_add_fetch(&C.conn_gen, 1, __ATOMIC_ACQ_REL);
	/* marker caches are not used while disconnected (contract 7) */
	omni_conn_bump_epoch();
	pthread_rwlock_unlock(&C.fd_lock);
	if (fd >= 0)
		close(fd);
	req_sweep(0, true);
	pend_clear(false);
	pthread_mutex_lock(&C.park_lock);
	park_finish_held();
	pthread_mutex_unlock(&C.park_lock);
	C.next_connect_ms = omni_now_ms() + C.backoff_ms;
	C.backoff_ms = C.backoff_ms ? C.backoff_ms * 2 : T_CONNECT_MIN_MS;
	if (C.backoff_ms > T_CONNECT_MAX_MS)
		C.backoff_ms = T_CONNECT_MAX_MS;
	if (was_up)
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: disconnected from the OmniFile daemon at %s (%s%s%s); non-local reads now FAIL CLOSED until it is back",
			C.sock_path, why, err ? ": " : "",
			err ? strerror(err) : "");
	else if (omni_rl_ok(&C.rl_conn, 10000, &d))
		LogWarn(COMPONENT_FSAL,
			"FSAL_OMNI: cannot reach the OmniFile daemon at %s (%s%s%s); non-local reads fail closed (%u similar suppressed)",
			C.sock_path, why, err ? ": " : "",
			err ? strerror(err) : "", d);
}

/* Close after sending ERROR (malformed, version, peer). */
static void fail_conn(uint32_t code, const char *msg)
{
	struct omni_buf *b = gsh_malloc(sizeof(*b), MEM_COMP_FSAL);

	omni_build_error(b, code, msg);
	if (C.fd >= 0)
		(void)send_raw(C.fd, b);
	gsh_free(b, MEM_COMP_FSAL);
	disconnect(msg, 0);
}

static void try_connect(uint64_t now)
{
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	struct ucred cred;
	socklen_t cl = sizeof(cred);
	int fd, sz = 1 << 20;
	struct omni_buf *b;

	fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (fd < 0) {
		disconnect("socket()", errno);
		return;
	}
	(void)setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz));
	snprintf(sa.sun_path, sizeof(sa.sun_path), "%s", C.sock_path);
	if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
		int err = errno;

		close(fd);
		disconnect("connect", err);
		return;
	}
	if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &cl) < 0 ||
	    cred.uid != 0) {
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: daemon socket %s is not owned by a root peer (uid %d); refusing it",
			C.sock_path, (int)cred.uid);
		b = gsh_malloc(sizeof(*b), MEM_COMP_FSAL);
		omni_build_error(b, OMNI_ERR_PEER, "peer not root");
		(void)send_raw(fd, b);
		gsh_free(b, MEM_COMP_FSAL);
		close(fd);
		disconnect("peer not allowed", EPERM);
		return;
	}
	b = gsh_malloc(sizeof(*b), MEM_COMP_FSAL);
	omni_build_hello(b, __atomic_add_fetch(&C.next_req, 1,
					       __ATOMIC_RELAXED),
			 OMNI_FSAL_FEATURES,
			 (uint32_t)getpid(), C.instance, GANESHA_VERSION,
			 OMNI_FSAL_BUILD);
	if (send_raw(fd, b) != 0) {
		int err = errno;

		gsh_free(b, MEM_COMP_FSAL);
		close(fd);
		disconnect("HELLO send", err);
		return;
	}
	gsh_free(b, MEM_COMP_FSAL);
	pthread_rwlock_wrlock(&C.fd_lock);
	C.fd = fd;
	__atomic_store_n(&C.state, CS_HELLO, __ATOMIC_RELEASE);
	pthread_rwlock_unlock(&C.fd_lock);
	C.hello_ms = now;
	C.last_rx_ms = now;
}

static void on_hello_ack(struct omni_msg *m)
{
	if (m->u.hello_ack.proto_major != OMNI_PROTO_MAJOR) {
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: daemon speaks protocol %u.%u, this FSAL %u.%u; refusing it (every non-local read fails closed)",
			m->u.hello_ack.proto_major, m->u.hello_ack.proto_minor,
			OMNI_PROTO_MAJOR, OMNI_PROTO_MINOR);
		fail_conn(OMNI_ERR_VERSION, "protocol major version mismatch");
		return;
	}
	C.features = m->u.hello_ack.features & OMNI_FSAL_FEATURES;
	/* M5: chunk maps only with a v0.2 daemon (contract 9). A v0.1 daemon
	 * keeps POPULATE_DIR, as in A's minor 1; its maps are ignored (prefix
	 * only), which is stricter.
	 */
	if (m->u.hello_ack.proto_minor < 2)
		C.features &= ~OMNI_FEAT_CHUNK_MAP;
	C.max_inflight = m->u.hello_ack.max_inflight ?: 4096;
	C.park_ms_max = m->u.hello_ack.park_ms_max ?: 10000;
	C.answer_ttl_ms = m->u.hello_ack.answer_ttl_ms ?: 1000;
	if (C.have_instance && C.daemon_instance != m->u.hello_ack.instance)
		LogEvent(COMPONENT_FSAL,
			 "FSAL_OMNI: daemon restarted (instance %" PRIx64
			 " -> %" PRIx64 "); pending table cleared",
			 C.daemon_instance, m->u.hello_ack.instance);
	C.daemon_instance = m->u.hello_ack.instance;
	C.have_instance = true;
	pend_clear(false); /* empty already; a new instance must start clean */
	C.backoff_ms = 0;
	__atomic_add_fetch(&C.conn_gen, 1, __ATOMIC_ACQ_REL);
	omni_conn_bump_epoch();
	/* Until RESYNC_END the pending table does not yet hold the daemon's
	 * running fills, so truncates of partial files wait (fill fence).
	 */
	C.resync_gen++;
	__atomic_store_n(&C.resyncing, true, __ATOMIC_RELEASE);
	__atomic_store_n(&C.state, CS_UP, __ATOMIC_RELEASE);
	LogEvent(COMPONENT_FSAL,
		 "FSAL_OMNI: connected to the OmniFile daemon at %s (protocol %u.%u, features 0x%x, max_inflight %u, park_ms_max %u, answer_ttl_ms %u)",
		 C.sock_path, m->u.hello_ack.proto_major,
		 m->u.hello_ack.proto_minor, C.features, C.max_inflight,
		 C.park_ms_max, C.answer_ttl_ms);
}

static void on_invalidate(struct omni_msg *m)
{
	struct omni_job *j = gsh_calloc(1, sizeof(*j), MEM_COMP_FSAL);

	j->kind = JOB_INVAL;
	j->obj = m->u.inval.obj;
	j->what = m->u.inval.what;
	j->flags = m->u.inval.flags;
	j->req_id = m->hdr.req_id;
	j->seq = m->u.inval.inval_seq;
	j->epoch = __atomic_load_n(&C.conn_gen, __ATOMIC_ACQUIRE);
	q_push(&C.inval_q, j);
}

static void handle_msg(const uint8_t *p, size_t n)
{
	struct omni_msg m;
	struct omni_buf b;
	uint32_t d;
	int st = __atomic_load_n(&C.state, __ATOMIC_ACQUIRE);

	if (omni_parse(p, n, &m) != 0) {
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: malformed message from the daemon (type 0x%04x, %zu bytes); closing",
			n >= 2 ? omni_ld16(p) : 0, n);
		fail_conn(OMNI_ERR_MALFORMED, "malformed message");
		return;
	}
	if (!m.known) {
		/* unknown request: ERROR 2, stay up; unknown notification:
		 * ignore. Requests carry a non-zero req_id.
		 */
		if (!(m.hdr.type & OMNI_REPLY_BIT) && m.hdr.req_id != 0) {
			omni_build_error(&b, OMNI_ERR_UNKNOWN_REQUEST,
					 "unknown request type");
			(void)send_raw(C.fd, &b);
		}
		if (omni_rl_ok(&C.rl_unknown, 10000, &d))
			LogInfo(COMPONENT_FSAL,
				"FSAL_OMNI: ignoring unknown message type 0x%04x (%u similar suppressed)",
				m.hdr.type, d);
		return;
	}
	if (st == CS_HELLO && m.hdr.type != OMNI_HELLO_ACK &&
	    m.hdr.type != OMNI_ERROR) {
		fail_conn(OMNI_ERR_MALFORMED, "message before HELLO_ACK");
		return;
	}
	switch (m.hdr.type) {
	case OMNI_HELLO_ACK:
		if (st != CS_HELLO) {
			fail_conn(OMNI_ERR_MALFORMED, "unexpected HELLO_ACK");
			return;
		}
		on_hello_ack(&m);
		break;
	case OMNI_READ_DECISION:
		on_decision(&m);
		break;
	case OMNI_POPULATE_RESULT:
		/* Nothing waits for it: the FSAL re-reads the directory's
		 * marker on the client's next retry.
		 */
		if (m.u.populate.result == OMNI_POP_EIO &&
		    omni_rl_ok(&C.rl_populate, 10000, &d))
			LogInfo(COMPONENT_FSAL,
				"FSAL_OMNI: the daemon could not fill a rewind directory (request %" PRIu64
				"); clients keep getting DELAY (%u similar suppressed)",
				m.hdr.req_id, d);
		break;
	case OMNI_RECALL_STATE:
		on_recall_state(&m);
		break;
	case OMNI_INVALIDATE:
		on_invalidate(&m);
		break;
	case OMNI_OPEN_STATE_QUERY:
		/* Answered at once from the open-state table: the daemon asks
		 * before it commits a file an NFSv4 client may still be
		 * writing (contract 5.5).
		 */
		omni_build_open_state(&b, m.hdr.req_id,
				      omni_open_writes(&m.u.open_q.obj));
		(void)send_raw(C.fd, &b);
		break;
	case OMNI_RESYNC_BEGIN:
		C.resync_gen++;
		__atomic_store_n(&C.resyncing, true, __ATOMIC_RELEASE);
		break;
	case OMNI_RESYNC_END:
		if (__atomic_load_n(&C.resyncing, __ATOMIC_ACQUIRE))
			pend_clear(true);
		__atomic_store_n(&C.resyncing, false, __ATOMIC_RELEASE);
		break;
	case OMNI_PING:
		omni_build_ping(&b, OMNI_PONG, m.hdr.req_id, m.u.ping.mono_ns);
		(void)send_raw(C.fd, &b);
		break;
	case OMNI_PONG:
		break;
	case OMNI_ERROR:
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: daemon sent ERROR %u (%s)", m.u.error.code,
			m.u.error.message);
		if (m.u.error.code != OMNI_ERR_UNKNOWN_REQUEST)
			disconnect("daemon ERROR", 0);
		break;
	default:
		break;
	}
}

static void do_read(void)
{
	uint8_t buf[OMNI_MAX_MSG + 1];
	ssize_t n;
	int i;

	for (i = 0; i < 256 && C.fd >= 0; i++) {
		n = recv(C.fd, buf, sizeof(buf), MSG_DONTWAIT | MSG_TRUNC);
		if (n < 0) {
			if (errno == EAGAIN || errno == EWOULDBLOCK ||
			    errno == EINTR)
				return;
			disconnect("recv", errno);
			return;
		}
		if (n == 0) {
			disconnect("daemon closed the connection", 0);
			return;
		}
		C.last_rx_ms = omni_now_ms();
		if ((size_t)n > OMNI_MAX_MSG) {
			fail_conn(OMNI_ERR_MALFORMED, "message over 4096 bytes");
			return;
		}
		handle_msg(buf, (size_t)n);
	}
}

static void tick(uint64_t now)
{
	static uint64_t last_slow;
	int st = __atomic_load_n(&C.state, __ATOMIC_ACQUIRE);
	struct omni_buf b;

	if (st == CS_DOWN && now >= C.next_connect_ms)
		try_connect(now);
	st = __atomic_load_n(&C.state, __ATOMIC_ACQUIRE);
	if (st == CS_HELLO && now - C.hello_ms >= T_HELLO_MS) {
		uint32_t d;

		if (omni_rl_ok(&C.rl_hello, 10000, &d))
			LogWarn(COMPONENT_FSAL,
				"FSAL_OMNI: no HELLO_ACK within 1 s from the daemon at %s; closing and retrying, non-local reads fail closed (%u similar suppressed)",
				C.sock_path, d);
		disconnect("no HELLO_ACK within 1 s", ETIMEDOUT);
	}
	if (st == CS_UP) {
		if (now - C.last_rx_ms >= T_DEAD_MS) {
			disconnect("no traffic for 3 s", ETIMEDOUT);
		} else if (now - C.last_rx_ms >= T_PING_IDLE_MS &&
			   now - C.last_ping_ms >= T_PING_IDLE_MS) {
			omni_build_ping(&b, OMNI_PING,
					__atomic_add_fetch(&C.next_req, 1,
							   __ATOMIC_RELAXED),
					now_ns());
			(void)omni_conn_send(&b);
			C.last_ping_ms = now;
		}
	}
	req_sweep(now, false);
	park_sweep(now);
	if (now - last_slow >= 1000) {
		last_slow = now;
		pend_sweep(now);
		(void)omni_cap_check(NULL);
	}
}

static void *sock_thread(void *arg)
{
	struct pollfd pf[2];
	uint64_t junk;

	(void)arg;
	SetNameFunction("omni_sock");
	while (!__atomic_load_n(&C.stopping, __ATOMIC_ACQUIRE)) {
		int nfds = 1;

		pf[0].fd = C.evfd;
		pf[0].events = POLLIN;
		if (C.fd >= 0) {
			pf[1].fd = C.fd;
			pf[1].events = POLLIN;
			nfds = 2;
		}
		if (poll(pf, nfds, T_TICK_MS) > 0) {
			if (pf[0].revents & POLLIN)
				(void)!read(C.evfd, &junk, sizeof(junk));
			if (nfds == 2 && (pf[1].revents & POLLIN))
				do_read();
			else if (nfds == 2 &&
				 (pf[1].revents & (POLLHUP | POLLERR)))
				disconnect("connection reset", ECONNRESET);
		}
		tick(omni_now_ms());
	}
	if (C.fd >= 0)
		disconnect("FSAL shutting down", 0);
	return NULL;
}

static void *compl_thread(void *arg)
{
	struct omni_job *j;

	(void)arg;
	SetNameFunction("omni_done");
	while ((j = q_pop(&C.done_q)) != NULL) {
		run_park_job(j);
		gsh_free(j, MEM_COMP_FSAL);
	}
	return NULL;
}

static void *inval_thread(void *arg)
{
	struct omni_job *j;
	struct omni_buf b;

	(void)arg;
	SetNameFunction("omni_inval");
	while ((j = q_pop(&C.inval_q)) != NULL) {
		omni_inval_apply(&j->obj, j->what);
		/* Ack only on the connection the INVALIDATE came on, and only
		 * now that the cached marker is gone and no pinned I/O can be
		 * using it.
		 */
		if ((j->flags & OMNI_INV_ACK_REQUIRED) &&
		    __atomic_load_n(&C.conn_gen, __ATOMIC_ACQUIRE) ==
			    j->epoch) {
			omni_build_inval_ack(&b, j->req_id, j->seq);
			(void)omni_conn_send(&b);
		}
		LogDebug(COMPONENT_FSAL,
			 "FSAL_OMNI: INVALIDATE ino=%" PRIu64
			 " what=0x%x seq=%" PRIu64 " acked=%d",
			 j->obj.ino, j->what, j->seq,
			 (j->flags & OMNI_INV_ACK_REQUIRED) != 0);
		gsh_free(j, MEM_COMP_FSAL);
	}
	return NULL;
}

void omni_conn_start(void)
{
	uint32_t i;

	pthread_mutex_lock(&C.life);
	if (C.started) {
		pthread_mutex_unlock(&C.life);
		return;
	}
	for (i = 0; i < OMNI_SHARDS; i++)
		pthread_mutex_init(&C.shard[i].mu, NULL);
	if (getrandom(&C.instance, sizeof(C.instance), 0) !=
	    sizeof(C.instance))
		C.instance = now_ns() ^ ((uint64_t)getpid() << 32);
	C.next_req = 0;
	C.evfd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
	C.stopping = false;
	if (pthread_create(&C.sock_thr, NULL, sock_thread, NULL) != 0)
		LogFatal(COMPONENT_FSAL,
			 "FSAL_OMNI: cannot start the daemon socket thread");
	if (pthread_create(&C.inval_thr, NULL, inval_thread, NULL) != 0)
		LogFatal(COMPONENT_FSAL,
			 "FSAL_OMNI: cannot start the invalidation thread");
	C.compl_thr = gsh_calloc(C.ncompl, sizeof(pthread_t), MEM_COMP_FSAL);
	for (i = 0; i < C.ncompl; i++)
		if (pthread_create(&C.compl_thr[i], NULL, compl_thread,
				   NULL) != 0)
			LogFatal(COMPONENT_FSAL,
				 "FSAL_OMNI: cannot start completion threads");
	C.started = true;
	LogEvent(COMPONENT_FSAL,
		 "FSAL_OMNI: guard started (daemon socket %s, %u completion threads)",
		 C.sock_path, C.ncompl);
	pthread_mutex_unlock(&C.life);
}

void omni_conn_stop(void)
{
	uint32_t i;

	pthread_mutex_lock(&C.life);
	if (!C.started) {
		pthread_mutex_unlock(&C.life);
		return;
	}
	__atomic_store_n(&C.stopping, true, __ATOMIC_RELEASE);
	wake();
	pthread_join(C.sock_thr, NULL);
	pthread_mutex_lock(&C.done_q.mu);
	pthread_cond_broadcast(&C.done_q.cv);
	pthread_mutex_unlock(&C.done_q.mu);
	pthread_mutex_lock(&C.inval_q.mu);
	pthread_cond_broadcast(&C.inval_q.cv);
	pthread_mutex_unlock(&C.inval_q.mu);
	for (i = 0; i < C.ncompl; i++)
		pthread_join(C.compl_thr[i], NULL);
	pthread_join(C.inval_thr, NULL);
	gsh_free(C.compl_thr, MEM_COMP_FSAL);
	close(C.evfd);
	C.evfd = -1;
	C.started = false;
	pthread_mutex_unlock(&C.life);
}
