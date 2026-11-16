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
 * Rewind trees over NFS (docs/design/fsal-namespace.md section 7).
 *
 * The daemon keeps each share's point-in-time trees at Snap_Dir
 * (<data>/.omni/snap/<share>), outside the share. Every directory and file
 * there carries a READONLY marker; a directory the daemon has not filled
 * yet also carries DIR_UNPOPULATED. Here:
 * - LOOKUP of the reserved name .snapshots in the share's root answers with
 *   Snap_Dir. READDIR never lists it.
 * - Every change to a READONLY object, or inside a READONLY directory, is
 *   ERR_FSAL_ROFS, before anything else looks at it (a write to a PARTIAL
 *   placeholder would otherwise start a recall, and a truncate would drop
 *   its marker).
 * - LOOKUP and READDIR in a DIR_UNPOPULATED directory send POPULATE_DIR and
 *   answer DELAY until the daemon clears the flag. Neither op is async in
 *   Ganesha, so the worker never waits for the daemon.
 *
 * A directory's marker is never answered from the cache when it has one:
 * the daemon clears DIR_UNPOPULATED when it has filled a directory, and
 * sets it again when it evicts one, without an INVALIDATE. Only "no marker"
 * (a live directory, which never becomes a rewind one) is cached. Most live
 * directories are never read at all: one found from a live directory is
 * live (omni_mark_live), so only handles rebuilt from the wire and those
 * reached through .snapshots read the marker.
 */

#include "config.h"

#include <errno.h>
#include <string.h>
#include <sys/stat.h>

#include "fsal.h"
#include "fsal_convert.h"
#include "FSAL/fsal_commonlib.h"
#include "FSAL/fsal_localfs.h"
#include "omni_methods.h"

/* How often one directory's POPULATE_DIR is re-sent while clients retry
 * DELAY (NFSv4 clients retry in about 0.1 s). The daemon answers
 * IN_PROGRESS to a duplicate; a re-send after a failure retries it.
 */
#define POPULATE_RESEND_MS 1000

enum omni_rw omni_rewind_of(struct omni_fsal_obj_handle *h)
{
	struct omni_inode *ino = h->ino != NULL ? h->ino : h->dino;
	struct omni_marker m;
	enum omni_mres r;
	uint32_t reason = 0;
	int err = 0;
	bool cached;

	if (ino == NULL ||
	    (h->dino != NULL && __atomic_load_n(&ino->dir_live, __ATOMIC_RELAXED)))
		return OMNI_RW_LIVE;
	r = omni_marker_get(ino, true, &m, &cached, &reason, &err);
	if (r == OMNI_M_OK && cached && h->dino != NULL)
		r = omni_marker_get(ino, false, &m, &cached, &reason, &err);
	if (r != OMNI_M_OK || !(m.flags & OMNI_MF_READONLY)) {
		/* An unreadable marker on a directory is not a reason to
		 * refuse the live share: a rewind directory read that way
		 * shows empty, never wrong bytes. Files fail closed in the
		 * guard as before.
		 */
		return OMNI_RW_LIVE;
	}
	if (h->dino != NULL && (m.flags & OMNI_MF_DIR_UNPOPULATED))
		return OMNI_RW_UNPOPULATED;
	return OMNI_RW_READONLY;
}

void omni_mark_live(struct fsal_obj_handle *obj)
{
	struct omni_fsal_obj_handle *h =
		container_of(obj, struct omni_fsal_obj_handle, obj_handle);

	if (h->dino != NULL)
		__atomic_store_n(&h->dino->dir_live, true, __ATOMIC_RELAXED);
}

bool omni_readonly(struct omni_fsal_obj_handle *h)
{
	return omni_rewind_of(h) != OMNI_RW_LIVE;
}

/* Is dir the root of this request's export? Compared by inode and device
 * with the export's path, so it holds for handles rebuilt from the wire.
 */
static bool export_root(struct omni_fsal_obj_handle *dir)
{
	struct fsal_obj_handle *sub = dir->sub_handle;
	struct stat st;
	fsal_dev_t dev;

	if (sub->type != DIRECTORY || sub->fs == NULL ||
	    op_ctx->ctx_fullpath == NULL)
		return false;
	if (stat(CTX_FULLPATH(op_ctx), &st) != 0)
		return false;
	dev = posix2fsal_devt(st.st_dev);
	return sub->fileid == (uint64_t)st.st_ino &&
	       sub->fs->dev.major == dev.major &&
	       sub->fs->dev.minor == dev.minor;
}

bool omni_snapshots_name(struct omni_fsal_export *exp,
			 struct omni_fsal_obj_handle *dir, const char *name)
{
	return exp->snap_dir != NULL && name != NULL &&
	       strcmp(name, OMNI_SNAPSHOTS) == 0 && export_root(dir);
}

