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
 * The stub guard: markers, live inodes, identity and the per-operation
 * decision (docs/contracts/fsal-socket.md section 6;
 * docs/design/fsal-namespace.md sections 5, 6 and 13).
 *
 * The marker is read without opening the file for I/O: open_by_handle_at
 * with O_PATH, then fgetxattr. FSAL_VFS's own xattr path opens O_RDWR, which
 * fails on chattr +i files and made them unreadable in the filer spike.
 */

#include "config.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <linux/capability.h>
#include <netinet/in.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/syscall.h>
#include <sys/xattr.h>

#include "fsal.h"
#include "fsal_convert.h"
#include "FSAL/fsal_commonlib.h"
#include "FSAL/fsal_localfs.h"
#include "nfs_proto_data.h"
#include "sal_data.h"
#include "omni_methods.h"

bool omni_cap_ok = true;

/* ---- CAP_SYS_ADMIN ---------------------------------------------------- */

/* trusted.* xattrs need CAP_SYS_ADMIN. Without it, fgetxattr answers
 * ENODATA ("no marker") for every file, which would make every stub look
 * like an ordinary local file whose holes read as zeros. So without the
 * capability nothing is trusted: every regular-file read and write on an
 * OMNI export fails closed, and this is logged at CRIT.
 */
bool omni_cap_check(const char *when)
{
	static struct omni_rl rl;
	struct __user_cap_header_struct hdr = {
		.version = _LINUX_CAPABILITY_VERSION_3, .pid = 0
	};
	struct __user_cap_data_struct data[2];
	bool ok, was;
	uint32_t d;

	memset(data, 0, sizeof(data));
	if (syscall(SYS_capget, &hdr, data) != 0)
		ok = false;
	else
		ok = (data[CAP_SYS_ADMIN / 32].effective &
		      (1u << (CAP_SYS_ADMIN % 32))) != 0;
	was = __atomic_exchange_n(&omni_cap_ok, ok, __ATOMIC_ACQ_REL);
	if (!ok && (when != NULL || was || omni_rl_ok(&rl, 60000, &d)))
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: ganesha.nfsd does NOT have CAP_SYS_ADMIN%s%s. The trusted.omnifile markers cannot be read, so EVERY read and write of a regular file on an OMNI export FAILS CLOSED (EIO for failfast, DELAY for wait). Run Ganesha with CAP_SYS_ADMIN (fsal/systemd/nfs-ganesha-omni.service).",
			when ? " at " : "", when ? when : "");
	else if (ok && !was)
		LogEvent(COMPONENT_FSAL,
			 "FSAL_OMNI: CAP_SYS_ADMIN is present; markers readable");
	return ok;
}

/* ---- mounts ------------------------------------------------------------ */

struct omni_mount {
	struct omni_mount *next;
	struct fsal_filesystem *fs;
	int fd; /* the open_by_handle_at mount fd (O_RDONLY: O_PATH is EBADF) */
	uint64_t fsid; /* statfs f_fsid as u64 (contract 4.1) */
};

static pthread_mutex_t mnt_mu = PTHREAD_MUTEX_INITIALIZER;
static struct omni_mount *mnts;

static struct omni_mount *mount_of(struct fsal_filesystem *fs)
{
	struct omni_mount *m;
	struct statfs sfs;
	int fd;

	if (fs == NULL || fs->path == NULL)
		return NULL;
	pthread_mutex_lock(&mnt_mu);
	for (m = mnts; m; m = m->next)
		if (m->fs == fs)
			goto out;
	fd = open(fs->path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0 || fstatfs(fd, &sfs) != 0) {
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: cannot open filesystem %s for marker reads: %s; its files fail closed",
			fs->path, strerror(errno));
		if (fd >= 0)
			close(fd);
		goto out;
	}
	/* Ganesha is built with ENABLE_VFS_POSIX_ACL (fsal/build/Containerfile):
	 * on a filesystem without POSIX ACL support FSAL_VFS then fails every
	 * GETATTR with NOTSUPP, so the export is unusable. Say why, loudly.
	 */
	if (fgetxattr(fd, "system.posix_acl_access", NULL, 0) < 0 &&
	    errno == EOPNOTSUPP)
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: filesystem %s has no POSIX ACL support; with Ganesha's VFS POSIX ACL mapping every GETATTR on it fails (NOTSUPP). Mount it with ACLs (xfs: CONFIG_XFS_POSIX_ACL)",
			fs->path);
	m = gsh_calloc(1, sizeof(*m), MEM_COMP_FSAL);
	m->fs = fs;
	m->fd = fd;
	memcpy(&m->fsid, &sfs.f_fsid, sizeof(m->fsid));
	m->next = mnts;
	mnts = m;
	LogInfo(COMPONENT_FSAL, "FSAL_OMNI: filesystem %s fsid %016" PRIx64,
		fs->path, m->fsid);
out:
	pthread_mutex_unlock(&mnt_mu);
	return m;
}

static bool fsid_known(uint64_t fsid)
{
	struct omni_mount *m;
	bool found = false;

	pthread_mutex_lock(&mnt_mu);
	for (m = mnts; m && !found; m = m->next)
		found = m->fsid == fsid;
	pthread_mutex_unlock(&mnt_mu);
	return found;
}

/* ---- live inode table (contract 6.5) ----------------------------------- */

#define INO_SHARDS 64
#define INO_BUCKETS 4096

static struct {
	pthread_mutex_t mu;
	struct omni_inode *b[INO_BUCKETS];
} itab[INO_SHARDS];
static pthread_once_t itab_once = PTHREAD_ONCE_INIT;

static void itab_init(void)
{
	int i;

	for (i = 0; i < INO_SHARDS; i++)
		pthread_mutex_init(&itab[i].mu, NULL);
}

static uint32_t ino_hash(uint64_t fsid, uint64_t ino)
{
	uint64_t h = (fsid ^ (ino * 0x9E3779B97F4A7C15ULL));

	return (uint32_t)(h ^ (h >> 29));
}

/* Decode FSAL_VFS's handle key (FSAL/FSAL_VFS/os/linux/handle_syscalls.c):
 * [flags|fsid type] [fsid] [kernel handle type: 1, 2 or 4 bytes]
 * [kernel handle]. Returns false if it cannot be decoded.
 */
#define VFS_HANDLE_TYPE_8 0x40
#define VFS_HANDLE_TYPE_16 0x80
#define VFS_HANDLE_TYPE_32 0xC0
#define VFS_HANDLE_TYPE_MASK 0xC0
#define VFS_HANDLE_DUMMY 0x20

static bool decode_vfs_key(const uint8_t *k, size_t len, int *type,
			   uint8_t *kh, uint32_t *khlen, uint32_t *gen)
{
	size_t cur;
	int fsz;
	int16_t t16;
	int32_t t32;

	if (len < 2 || (k[0] & VFS_HANDLE_DUMMY))
		return false;
	fsz = sizeof_fsid((enum fsid_type)(k[0] & 0x1F));
	if (fsz < 0)
		return false;
	cur = 1 + fsz;
	switch (k[0] & VFS_HANDLE_TYPE_MASK) {
	case VFS_HANDLE_TYPE_8:
		if (cur + 1 > len)
			return false;
		*type = k[cur];
		cur += 1;
		break;
	case VFS_HANDLE_TYPE_16:
		if (cur + 2 > len)
			return false;
		memcpy(&t16, k + cur, 2);
		*type = t16;
		cur += 2;
		break;
	case VFS_HANDLE_TYPE_32:
		if (cur + 4 > len)
			return false;
		memcpy(&t32, k + cur, 4);
		*type = t32;
		cur += 4;
		break;
	default:
		return false;
	}
	if (len - cur == 0 || len - cur > 48)
		return false;
	*khlen = (uint32_t)(len - cur);
	memcpy(kh, k + cur, *khlen);
	/* The inode generation: FILEID_INO32_GEN(_PARENT) put it after a
	 * 32-bit inode; xfs's 64-bit-inode types (0x81, 0x82) after a 64-bit
	 * one. Other layouts: 0, and the daemon keys on file_id.
	 */
	*gen = 0;
	if ((*type == 1 || *type == 2) && *khlen >= 8)
		*gen = omni_ld32(kh + 4);
	else if ((*type == 0x81 || *type == 0x82) && *khlen >= 12)
		*gen = omni_ld32(kh + 8);
	return true;
}