/* A sub-FSAL handle by absolute path. FSAL_VFS's lookup_path fills only
 * the POSIX attributes, so the rest (the ACL) comes from getattrs, as a
 * LOOKUP's would.
 */
static fsal_status_t lookup_abs(struct omni_fsal_export *exp, const char *path,
				struct fsal_obj_handle **sub_handle,
				struct fsal_attrlist *attrs_out)
{
	struct fsal_export *sub = exp->export.sub_export;
	fsal_status_t st;

	*sub_handle = NULL;
	op_ctx->fsal_export = sub;
	st = sub->exp_ops.lookup_path(sub, path, sub_handle, NULL);
	if (!FSAL_IS_ERROR(st) && attrs_out != NULL) {
		st = (*sub_handle)->obj_ops->getattrs(*sub_handle, attrs_out);
		if (FSAL_IS_ERROR(st)) {
			(*sub_handle)->obj_ops->release(*sub_handle);
			*sub_handle = NULL;
		}
	}
	op_ctx->fsal_export = &exp->export;
	return st;
}

fsal_status_t omni_snapshots_lookup(struct omni_fsal_export *exp,
				    struct fsal_obj_handle **sub_handle,
				    struct fsal_attrlist *attrs_out)
{
	fsal_status_t st = lookup_abs(exp, exp->snap_dir, sub_handle,
				      attrs_out);

	if (FSAL_IS_ERROR(st) && st.major != ERR_FSAL_NOENT)
		LogInfo(COMPONENT_FSAL,
			"FSAL_OMNI: export %u: .snapshots at %s: %s",
			exp->export_id, exp->snap_dir, fsal_err_txt(st));
	return st;
}

fsal_status_t omni_rewind_parent(struct omni_fsal_export *exp,
				 struct fsal_obj_handle **sub_handle,
				 struct fsal_attrlist *attrs_out)
{
	struct fsal_obj_handle *up = *sub_handle;
	struct omni_fsal_obj_handle tmp = { .sub_handle = up };
	bool inside;

	if (up->type == DIRECTORY)
		tmp.dino = omni_inode_get(exp, up);
	inside = tmp.dino != NULL && omni_readonly(&tmp);
	omni_inode_put(tmp.dino);
	if (inside)
		return fsalstat(ERR_FSAL_NO_ERROR, 0);
	/* ".." of the top of a rewind tree: the share's root, never the
	 * private area above it.
	 */
	op_ctx->fsal_export = exp->export.sub_export;
	up->obj_ops->release(up);
	op_ctx->fsal_export = &exp->export;
	if (attrs_out != NULL)
		fsal_release_attrs(attrs_out);
	return lookup_abs(exp, CTX_FULLPATH(op_ctx), sub_handle, attrs_out);
}

fsal_status_t omni_populate(struct omni_fsal_export *exp,
			    struct omni_fsal_obj_handle *dir)
{
	struct omni_inode *ino = dir->dino;
	struct omni_identity id;
	struct omni_obj_ref obj;
	uint64_t now = omni_now_ms();
	bool send = false;
	uint32_t d;
	int rc;

	pthread_mutex_lock(&ino->mu);
	if (ino->populate_ms == 0 ||
	    now - ino->populate_ms >= POPULATE_RESEND_MS) {
		ino->populate_ms = now;
		send = true;
	}
	pthread_mutex_unlock(&ino->mu);
	if (!send)
		return fsalstat(ERR_FSAL_DELAY, 0);

	omni_obj_ref_of(ino, NULL, exp->export_id, &obj);
	omni_identity_from_ctx(&id);
	rc = omni_conn_populate(&obj, &id);
	if (rc != 0) {
		/* Not sent: no daemon, or one without POPULATE_DIR. The
		 * share's policy decides, as for a read nobody can answer.
		 */
		pthread_mutex_lock(&ino->mu);
		ino->populate_ms = 0;
		pthread_mutex_unlock(&ino->mu);
		if (omni_rl_ok(&exp->rl_rewind, 10000, &d))
			LogInfo(COMPONENT_FSAL,
				"FSAL_OMNI: export %u: cannot ask the daemon to fill rewind directory ino %" PRIu64
				" (%s; %u similar suppressed)",
				exp->export_id, ino->ino, strerror(rc), d);
		return omni_policy_default(exp);
	}
	return fsalstat(ERR_FSAL_DELAY, 0);
}

fsal_status_t omni_dir_change(struct omni_fsal_export *exp,
			      struct omni_fsal_obj_handle *dir,
			      const char *name)
{
	if (omni_readonly(dir) || omni_snapshots_name(exp, dir, name))
		return fsalstat(ERR_FSAL_ROFS, 0);
	return fsalstat(ERR_FSAL_NO_ERROR, 0);
}