struct omni_inode *omni_inode_get(struct omni_fsal_export *exp,
				  struct fsal_obj_handle *sub)
{
	struct gsh_buffdesc key = { 0 };
	struct omni_mount *mnt;
	struct omni_inode *ino, *n;
	uint8_t kh[48];
	uint32_t khlen = 0, gen = 0, h, bkt;
	int type = 0;
	bool ok;

	pthread_once(&itab_once, itab_init);
	sub->obj_ops->handle_to_key(sub, &key);
	ok = key.addr != NULL &&
	     decode_vfs_key(key.addr, key.len, &type, kh, &khlen, &gen);
	mnt = ok ? mount_of(sub->fs) : NULL;

	n = gsh_calloc(1, sizeof(*n), MEM_COMP_FSAL);
	n->fsid = mnt ? mnt->fsid : 0;
	n->ino = sub->fileid;
	n->gen = gen;
	n->mnt = mnt;
	n->kh_type = type;
	n->kh_len = khlen;
	memcpy(n->kh, kh, khlen);
	n->up = exp->export.up_ops;
	if (key.len <= sizeof(n->key)) {
		n->keylen = key.len;
		memcpy(n->key, key.addr, key.len);
	}
	if (mnt == NULL) {
		/* Undecodable: never cached, never matched; every check of a
		 * non-resident range fails closed (marker unreadable).
		 */
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: cannot decode the VFS handle of ino %" PRIu64
			"; reads of it fail closed",
			sub->fileid);
		pthread_mutex_init(&n->mu, NULL);
		pthread_cond_init(&n->cv, NULL);
		n->refs = 1;
		return n;
	}

	h = ino_hash(n->fsid, n->ino);
	bkt = (h >> 6) % INO_BUCKETS;
	pthread_mutex_lock(&itab[h % INO_SHARDS].mu);
	for (ino = itab[h % INO_SHARDS].b[bkt]; ino; ino = ino->next) {
		if (ino->fsid == n->fsid && ino->ino == n->ino &&
		    ino->gen == n->gen) {
			ino->refs++;
			pthread_mutex_unlock(&itab[h % INO_SHARDS].mu);
			gsh_free(n, MEM_COMP_FSAL);
			return ino;
		}
	}
	pthread_mutex_init(&n->mu, NULL);
	pthread_cond_init(&n->cv, NULL);
	n->refs = 1;
	n->next = itab[h % INO_SHARDS].b[bkt];
	itab[h % INO_SHARDS].b[bkt] = n;
	pthread_mutex_unlock(&itab[h % INO_SHARDS].mu);
	return n;
}

static void inode_free(struct omni_inode *ino)
{
	omni_map_put(ino->map);
	pthread_mutex_destroy(&ino->mu);
	pthread_cond_destroy(&ino->cv);
	gsh_free(ino, MEM_COMP_FSAL);
}

void omni_inode_put(struct omni_inode *ino)
{
	struct omni_inode **pp;
	uint32_t h, bkt;

	if (ino == NULL)
		return;
	if (ino->mnt == NULL) {
		if (__atomic_sub_fetch(&ino->refs, 1, __ATOMIC_ACQ_REL) == 0)
			inode_free(ino);
		return;
	}
	h = ino_hash(ino->fsid, ino->ino);
	bkt = (h >> 6) % INO_BUCKETS;
	pthread_mutex_lock(&itab[h % INO_SHARDS].mu);
	if (--ino->refs > 0) {
		pthread_mutex_unlock(&itab[h % INO_SHARDS].mu);
		return;
	}
	for (pp = &itab[h % INO_SHARDS].b[bkt]; *pp; pp = &(*pp)->next) {
		if (*pp == ino) {
			*pp = ino->next;
			break;
		}
	}
	pthread_mutex_unlock(&itab[h % INO_SHARDS].mu);
	inode_free(ino);
}

/* ---- pins -------------------------------------------------------------- */

void omni_pin(struct omni_inode *ino)
{
	pthread_mutex_lock(&ino->mu);
	/* An INVALIDATE waiting for pins to drain goes first: new I/O waits
	 * (for local I/O to finish, never for the daemon) and then sees the
	 * new marker.
	 */
	while (ino->inval_waiting)
		pthread_cond_wait(&ino->cv, &ino->mu);
	ino->pins++;
	pthread_mutex_unlock(&ino->mu);
}

void omni_unpin(struct omni_inode *ino)
{
	pthread_mutex_lock(&ino->mu);
	/* 0: an INVALIDATE may go; 1: a truncate to 0 may go (it holds one) */
	if (--ino->pins <= 1)
		pthread_cond_broadcast(&ino->cv);
	pthread_mutex_unlock(&ino->mu);
}

/* ---- markers ----------------------------------------------------------- */

static int open_opath(struct omni_inode *ino)
{
	struct {
		struct file_handle fh;
		unsigned char bytes[48];
	} h;

	if (ino->mnt == NULL) {
		errno = ESTALE;
		return -1;
	}
	h.fh.handle_bytes = ino->kh_len;
	h.fh.handle_type = ino->kh_type;
	memcpy(h.fh.f_handle, ino->kh, ino->kh_len);
	return open_by_handle_at(ino->mnt->fd, &h.fh, O_PATH | O_CLOEXEC);
}

/* f*xattr on an O_PATH fd needs a recent kernel; fall back to the
 * /proc/self/fd magic link, which reaches the same inode without an open.
 */
static ssize_t xattr_get(int fd, const char *name, void *buf, size_t len)
{
	char path[64];
	ssize_t n = fgetxattr(fd, name, buf, len);

	if (n >= 0 || errno != EBADF)
		return n;
	snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
	return getxattr(path, name, buf, len);
}

static int xattr_set(int fd, const char *name, const void *v, size_t len,
		     int flags)
{
	char path[64];
	int rc = fsetxattr(fd, name, v, len, flags);

	if (rc == 0 || errno != EBADF)
		return rc;
	snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
	return setxattr(path, name, v, len, flags);
}

static int xattr_remove(int fd, const char *name)
{
	char path[64];
	int rc = fremovexattr(fd, name);

	if (rc == 0 || errno != EBADF)
		return rc;
	snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
	return removexattr(path, name);
}

void omni_map_put(struct omni_map *map)
{
	if (map != NULL && __atomic_sub_fetch(&map->refs, 1, __ATOMIC_ACQ_REL) == 0)
		gsh_free(map, MEM_COMP_FSAL);
}

static struct omni_map *map_get(struct omni_map *map)
{
	if (map != NULL)
		__atomic_add_fetch(&map->refs, 1, __ATOMIC_RELAXED);
	return map;
}

/* Maps this size or smaller are cached with their marker; larger ones
 * (files over 256 GiB at 4 MiB chunks) are read again per operation, which
 * bounds the cache at about 8 KiB per live handle.
 */
#define OMNI_MAP_CACHE_MAX 8192

/* Read the chunk map of a marker that has the CHUNK_MAP flag. The marker
 * was read first: the daemon widens the map before the marker and narrows
 * both before it punches (with an acknowledged INVALIDATE that waits for
 * this operation's pin), so a map newer than the marker only ever names
 * more durable chunks, never fewer.
 */
static enum omni_mres map_read_disk(int fd, const struct omni_marker *m,
				    struct omni_map **mapp, uint32_t *reason,
				    int *err)
{
	size_t want = omni_map_len(m);
	struct omni_map *map;
	ssize_t n;

	if (want == 0) {
		*reason = OMNI_BM_LAYOUT;
		*err = EINVAL;
		return OMNI_M_BAD;
	}
	map = gsh_malloc(sizeof(*map) + want + 1, MEM_COMP_FSAL);
	n = xattr_get(fd, OMNI_MARKER_MAP_XATTR, map->bits, want + 1);
	if (n < 0) {
		*err = errno;
		gsh_free(map, MEM_COMP_FSAL);
		/* a flagged marker without its map is invalid */
		*reason = *err == ENODATA || *err == ERANGE ? OMNI_BM_LAYOUT
							   : OMNI_BM_READ;
		return OMNI_M_BAD;
	}
	if (!omni_map_valid(m, map->bits, (size_t)n)) {
		gsh_free(map, MEM_COMP_FSAL);
		*reason = OMNI_BM_LAYOUT;
		*err = EINVAL;
		return OMNI_M_BAD;
	}
	map->refs = 1;
	map->len = (uint32_t)n;
	*mapp = map;
	return OMNI_M_OK;
}

static enum omni_mres marker_read_disk(struct omni_inode *ino,
				       struct omni_marker *m,
				       struct omni_map **mapp,
				       uint32_t *reason, int *err)
{
	uint8_t buf[OMNI_MARKER_LEN + 1];
	struct stat st;
	ssize_t n;
	int fd, e;
	enum omni_bad_marker bm;
	enum omni_mres r;

	*mapp = NULL;
	if (!__atomic_load_n(&omni_cap_ok, __ATOMIC_ACQUIRE)) {
		*reason = OMNI_BM_READ;
		*err = EPERM;
		return OMNI_M_BAD;
	}
	fd = open_opath(ino);
	if (fd < 0) {
		*reason = OMNI_BM_READ;
		*err = errno;
		return OMNI_M_BAD;
	}
	n = xattr_get(fd, OMNI_MARKER_XATTR, buf, sizeof(buf));
	e = errno;
	if (n < 0) {
		close(fd);
		if (e == ENODATA)
			return OMNI_M_NONE;
		*reason = OMNI_BM_READ;
		*err = e;
		return OMNI_M_BAD;
	}
	if (fstat(fd, &st) != 0) {
		e = errno;
		close(fd);
		*reason = OMNI_BM_READ;
		*err = e;
		return OMNI_M_BAD;
	}
	bm = omni_marker_decode(buf, (size_t)n, m);
	if (bm != OMNI_BM_OK) {
		close(fd);
		*reason = bm;
		*err = EINVAL;
		return OMNI_M_BAD;
	}
	if ((uint64_t)st.st_size != m->size) {
		close(fd);
		/* RESIDENT: every byte was local when the marker was written
		 * and the daemon narrows to PARTIAL (acknowledged) before it
		 * removes any, so a local append or resize since cannot have
		 * made a byte remote: all local, up to the inode's size.
		 * PARTIAL: stale, fail closed.
		 */
		if (m->state == OMNI_MST_RESIDENT) {
			m->size = (uint64_t)st.st_size;
			m->resident_bytes = (uint64_t)st.st_size;
			m->flags &= ~OMNI_MF_CHUNK_MAP;
			return OMNI_M_OK;
		}
		*reason = OMNI_BM_SIZE;
		*err = 0;
		return OMNI_M_BAD;
	}
	/* M5: the chunk map counts only when both sides offered CHUNK_MAP.
	 * Without it a flagged marker is served by its prefix alone, which is
	 * stricter (contract 6.1). A RESIDENT file needs no map.
	 */
	r = OMNI_M_OK;
	if (m->state == OMNI_MST_PARTIAL && (m->flags & OMNI_MF_CHUNK_MAP) &&
	    (omni_conn_features() & OMNI_FEAT_CHUNK_MAP))
		r = map_read_disk(fd, m, mapp, reason, err);
	close(fd);
	return r;
}

enum omni_mres omni_marker_map_get(struct omni_inode *ino, bool use_cache,
				   struct omni_marker *m, struct omni_map **mapp,
				   bool *from_cache, uint32_t *bad_reason,
				   int *bad_errno)
{
	enum omni_mres r;
	uint64_t epoch = omni_conn_epoch(), gen;
	bool connected = omni_conn_connected();
	struct omni_map *map = NULL, *old = NULL;

	*from_cache = false;
	if (mapp)
		*mapp = NULL;
	pthread_mutex_lock(&ino->mu);
	if (use_cache && connected && ino->cached &&
	    ino->cache_epoch == epoch &&
	    /* a flagged marker whose map is too big to cache: from disk */
	    (ino->map != NULL || !ino->cached_managed ||
	     !(ino->m.flags & OMNI_MF_CHUNK_MAP) ||
	     ino->m.state != OMNI_MST_PARTIAL ||
	     !(omni_conn_features() & OMNI_FEAT_CHUNK_MAP))) {
		*from_cache = true;
		r = ino->cached_managed ? OMNI_M_OK : OMNI_M_NONE;
		*m = ino->m;
		if (mapp)
			*mapp = map_get(ino->map);
		pthread_mutex_unlock(&ino->mu);
		return r;
	}
	gen = ino->inval_gen;
	pthread_mutex_unlock(&ino->mu);
	r = marker_read_disk(ino, m, &map, bad_reason, bad_errno);
	/* Cache only while connected, for the epoch the read started in, and
	 * only if no INVALIDATE of this inode began since the read started
	 * (inval_gen): a reader that saw the old marker must never store it
	 * after the INVALIDATE dropped the cache, pinned or not.
	 */
	if (r != OMNI_M_BAD && connected && ino->mnt != NULL &&
	    (map == NULL || map->len <= OMNI_MAP_CACHE_MAX)) {
		pthread_mutex_lock(&ino->mu);
		if (ino->inval_waiting == 0 && ino->inval_gen == gen) {
			ino->cached = true;
			ino->cached_managed = r == OMNI_M_OK;
			ino->cache_epoch = epoch;
			ino->m = *m;
			old = ino->map;
			ino->map = map_get(map);
		}
		pthread_mutex_unlock(&ino->mu);
		omni_map_put(old);
	}
	if (mapp)
		*mapp = map;
	else
		omni_map_put(map);
	return r;
}

enum omni_mres omni_marker_get(struct omni_inode *ino, bool use_cache,
			       struct omni_marker *m, bool *from_cache,
			       uint32_t *bad_reason, int *bad_errno)
{
	return omni_marker_map_get(ino, use_cache, m, NULL, from_cache,
				   bad_reason, bad_errno);
}

void omni_obj_ref_of(struct omni_inode *ino, const struct omni_marker *m,
		     uint32_t export_id, struct omni_obj_ref *obj)
{
	obj->fsid = ino->fsid;
	obj->ino = ino->ino;
	obj->ino_gen = ino->gen;
	obj->export_id = export_id;
	obj->file_id = m ? m->file_id : 0;
}

/* ---- INVALIDATE ---------------------------------------------------------- */

static void inval_one(struct omni_inode *ino, uint32_t what)
{
	struct gsh_buffdesc key;
	struct omni_map *old;
	uint32_t up = 0;

	pthread_mutex_lock(&ino->mu);
	ino->inval_gen++;
	ino->inval_waiting++;
	while (ino->pins > 0)
		pthread_cond_wait(&ino->cv, &ino->mu);
	ino->cached = false;
	old = ino->map;
	ino->map = NULL;
	ino->inval_waiting--;
	pthread_cond_broadcast(&ino->cv);
	pthread_mutex_unlock(&ino->mu);
	omni_map_put(old);

	if (what & OMNI_INV_ATTRS)
		up |= FSAL_UP_INVALIDATE_ATTRS;
	if (what & OMNI_INV_CONTENT)
		up |= FSAL_UP_INVALIDATE_CONTENT;
	if (what & OMNI_INV_DIR)
		up |= FSAL_UP_INVALIDATE_DIR_POPULATED |
		      FSAL_UP_INVALIDATE_DIR_CHUNKS;
	if (up != 0 && ino->up != NULL && ino->keylen != 0) {
		key.addr = ino->key;
		key.len = ino->keylen;
		(void)ino->up->invalidate(ino->up, &key, up);
	}
}

/* An INVALIDATE the FSAL cannot match to an inode: every cached marker is
 * dropped (epoch bump), and every inode in the table is drained like a
 * matched one, so the ack still means "no I/O that passed the check under
 * an old marker is running" (contract 4.1 and 5.5). Returns the number of
 * inodes drained.
 */
static uint32_t inval_all_inodes(void)
{
	struct omni_inode *ino, **list;
	uint32_t s, b, n, i, total = 0;

	omni_conn_bump_epoch();
	for (s = 0; s < INO_SHARDS; s++) {
		pthread_mutex_lock(&itab[s].mu);
		n = 0;
		for (b = 0; b < INO_BUCKETS; b++)
			for (ino = itab[s].b[b]; ino; ino = ino->next)
				n++;
		if (n == 0) {
			pthread_mutex_unlock(&itab[s].mu);
			continue;
		}
		list = gsh_calloc(n, sizeof(*list), MEM_COMP_FSAL);
		n = 0;
		for (b = 0; b < INO_BUCKETS; b++) {
			for (ino = itab[s].b[b]; ino; ino = ino->next) {
				ino->refs++;
				list[n++] = ino;
			}
		}
		pthread_mutex_unlock(&itab[s].mu);
		for (i = 0; i < n; i++) {
			inval_one(list[i], 0); /* no up-call: no key */
			omni_inode_put(list[i]);
		}
		gsh_free(list, MEM_COMP_FSAL);
		total += n;
	}
	return total;
}

void omni_inval_apply(const struct omni_obj_ref *obj, uint32_t what)
{
	struct omni_inode *ino, *list[16];
	uint32_t h, bkt, n = 0, i;

	pthread_once(&itab_once, itab_init);
	if (!fsid_known(obj->fsid)) {
		/* Not a filesystem this FSAL knows by that fsid: the daemon
		 * and the FSAL disagree on the key. Drop every cached marker
		 * and drain every pin before the caller acks, rather than risk
		 * a stale marker or an in-flight read of punched bytes.
		 */
		n = inval_all_inodes();
		LogWarn(COMPONENT_FSAL,
			"FSAL_OMNI: INVALIDATE for unknown fsid %016" PRIx64
			" (ino %" PRIu64
			"): dropped every cached marker and drained pinned I/O on all %u live inodes before acking; the daemon's fsid does not match this FSAL's (contract 4.1)",
			obj->fsid, obj->ino, n);
		return;
	}
	h = ino_hash(obj->fsid, obj->ino);
	bkt = (h >> 6) % INO_BUCKETS;
	/* Every generation of the inode: a mismatched generation must never
	 * leave a stale marker cached.
	 */
	pthread_mutex_lock(&itab[h % INO_SHARDS].mu);
	for (ino = itab[h % INO_SHARDS].b[bkt]; ino && n < 16;
	     ino = ino->next) {
		if (ino->fsid == obj->fsid && ino->ino == obj->ino) {
			ino->refs++;
			list[n++] = ino;
		}
	}
	pthread_mutex_unlock(&itab[h % INO_SHARDS].mu);
	for (i = 0; i < n; i++) {
		inval_one(list[i], what);
		omni_inode_put(list[i]);
	}
}

/* ---- identity ------------------------------------------------------------ */

void omni_identity_from_ctx(struct omni_identity *id)
{
	memset(id, 0, sizeof(*id));
	id->uid = OMNI_UID_UNKNOWN;
	id->gid = OMNI_UID_UNKNOWN;
	id->origin = OMNI_ORIGIN_UNKNOWN;
	if (op_ctx == NULL)
		return;
	id->origin = OMNI_ORIGIN_REQUEST;
	id->uid = op_ctx->creds.caller_uid;
	id->gid = op_ctx->creds.caller_gid;
	id->nfs_vers = (uint8_t)op_ctx->nfs_vers;
	id->nfs_minor = (uint8_t)op_ctx->nfs_minorvers;
	if (op_ctx->nfs_reqdata != NULL) {
		struct svc_req *req = &op_ctx->nfs_reqdata->svc;

		switch (req->rq_msg.cb_cred.oa_flavor) {
		case AUTH_UNIX:
			id->sec = OMNI_SEC_SYS;
			break;
#ifdef _HAVE_GSSAPI
		case RPCSEC_GSS: {
			/* The service level is per request, in the RPCSEC_GSS
			 * credential (as libntirpc's svc_auth_gss.c reads it);
			 * the principal is the context's client name, which
			 * Ganesha maps to the uid (support/nfs_creds.c).
			 */
			const struct rpc_gss_cred *gc =
				(const struct rpc_gss_cred *)
					req->rq_msg.rq_cred_body;
			struct svc_rpc_gss_data *gd =
				req->rq_auth ? SVCAUTH_PRIVATE(req->rq_auth)
					     : NULL;

			id->sec = OMNI_SEC_KRB5;
			if (gc != NULL && gc->gc_svc == RPCSEC_GSS_SVC_INTEGRITY)
				id->sec = OMNI_SEC_KRB5I;
			else if (gc != NULL &&
				 gc->gc_svc == RPCSEC_GSS_SVC_PRIVACY)
				id->sec = OMNI_SEC_KRB5P;
			if (gd != NULL && gd->cname.value != NULL) {
				size_t n = gd->cname.length;

				if (n >= sizeof(id->principal))
					n = sizeof(id->principal) - 1;
				memcpy(id->principal, gd->cname.value, n);
				id->principal[n] = '\0';
			}
			break;
		}
#endif
		default:
			id->sec = OMNI_SEC_UNKNOWN;
			break;
		}
	}
	if (op_ctx->caller_addr != NULL) {
		const struct sockaddr_storage *ss =
			(const struct sockaddr_storage *)op_ctx->caller_addr;

		if (ss->ss_family == AF_INET) {
			const struct sockaddr_in *s4 = (const void *)ss;

			id->addr_family = 4;
			memcpy(id->addr, &s4->sin_addr, 4);
			id->port = ntohs(s4->sin_port);
		} else if (ss->ss_family == AF_INET6) {
			const struct sockaddr_in6 *s6 = (const void *)ss;

			if (IN6_IS_ADDR_V4MAPPED(&s6->sin6_addr)) {
				id->addr_family = 4;
				memcpy(id->addr, &s6->sin6_addr.s6_addr[12],
				       4);
			} else {
				id->addr_family = 6;
				memcpy(id->addr, &s6->sin6_addr, 16);
			}
			id->port = ntohs(s6->sin6_port);
		}
	}
	if (op_ctx->clientid != NULL)
		id->clientid = *op_ctx->clientid;
}

/* ---- open-state table (contract 6.4) ------------------------------------- */

#define ST_BUCKETS 4096

struct omni_st {
	struct omni_st *next;
	struct state_t *state;
	struct omni_identity id;
	fsal_openflags_t flags;
	/* An NFSv4 share state (an OPEN), its inode, and whether it is open
	 * for write now: OPEN_STATE_QUERY counts those (contract 5.5).
	 */
	bool share;
	bool write;
	uint64_t fsid, ino;
	uint32_t gen;
};

static pthread_mutex_t st_mu = PTHREAD_MUTEX_INITIALIZER;
static struct omni_st *st_tab[ST_BUCKETS];
static uint32_t st_hash(const void *p);

/* NFSv4 share states open for write on (fsid, ino, gen) (contract 5.5). A
 * file an NFSv4 client holds open for write is still being written, however
 * long it goes quiet: the daemon asks before committing it.
 *
 * A state is open for write from an open2 with FSAL_O_WRITE until an
 * OPEN_DOWNGRADE without it or its close2 (omni_state_reopened). Ganesha
 * calls close2 for every share state it frees (CLOSE, lease expiry;
 * nfs4_state.c), and close2 removes it here. NFSv3 has no open state and
 * counts 0.
 */
uint32_t omni_open_writes(const struct omni_obj_ref *obj)
{
	struct omni_st *e;
	uint32_t n = 0, b;

	pthread_mutex_lock(&st_mu);
	for (b = 0; b < ST_BUCKETS; b++)
		for (e = st_tab[b]; e; e = e->next)
			if (e->share && e->write && e->fsid == obj->fsid &&
			    e->ino == obj->ino && e->gen == obj->ino_gen)
				n++;
	pthread_mutex_unlock(&st_mu);
	return n;
}

/* Whether the NFS operation running on this thread is OPEN_DOWNGRADE. */
static bool op_is_open_downgrade(void)
{
	compound_data_t *data;

	if (op_ctx == NULL || op_ctx->nfs_vers != 4 || op_ctx->nfs_reqdata == NULL)
		return false;
	data = op_ctx->nfs_reqdata->proc_data;
	return data != NULL && data->opcode == NFS4_OP_OPEN_DOWNGRADE;
}

/* reopen2 of a state (contract 5.5). An OPEN upgrade calls it with only the
 * new OPEN's access, and ORs that into the state's share_access afterwards
 * (nfs4_op_open.c): a reader that joins a writer of the same open owner must
 * not drop the writer. Only an OPEN_DOWNGRADE sets the access it names.
 *
 * A failed upgrade is rolled back by Ganesha with another reopen2 of the
 * old flags (nfs4_op_open.c, around line 1236), but old_openflags is never
 * assigned there, so the rollback asks for no access at all. Not being a
 * downgrade, it leaves the write flag as it was: a failed upgrade to write
 * keeps the state counted until its close. That is the safe direction (a
 * commit waits for the CLOSE), never a writer dropped.
 */
void omni_state_reopened(struct state_t *state, fsal_openflags_t flags)
{
	struct omni_st *e;
	bool downgrade = op_is_open_downgrade();

	if (state == NULL)
		return;
	pthread_mutex_lock(&st_mu);
	for (e = st_tab[st_hash(state)]; e; e = e->next)
		if (e->state == state) {
			if (downgrade)
				e->write = (flags & FSAL_O_WRITE) != 0;
			else if (flags & FSAL_O_WRITE)
				e->write = true;
			break;
		}
	pthread_mutex_unlock(&st_mu);
}

static uint32_t st_hash(const void *p)
{
	uint64_t v = (uintptr_t)p;

	return (uint32_t)((v >> 4) ^ (v >> 20)) % ST_BUCKETS;
}

void omni_state_opened(struct state_t *state, struct omni_inode *ino,
		       fsal_openflags_t flags)
{
	struct omni_identity id;
	struct omni_st *e;
	uint32_t b;

	omni_identity_from_ctx(&id);
	if (ino != NULL) {
		pthread_mutex_lock(&ino->mu);
		ino->last_open = id;
		ino->last_open_ms = omni_now_ms();
		pthread_mutex_unlock(&ino->mu);
	}
	if (state == NULL)
		return;
	b = st_hash(state);
	pthread_mutex_lock(&st_mu);
	for (e = st_tab[b]; e; e = e->next)
		if (e->state == state)
			break;
	if (e == NULL) {
		e = gsh_calloc(1, sizeof(*e), MEM_COMP_FSAL);
		e->state = state;
		e->next = st_tab[b];
		st_tab[b] = e;
	}
	e->id = id;
	e->flags = flags;
	if (ino != NULL && state->state_type == STATE_TYPE_SHARE) {
		e->share = true;
		e->write = (flags & FSAL_O_WRITE) != 0;
		e->fsid = ino->fsid;
		e->ino = ino->ino;
		e->gen = ino->gen;
	}
	pthread_mutex_unlock(&st_mu);
}

bool omni_state_closed(struct state_t *state, struct omni_identity *id,
		       fsal_openflags_t *flags)
{
	struct omni_st *e = NULL, **pp;
	uint32_t b;

	if (state == NULL)
		return false;
	b = st_hash(state);
	pthread_mutex_lock(&st_mu);
	for (pp = &st_tab[b]; *pp; pp = &(*pp)->next) {
		if ((*pp)->state == state) {
			e = *pp;
			*pp = e->next;
			break;
		}
	}
	pthread_mutex_unlock(&st_mu);
	if (e == NULL)
		return false;
	*id = e->id;
	/* Open for write when it closed: an upgrade to write counts too, so
	 * its close sends WRITE_CLOSED (contract 5.4).
	 */
	*flags = e->share ? ((e->flags & ~FSAL_O_WRITE) |
			     (e->write ? FSAL_O_WRITE : 0))
			  : e->flags;
	gsh_free(e, MEM_COMP_FSAL);
	return true;
}

static bool st_lookup(struct state_t *state, struct omni_identity *id)
{
	struct omni_st *e;
	bool found = false;

	pthread_mutex_lock(&st_mu);
	for (e = st_tab[st_hash(state)]; e; e = e->next) {
		if (e->state == state) {
			*id = e->id;
			found = true;
			break;
		}
	}
	pthread_mutex_unlock(&st_mu);
	return found;
}

/* A lock stateid's open state, found by its "other" (states in the table
 * are still open, so reading their stateid_other is safe).
 */
static bool st_lookup_other(const char *other, struct omni_identity *id)
{
	struct omni_st *e;
	bool found = false;
	uint32_t i;

	pthread_mutex_lock(&st_mu);
	for (i = 0; i < ST_BUCKETS && !found; i++) {
		for (e = st_tab[i]; e; e = e->next) {
			if (memcmp(e->state->stateid_other, other,
				   OTHERSIZE) == 0) {
				*id = e->id;
				found = true;
				break;
			}
		}
	}
	pthread_mutex_unlock(&st_mu);
	return found;
}

void omni_identity_for_state(struct state_t *state, struct omni_inode *ino,
			     struct omni_identity *id)
{
	if (state != NULL) {
		if (st_lookup(state, id) ||
		    (state->state_type == STATE_TYPE_LOCK &&
		     st_lookup_other(state->state_data.lock.openstate_key,
				     id))) {
			id->origin = OMNI_ORIGIN_OPEN_STATEID;
			return;
		}
	}
	if (ino != NULL) {
		pthread_mutex_lock(&ino->mu);
		if (ino->last_open_ms != 0 &&
		    omni_now_ms() - ino->last_open_ms < 60000) {
			*id = ino->last_open;
			id->origin = OMNI_ORIGIN_LAST_OPENER;
			pthread_mutex_unlock(&ino->mu);
			return;
		}
		pthread_mutex_unlock(&ino->mu);
	}
	memset(id, 0, sizeof(*id));
	id->uid = OMNI_UID_UNKNOWN;
	id->gid = OMNI_UID_UNKNOWN;
	id->origin = OMNI_ORIGIN_UNKNOWN;
}

/* ---- the guard ------------------------------------------------------------ */

uint8_t omni_policy_default_dec(uint8_t policy)
{
	return policy == OMNI_POL_FAILFAST ? OMNI_DEC_EIO : OMNI_DEC_DELAY;
}

fsal_status_t omni_policy_default(const struct omni_fsal_export *exp)
{
	if (exp->policy == OMNI_POL_FAILFAST)
		return fsalstat(ERR_FSAL_IO, EIO);
	return fsalstat(ERR_FSAL_DELAY, EAGAIN);
}

static const char *op_name(uint8_t op)
{
	switch (op) {
	case OMNI_OP_READ:
		return "read";
	case OMNI_OP_READ_PLUS:
		return "read_plus";
	case OMNI_OP_COPY_SRC:
		return "copy";
	case OMNI_OP_WRITE:
		return "write";
	case OMNI_OP_SETATTR_SIZE:
		return "setattr-size";
	case OMNI_OP_FALLOCATE:
		return "fallocate";
	}
	return "?";
}

/* Does the marker cover what op needs? Reads need [off, off+len) clipped
 * to the size; everything that changes bytes or size needs the whole file
 * (R1 section 6). Local means below the resident prefix, or (M5, CHUNK_MAP
 * negotiated) in a chunk whose bit is set in the map.
 */
bool omni_covered(const struct omni_marker *m, const struct omni_map *map,
		  uint8_t op, uint64_t off, uint64_t len)
{
	uint64_t end;
	const uint8_t *bits = map ? map->bits : NULL;
	size_t blen = map ? map->len : 0;

	if (m->state == OMNI_MST_RESIDENT)
		return true;
	if (op == OMNI_OP_READ || op == OMNI_OP_READ_PLUS ||
	    op == OMNI_OP_COPY_SRC) {
		if (off >= m->size || len == 0)
			return true; /* EOF read */
		end = len > m->size - off ? m->size : off + len;
		return omni_range_local(m, bits, blen, off, end);
	}
	return omni_range_local(m, bits, blen, 0, m->size);
}

/* May a CACHED marker answer for this op? Only for a range it covers below
 * its size. A read at or after a PARTIAL marker's size is an EOF read only
 * if the size is still current: while the daemon replaces a file's content
 * (resize, then a new marker and an INVALIDATE that is not acknowledged)
 * the file can have grown with holes, so the marker is re-read from disk,
 * which checks the size against the inode.
 */
static bool cache_answers(const struct omni_marker *m,
			  const struct omni_map *map, uint8_t op, uint64_t off,
			  uint64_t len)
{
	if (!omni_covered(m, map, op, off, len))
		return false;
	if (m->state == OMNI_MST_PARTIAL &&
	    (op == OMNI_OP_READ || op == OMNI_OP_READ_PLUS ||
	     op == OMNI_OP_COPY_SRC) &&
	    off >= m->size)
		return false;
	return true;
}

static void report_bad(struct omni_fsal_export *exp, struct omni_inode *ino,
		       uint8_t op, uint32_t reason, int err)
{
	struct omni_obj_ref obj;
	struct omni_buf b;
	uint32_t d;

	if (!omni_cap_ok) {
		if (omni_rl_ok(&exp->rl_cap, 10000, &d))
			LogCrit(COMPONENT_FSAL,
				"FSAL_OMNI: %s of ino %" PRIu64
				" on export %u FAILS CLOSED: no CAP_SYS_ADMIN, markers unreadable (%u similar suppressed)",
				op_name(op), ino->ino, exp->export_id, d);
		return;
	}
	if (omni_rl_ok(&ino->rl_bad, 10000, &d)) {
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: bad marker on ino %" PRIu64
			" (reason %u, errno %d); %s fails closed (%u similar suppressed)",
			ino->ino, reason, err, op_name(op), d);
		omni_obj_ref_of(ino, NULL, exp->export_id, &obj);
		omni_build_bad_marker(&b, &obj, reason, (uint32_t)err);
		(void)omni_conn_send(&b);
	}
}

enum omni_gres omni_guard(struct omni_fsal_export *exp,
			  struct omni_fsal_obj_handle *h, uint8_t op,
			  uint64_t off, uint64_t len,
			  const struct omni_identity *id,
			  const struct omni_park_req *park, fsal_status_t *st)
{
	struct omni_inode *ino = h->ino;
	struct omni_marker m;
	struct omni_map *map = NULL;
	struct omni_ask a;
	enum omni_mres r;
	uint32_t reason = 0;
	int err = 0;
	bool cached;

	if (ino == NULL)
		return OMNI_G_PASS;

	/* Pin before reading the marker, so an INVALIDATE that narrows it
	 * waits until this operation is done with the bytes it allows.
	 */
	omni_pin(ino);
	r = omni_marker_map_get(ino, true, &m, &map, &cached, &reason, &err);
	if (r == OMNI_M_OK && cached && !cache_answers(&m, map, op, off, len)) {
		/* a widening needs no invalidation, and an EOF read must see
		 * the current size: re-read before asking or passing
		 */
		omni_map_put(map);
		r = omni_marker_map_get(ino, false, &m, &map, &cached, &reason,
					&err);
	}
	if (r == OMNI_M_NONE ||
	    (r == OMNI_M_OK && omni_covered(&m, map, op, off, len))) {
		omni_map_put(map);
		return OMNI_G_PASS;
	}
	omni_unpin(ino);

	if (r == OMNI_M_BAD) {
		report_bad(exp, ino, op, reason, err);
		*st = omni_policy_default(exp);
		return OMNI_G_ANSWER;
	}

	memset(&a, 0, sizeof(a));
	a.exp = exp;
	omni_obj_ref_of(ino, &m, exp->export_id, &a.obj);
	a.m = m;
	a.op = op;
	if (op == OMNI_OP_READ || op == OMNI_OP_READ_PLUS ||
	    op == OMNI_OP_COPY_SRC) {
		a.off = off;
		a.len = len;
	} else {
		a.off = 0;
		a.len = m.size;
	}
	a.chunk = UINT64_MAX;
	if ((omni_conn_features() & OMNI_FEAT_CHUNK_MAP) &&
	    (m.flags & OMNI_MF_ONLINE) && !(m.flags & OMNI_MF_ARCHIVED)) {
		/* the first chunk the range needs that is not local */
		uint8_t sh = m.chunk_shift
				     ? m.chunk_shift
				     : omni_chunk_shift_for(
					       m.size, OMNI_CHUNK_SHIFT_DEFAULT);
		uint64_t c = (a.off > m.resident_bytes ? a.off
						       : m.resident_bytes) >> sh;
		uint64_t last = a.len == 0 ? c
			: ((a.off + a.len > m.size ? m.size : a.off + a.len) -
			   1) >> sh;

		while (map != NULL && c < last && (c >> 3) < map->len &&
		       (map->bits[c >> 3] & (1u << (c & 7))))
			c++;
		a.chunk = c;
	}
	omni_map_put(map);
	if (id != NULL)
		a.id = *id;
	else
		omni_identity_from_ctx(&a.id);
	a.park = park;
	return omni_decide(&a, st);
}

/* ---- truncate to 0 ----------------------------------------------------- */

/* A file truncated to 0 holds no bytes that could be non-local, so its
 * marker goes (R1 section 5) and the daemon is told. The marker is removed
 * just BEFORE the truncate, not after: the smb-guard's fanotify group sees
 * the VFS truncate as a pre-content event, and for a marked file it would
 * wait for a fill (forever, after a failed one). In between, no other NFS
 * operation on the inode may check the marker (it would see an unmarked
 * file whose holes read as zeros), so begin holds back new pins and waits
 * for the others to drain, as an INVALIDATE does; it waits only for local
 * I/O, never for the daemon.
 */
static void trunc_release(struct omni_trunc *t)
{
	struct omni_inode *ino = t->ino;

	pthread_mutex_lock(&ino->mu);
	if (t->excl) {
		ino->inval_waiting--;
		t->excl = false;
	}
	pthread_cond_broadcast(&ino->cv);
	pthread_mutex_unlock(&ino->mu);
	omni_unpin(ino);
	pthread_mutex_lock(&ino->mu);
	ino->trunc_busy = false;
	pthread_cond_broadcast(&ino->cv);
	pthread_mutex_unlock(&ino->mu);
}

fsal_status_t omni_trunc0_begin(struct omni_fsal_export *exp,
				struct omni_inode *ino, struct omni_trunc *t)
{
	struct omni_obj_ref obj;
	struct omni_marker m;
	enum omni_mres r;
	const char *why;
	uint32_t reason = 0, d;
	int err = 0, fd;
	bool cached;

	memset(t, 0, sizeof(*t));
	t->ino = ino;
	t->len = -1; /* no marker seen */
	if (ino == NULL)
		return fsalstat(ERR_FSAL_NO_ERROR, 0);
	/* one truncate of the inode at a time (each holds a pin) */
	pthread_mutex_lock(&ino->mu);
	while (ino->trunc_busy)
		pthread_cond_wait(&ino->cv, &ino->mu);
	ino->trunc_busy = true;
	pthread_mutex_unlock(&ino->mu);
	/* The pin is held across the check, the marker removal and the
	 * truncate: the daemon's fence INVALIDATE (what=CONTENT) is acked only
	 * after it drains, so either the daemon sees the marker gone, or this
	 * check sees its FETCHING entry (applied before the INVALIDATE).
	 */
	omni_pin(ino);
	r = omni_marker_get(ino, false, &m, &cached, &reason, &err);
	if (r == OMNI_M_NONE)
		return fsalstat(ERR_FSAL_NO_ERROR, 0);
	if (r != OMNI_M_OK) {
		/* A marker that cannot be read: its content version is
		 * unknown, so no FETCHING entry can be ruled out. Fenced on
		 * the inode itself, whatever the version: DELAY.
		 */
		trunc_release(t);
		report_bad(exp, ino, OMNI_OP_SETATTR_SIZE, reason, err);
		if (omni_rl_ok(&exp->rl_closed, 1000, &d))
			LogWarn(COMPONENT_FSAL,
				"FSAL_OMNI: truncate to 0 of ino %" PRIu64
				" answers DELAY: its marker cannot be read (fill fence; %u similar suppressed)",
				ino->ino, d);
		return fsalstat(ERR_FSAL_DELAY, EAGAIN);
	}
	if (m.state == OMNI_MST_PARTIAL) {
		/* fenced while it may be being filled */
		omni_obj_ref_of(ino, &m, exp->export_id, &obj);
		why = omni_conn_fill_fence(&obj, m.version_id);
		if (why != NULL) {
			trunc_release(t);
			if (omni_rl_ok(&exp->rl_closed, 1000, &d))
				LogWarn(COMPONENT_FSAL,
					"FSAL_OMNI: truncate to 0 of partial ino %" PRIu64
					" (file_id %" PRIu64
					") answers DELAY: %s (fill fence; %u similar suppressed)",
					ino->ino, m.file_id, why, d);
			return fsalstat(ERR_FSAL_DELAY, EAGAIN);
		}
	}

	/* Alone on the inode: new pins wait, the others drain. */
	pthread_mutex_lock(&ino->mu);
	ino->inval_waiting++;
	t->excl = true;
	ino->inval_gen++;
	ino->cached = false;
	while (ino->pins > 1)
		pthread_cond_wait(&ino->cv, &ino->mu);
	pthread_mutex_unlock(&ino->mu);

	fd = open_opath(ino);
	if (fd < 0) {
		err = errno;
		trunc_release(t);
		if (omni_rl_ok(&exp->rl_closed, 1000, &d))
			LogCrit(COMPONENT_FSAL,
				"FSAL_OMNI: truncate to 0 of ino %" PRIu64
				" answers DELAY: cannot open it to remove its marker: %s (%u similar suppressed)",
				ino->ino, strerror(err), d);
		return fsalstat(ERR_FSAL_DELAY, EAGAIN);
	}
	t->len = xattr_get(fd, OMNI_MARKER_XATTR, t->marker, sizeof(t->marker));
	if (t->len >= 0 && xattr_remove(fd, OMNI_MARKER_XATTR) == 0) {
		t->unmarked = true;
	} else if (t->len >= 0) {
		/* e.g. an immutable file, whose truncate fails anyway: keep
		 * the marker (the old, safe state); end drops it if the
		 * truncate succeeds after all
		 */
		LogWarn(COMPONENT_FSAL,
			"FSAL_OMNI: cannot remove the marker of ino %" PRIu64
			" before its truncate to 0: %s",
			ino->ino, strerror(errno));
	}
	close(fd);
	t->m = m;
	return fsalstat(ERR_FSAL_NO_ERROR, 0);
}

void omni_trunc0_end(struct omni_fsal_export *exp, struct omni_trunc *t,
		     bool truncated)
{
	struct omni_inode *ino = t->ino;
	struct omni_identity id;
	struct omni_obj_ref obj;
	struct omni_buf b;
	int fd;

	if (ino == NULL)
		return;
	if (t->unmarked) {
		fd = open_opath(ino);
		if (truncated) {
			if (fd >= 0)
				(void)xattr_remove(fd, OMNI_MARKER_MAP_XATTR);
		} else if (fd < 0 ||
			   xattr_set(fd, OMNI_MARKER_XATTR, t->marker,
				     (size_t)t->len, XATTR_CREATE) != 0) {
			LogCrit(COMPONENT_FSAL,
				"FSAL_OMNI: the truncate to 0 of ino %" PRIu64
				" (file_id %" PRIu64
				") failed and its marker cannot be put back: %s; the daemon must re-mark it",
				ino->ino, t->m.file_id, strerror(errno));
		}
		if (fd >= 0)
			close(fd);
	}
	if (truncated && !t->unmarked && t->len >= 0)
		omni_after_truncate0(exp, ino); /* marker kept above */
	trunc_release(t);
	if (!t->unmarked || !truncated)
		return;
	LogInfo(COMPONENT_FSAL,
		"FSAL_OMNI: ino %" PRIu64 " (file_id %" PRIu64
		") truncated to 0 over NFS; marker removed first",
		ino->ino, t->m.file_id);
	if (omni_conn_features() & OMNI_FEAT_NS_HINT) {
		omni_identity_from_ctx(&id);
		omni_obj_ref_of(ino, &t->m, exp->export_id, &obj);
		omni_build_ns_hint(&b, OMNI_NS_SETATTR, OMNI_NS_WHAT_SIZE, NULL,
				   &obj, &id, id.principal, "", NULL);
		(void)omni_conn_send(&b);
	}
}

void omni_after_truncate0(struct omni_fsal_export *exp,
			  struct omni_inode *ino)
{
	struct omni_identity id;
	struct omni_obj_ref obj;
	struct omni_marker m;
	struct omni_buf b;
	struct stat sb;
	ssize_t n;
	uint8_t buf[OMNI_MARKER_LEN + 1];
	int fd;

	if (ino == NULL || !omni_cap_ok)
		return;
	fd = open_opath(ino);
	if (fd < 0)
		return;
	n = xattr_get(fd, OMNI_MARKER_XATTR, buf, sizeof(buf));
	if (n < 0 || fstat(fd, &sb) != 0 || sb.st_size != 0) {
		close(fd);
		return;
	}
	if (omni_marker_decode(buf, (size_t)n, &m) != OMNI_BM_OK)
		memset(&m, 0, sizeof(m));
	if (xattr_remove(fd, OMNI_MARKER_XATTR) != 0 && errno != ENODATA)
		LogCrit(COMPONENT_FSAL,
			"FSAL_OMNI: cannot remove the marker of truncated ino %" PRIu64
			": %s",
			ino->ino, strerror(errno));
	(void)xattr_remove(fd, OMNI_MARKER_MAP_XATTR);
	close(fd);

	/* inval_gen: a read that saw the marker before its removal must not
	 * cache it afterwards
	 */
	pthread_mutex_lock(&ino->mu);
	ino->inval_gen++;
	ino->cached = false;
	pthread_mutex_unlock(&ino->mu);

	LogInfo(COMPONENT_FSAL,
		"FSAL_OMNI: ino %" PRIu64 " (file_id %" PRIu64
		") truncated to 0 over NFS; marker removed",
		ino->ino, m.file_id);
	if (omni_conn_features() & OMNI_FEAT_NS_HINT) {
		omni_identity_from_ctx(&id);
		omni_obj_ref_of(ino, &m, exp->export_id, &obj);
		omni_build_ns_hint(&b, OMNI_NS_SETATTR, OMNI_NS_WHAT_SIZE, NULL,
				   &obj, &id, id.principal, "", NULL);
		(void)omni_conn_send(&b);
	}
}

/* ---- NS_HINT: who changed the namespace (contract 5.4) ---------------- */

/* The (fsid, ino, ino_gen) of a file's or a directory's guard inode; zeros
 * for anything else (the daemon then goes by the parent and the name).
 */
static void ns_ref(struct omni_fsal_export *exp,
		   struct omni_fsal_obj_handle *h, struct omni_obj_ref *obj)
{
	struct omni_inode *ino = NULL;

	memset(obj, 0, sizeof(*obj));
	if (h != NULL)
		ino = h->ino != NULL ? h->ino : h->dino;
	if (ino != NULL)
		omni_obj_ref_of(ino, NULL, exp->export_id, obj);
}

void omni_ns_hint(struct omni_fsal_export *exp, uint8_t op, uint8_t what,
		  struct omni_fsal_obj_handle *parent,
		  struct omni_fsal_obj_handle *target, const char *name,
		  const char *new_name)
{
	struct omni_obj_ref p, t;
	struct omni_identity id;
	struct omni_buf b;

	if (!(omni_conn_features() & OMNI_FEAT_NS_HINT))
		return;
	omni_identity_from_ctx(&id);
	ns_ref(exp, parent, &p);
	ns_ref(exp, target, &t);
	if (!omni_build_ns_hint(&b, op, what, &p, &t, &id, id.principal,
				name != NULL ? name : "", new_name))
		return;
	(void)omni_conn_send(&b);
}

/* ---- the protocol's async handshake (contract 6.3) ------------------- */

/* Ganesha V15.6 keeps the state of an async READ in a struct private to
 * Protocols/NFS/nfs3_read.c:88 and nfs4_op_read.c:59: the protocol clears
 * ASYNC_PROC_EXIT | ASYNC_PROC_DONE (nfs_proto_data.h) before every read2,
 * sets ASYNC_PROC_EXIT after read2 returns, and done_cb sets
 * ASYNC_PROC_DONE. A done_cb that comes after ASYNC_PROC_EXIT reschedules
 * the request with svc_resume, and the resume handler honours fsal_resume;
 * one that comes before it is completed inline by the code after read2,
 * which in the resume handlers ignores fsal_resume. These mirrors locate
 * that flags word from read_arg (the structs' last member, passed down
 * through MDCACHE unchanged). They are used only after the request itself
 * confirms the layout: v3, the request's proc_data is the struct and its
 * req is the request; v4, the compound's current op is READ or READ_PLUS,
 * its op_data is the struct and the struct points back at the compound. A
 * Ganesha bump that moves these members fails the checks, and then reads
 * do not park (they get the decision's answer), never a wrong handshake.
 */
struct omni_nfs3_read_data {
	void *res;
	struct svc_req *req;
	struct fsal_obj_handle *obj;
	int rc;
	uint32_t flags;
	struct fsal_io_arg read_arg;
};

struct omni_nfs4_read_data {
	void *res_READ4;
	void *owner;
	compound_data_t *data;
	struct fsal_obj_handle *obj;
	uint32_t flags;
#ifdef ENABLE_QOS
	uint32_t qos_flag;
#endif
	struct io_info info;
	struct fsal_io_arg read_arg;
};

/* Build-time guard for the two mirrors above (contract 6.3; fsal/README.md,
 * "Bumping Ganesha"). They copy structs private to Ganesha's
 * src/Protocols/NFS/nfs3_read.c (struct nfs3_read_data) and
 * src/Protocols/NFS/nfs4_op_read.c (struct nfs4_read_data), checked against
 * V15.6 only. A build against any other Ganesha stops here until someone
 * re-checks both structs in that version's source, updates the mirrors, and
 * either changes OMNI_READ_MIRRORS_CHECKED or builds with
 * OMNI_READ_MIRRORS_VERIFIED_FOR="<that version>" (make variable of the same
 * name), which must equal the Ganesha being built against.
 */
#define OMNI_READ_MIRRORS_CHECKED_MAJOR 15
#define OMNI_READ_MIRRORS_CHECKED "15.6"
#ifdef OMNI_READ_MIRRORS_VERIFIED_FOR
#define OMNI_READ_MIRRORS_FOR OMNI_READ_MIRRORS_VERIFIED_FOR
#else
#define OMNI_READ_MIRRORS_FOR OMNI_READ_MIRRORS_CHECKED
#if GANESHA_VERSION_MAJOR != OMNI_READ_MIRRORS_CHECKED_MAJOR
#error "FSAL_OMNI: omni_nfs3_read_data / omni_nfs4_read_data mirror Ganesha V15.6's private struct nfs3_read_data (src/Protocols/NFS/nfs3_read.c) and struct nfs4_read_data (src/Protocols/NFS/nfs4_op_read.c). Re-check both in this Ganesha's source, update the mirrors in fsal/FSAL_OMNI/omni_guard.c, then build with OMNI_READ_MIRRORS_VERIFIED_FOR=<version> (see fsal/README.md, Bumping Ganesha)."
#endif
#endif
/* The minor version too (GANESHA_VERSION_MINOR is ".6", which #if cannot
 * compare): GCC folds the comparison of the two literals.
 */
_Static_assert(__builtin_strcmp(GANESHA_VERSION, OMNI_READ_MIRRORS_FOR) == 0,
	       "FSAL_OMNI: the NFS READ mirrors in omni_guard.c were checked "
	       "against another Ganesha version than this one: re-check struct "
	       "nfs3_read_data (src/Protocols/NFS/nfs3_read.c) and struct "
	       "nfs4_read_data (src/Protocols/NFS/nfs4_op_read.c), then build "
	       "with OMNI_READ_MIRRORS_VERIFIED_FOR=<version> (fsal/README.md)");

/* What the public headers do expose: the types omni_read_async_flags reads
 * to confirm a mirror, and the handshake bits.
 */
#define OMNI_SAME_TYPE(a, b) __builtin_types_compatible_p(__typeof__(a), b)
_Static_assert(OMNI_SAME_TYPE(((nfs_request_t *)0)->proc_data, void *),
	       "nfs_request_t.proc_data is no longer void *");
_Static_assert(OMNI_SAME_TYPE(((nfs_request_t *)0)->svc, struct svc_req),
	       "nfs_request_t.svc is no longer a struct svc_req");
_Static_assert(OMNI_SAME_TYPE(((compound_data_t *)0)->op_data, void *),
	       "compound_data_t.op_data is no longer void *");
_Static_assert(OMNI_SAME_TYPE(((compound_data_t *)0)->opcode, nfs_opnum4),
	       "compound_data_t.opcode is no longer nfs_opnum4");
_Static_assert(ASYNC_PROC_EXIT != ASYNC_PROC_DONE &&
		       (ASYNC_PROC_EXIT & (ASYNC_PROC_EXIT - 1)) == 0 &&
		       (ASYNC_PROC_DONE & (ASYNC_PROC_DONE - 1)) == 0,
	       "ASYNC_PROC_EXIT and ASYNC_PROC_DONE are no longer two bits");
/* The mirrors' own shape, as copied: read_arg last (Ganesha's "must be
 * last"), flags a uint32_t right after the member before it.
 */
_Static_assert(offsetof(struct omni_nfs3_read_data, read_arg) +
			       sizeof(struct fsal_io_arg) ==
		       sizeof(struct omni_nfs3_read_data),
	       "omni_nfs3_read_data: read_arg must be the last member");
_Static_assert(offsetof(struct omni_nfs4_read_data, read_arg) +
			       sizeof(struct fsal_io_arg) ==
		       sizeof(struct omni_nfs4_read_data),
	       "omni_nfs4_read_data: read_arg must be the last member");
_Static_assert(offsetof(struct omni_nfs3_read_data, flags) ==
		       offsetof(struct omni_nfs3_read_data, rc) + sizeof(int),
	       "omni_nfs3_read_data: flags must follow rc");
_Static_assert(offsetof(struct omni_nfs4_read_data, flags) ==
		       offsetof(struct omni_nfs4_read_data, obj) +
			       sizeof(struct fsal_obj_handle *),
	       "omni_nfs4_read_data: flags must follow obj");
_Static_assert(OMNI_SAME_TYPE(((struct omni_nfs3_read_data *)0)->flags,
			      uint32_t) &&
		       OMNI_SAME_TYPE(((struct omni_nfs4_read_data *)0)->flags,
				      uint32_t),
	       "the mirrored flags words must be uint32_t");

uint32_t *omni_read_async_flags(struct fsal_io_arg *read_arg)
{
	nfs_request_t *rq;

	if (op_ctx == NULL || op_ctx->nfs_reqdata == NULL || read_arg == NULL)
		return NULL;
	rq = op_ctx->nfs_reqdata;
	if (rq->proc_data == NULL)
		return NULL;
	if (op_ctx->nfs_vers == 3) {
		struct omni_nfs3_read_data *d = container_of(
			read_arg, struct omni_nfs3_read_data, read_arg);

		if (rq->svc.rq_msg.cb_proc != NFSPROC3_READ ||
		    rq->proc_data != (void *)d || d->req != &rq->svc)
			return NULL;
		return &d->flags;
	}
	if (op_ctx->nfs_vers == 4) {
		compound_data_t *c = rq->proc_data;
		struct omni_nfs4_read_data *d = container_of(
			read_arg, struct omni_nfs4_read_data, read_arg);

		if ((c->opcode != NFS4_OP_READ &&
		     c->opcode != NFS4_OP_READ_PLUS) ||
		    c->op_data != (void *)d || d->data != c)
			return NULL;
		return &d->flags;
	}
	return NULL;
}
