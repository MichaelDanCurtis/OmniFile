// SPDX-License-Identifier: LGPL-3.0-or-later
/*
 * vim:noexpandtab:shiftwidth=8:tabstop=8:
 *
 * FSAL_OMNI, part of OmniFile Gateway. Derived from NFS-Ganesha V15.6
 * src/FSAL/Stackable_FSALs/FSAL_NULL (renamed NULL -> OMNI).
 * Modifications Copyright (C) 2026 The OmniFile Gateway authors.
 *
 * Copyright (C) Panasas Inc., 2011
 * Author: Jim Lieb jlieb@panasas.com
 *
 * contributeur : Philippe DENIEL   philippe.deniel@cea.fr
 *                Thomas LEIBOVICI  thomas.leibovici@cea.fr
 *
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
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
 */

/* file.c
 * File I/O methods for OMNI module
 */

#include "config.h"

#include <assert.h>
#include "fsal.h"
#include "FSAL/access_check.h"
#include "fsal_convert.h"
#include <unistd.h>
#include <fcntl.h>
#include "FSAL/fsal_commonlib.h"
#include "omni_methods.h"

/**
 * @brief Callback arg for OMNI async callbacks
 *
 * OMNI needs to know what its object is related to the sub-FSAL's object.
 * This wraps the given callback arg with OMNI specific info
 */
struct omni_async_arg {
	struct fsal_obj_handle *obj_hdl; /**< OMNI's handle */
	fsal_async_cb cb; /**< Wrapped callback */
	void *cb_arg; /**< Wrapped callback data */
	struct omni_inode *pin; /**< Guard pin to drop once the I/O is done */
};

/**
 * @brief Callback for OMNI async calls
 *
 * Unstack, and call up.
 *
 * @param[in] obj		Object being acted on
 * @param[in] ret		Return status of call
 * @param[in] obj_data		Data for call
 * @param[in] caller_data	Data for caller
 */
void omni_async_cb(struct fsal_obj_handle *obj, fsal_status_t ret,
		   void *obj_data, void *caller_data)
{
	struct fsal_export *save_exp = op_ctx->fsal_export;
	struct omni_async_arg *arg = caller_data;

	(void)obj; /* the sub-FSAL's handle; we answer with our own */
	if (arg->pin != NULL)
		omni_unpin(arg->pin);
	op_ctx->fsal_export = save_exp->super_export;
	arg->cb(arg->obj_hdl, ret, obj_data, arg->cb_arg);
	op_ctx->fsal_export = save_exp;

	gsh_free(arg, MEM_COMP_FSAL);
}

/* omni_close
 * Close the file if it is still open.
 */

fsal_status_t omni_close(struct fsal_obj_handle *obj_hdl)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->close(handle->sub_handle);
	op_ctx->fsal_export = &export->export;

	return status;
}

fsal_status_t omni_open2(struct fsal_obj_handle *obj_hdl,
			   struct state_t *state, fsal_openflags_t openflags,
			   enum fsal_create_mode createmode, const char *name,
			   struct fsal_attrlist *attrs_in,
			   fsal_verifier_t verifier,
			   struct fsal_obj_handle **new_obj,
			   struct fsal_attrlist *attrs_out,
			   bool *caller_perm_check,
			   struct fsal_attrlist *parent_pre_attrs_out,
			   struct fsal_attrlist *parent_post_attrs_out)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);
	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	struct fsal_obj_handle *sub_handle = NULL;
	struct omni_fsal_obj_handle *target = NULL;
	struct omni_inode *tino = NULL; /* being truncated to 0 */
	struct omni_trunc tr = { 0 };
	bool tino_ref = false;
	bool writes = openflags & (FSAL_O_WRITE | FSAL_O_TRUNC);
	fsal_status_t status;

	/* Rewind trees are read-only (R1 section 7), checked before the
	 * truncate fence below would drop a placeholder's marker. By name, the
	 * directory decides: every placeholder lives in a READONLY one.
	 */
	if (name != NULL) {
		enum omni_rw rw = omni_rewind_of(handle);

		if (writes || createmode != FSAL_NO_CREATE) {
			status = omni_dir_change(export, handle, name);
			if (FSAL_IS_ERROR(status))
				return status;
		} else if (rw == OMNI_RW_UNPOPULATED) {
			/* Never "no such file" for a name not filled in
			 * yet. Ganesha V15.6's OPEN looks the name up first,
			 * and that LOOKUP answers DELAY already; this covers
			 * an OPEN that does not.
			 */
			return omni_populate(export, handle);
		}
	} else if (writes && omni_readonly(handle)) {
		return fsalstat(ERR_FSAL_ROFS, 0);
	}

	/* An open with truncate of an existing file is a truncate to 0: the
	 * fill fence applies (contract 6.1). MDCACHE opens an existing file
	 * by handle; by name only if its lookup missed, so a file created
	 * since (a placeholder linked in by the daemon) is looked up here.
	 */
	if (openflags & FSAL_O_TRUNC) {
		if (name == NULL) {
			tino = handle->ino;
		} else if (createmode < FSAL_GUARDED) {
			struct fsal_obj_handle *sub = NULL;

			op_ctx->fsal_export = export->export.sub_export;
			status = handle->sub_handle->obj_ops->lookup(
				handle->sub_handle, name, &sub, NULL);
			if (!FSAL_IS_ERROR(status) && sub != NULL &&
			    sub->type == REGULAR_FILE) {
				tino = omni_inode_get(export, sub);
				tino_ref = true;
			}
			if (sub != NULL)
				sub->obj_ops->release(sub);
			op_ctx->fsal_export = &export->export;
		}
		if (tino != NULL) {
			status = omni_trunc0_begin(export, tino, &tr);
			if (FSAL_IS_ERROR(status)) {
				if (tino_ref)
					omni_inode_put(tino);
				return status;
			}
		}
	}

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	status = handle->sub_handle->obj_ops->open2(
		handle->sub_handle, state, openflags, createmode, name,
		attrs_in, verifier, &sub_handle, attrs_out, caller_perm_check,
		parent_pre_attrs_out, parent_post_attrs_out);
	op_ctx->fsal_export = &export->export;

	if (sub_handle) {
		/* wrap the subfsal handle in a omni handle. */
		status = omni_alloc_and_check_handle(export, sub_handle,
						     obj_hdl->fs, new_obj,
						     status);
		if (!FSAL_IS_ERROR(status))
			target = container_of(*new_obj,
					      struct omni_fsal_obj_handle,
					      obj_handle);
	} else if (!FSAL_IS_ERROR(status) && name == NULL) {
		target = handle;
	}

	if (!FSAL_IS_ERROR(status) && target != NULL) {
		/* Identity for later server-side COPY, keyed by the state
		 * (contract 6.4); the last opener for anonymous stateids.
		 */
		omni_state_opened(state, target->ino, openflags);
		/* Truncate to 0 is allowed on a stub (R1 section 6). A file
		 * fenced above lost its marker before the truncate; one the
		 * lookup missed (linked in since) loses it now.
		 */
		if ((openflags & FSAL_O_TRUNC) && tino == NULL &&
		    target->ino != NULL)
			omni_after_truncate0(export, target->ino);
	}
	if (tino != NULL) {
		omni_trunc0_end(export, &tr, !FSAL_IS_ERROR(status));
		if (tino_ref)
			omni_inode_put(tino);
	}

	return status;
}

bool omni_check_verifier(struct fsal_obj_handle *obj_hdl,
			   fsal_verifier_t verifier)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	bool result =
		handle->sub_handle->obj_ops->check_verifier(handle->sub_handle,
							    verifier);
	op_ctx->fsal_export = &export->export;

	return result;
}

fsal_openflags_t omni_status2(struct fsal_obj_handle *obj_hdl,
				struct state_t *state)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_openflags_t result =
		handle->sub_handle->obj_ops->status2(handle->sub_handle, state);
	op_ctx->fsal_export = &export->export;

	return result;
}

fsal_status_t omni_reopen2(struct fsal_obj_handle *obj_hdl,
			     struct state_t *state, fsal_openflags_t openflags)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->reopen2(handle->sub_handle, state,
						     openflags);
	op_ctx->fsal_export = &export->export;

	if (!FSAL_IS_ERROR(status))
		omni_state_reopened(state, openflags);

	return status;
}

static uint64_t io_len(const struct fsal_io_arg *arg)
{
	uint64_t len = arg->io_request;
	int i;

	if (len == 0)
		for (i = 0; i < arg->iov_count; i++)
			len += arg->iov[i].iov_len;
	return len;
}

void omni_read2(struct fsal_obj_handle *obj_hdl, bool bypass,
		  fsal_async_cb done_cb, struct fsal_io_arg *read_arg,
		  void *caller_arg)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);
	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	struct omni_async_arg *arg;
	struct omni_inode *pin = NULL;

	/* A parked read resumed by the protocol layer (contract 6.3): run
	 * the whole guard again, which re-reads the marker.
	 */
	if (read_arg->fsal_resume == OMNI_FSAL_RESUME)
		read_arg->fsal_resume = FSAL_NORESUME;

	/* The stub guard: never let a read reach bytes the marker does not
	 * cover. READ_PLUS takes the same path and never gets NOTSUPP.
	 */
	if (handle->ino != NULL) {
		struct omni_park_req park = { .obj = obj_hdl,
					      .read_arg = read_arg,
					      .done_cb = done_cb,
					      .caller_arg = caller_arg,
					      .async_flags = omni_read_async_flags(
						      read_arg) };
		fsal_status_t st;

		switch (omni_guard(export, handle,
				   read_arg->info ? OMNI_OP_READ_PLUS
						  : OMNI_OP_READ,
				   read_arg->offset, io_len(read_arg), NULL,
				   &park, &st)) {
		case OMNI_G_PARKED:
			/* done_cb comes later from an FSAL thread; the
			 * protocol layer returns NFS_REQ_ASYNC_WAIT. A resume
			 * waits until the protocol has marked the request as
			 * waiting (ASYNC_PROC_EXIT), whenever that is.
			 */
			if (omni_test_parked_return)
				omni_test_parked_return();
			return;
		case OMNI_G_ANSWER:
			done_cb(obj_hdl, st, read_arg, caller_arg);
			return;
		case OMNI_G_PASS:
			pin = handle->ino;
			break;
		}
	}

	/* Set up async callback */
	arg = gsh_calloc(1, sizeof(*arg), MEM_COMP_FSAL);
	arg->obj_hdl = obj_hdl;
	arg->cb = done_cb;
	arg->cb_arg = caller_arg;
	arg->pin = pin;

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	handle->sub_handle->obj_ops->read2(handle->sub_handle, bypass,
					   omni_async_cb, read_arg, arg);
	op_ctx->fsal_export = &export->export;
}

void omni_write2(struct fsal_obj_handle *obj_hdl, bool bypass,
		   fsal_async_cb done_cb, struct fsal_io_arg *write_arg,
		   void *caller_arg)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	struct omni_async_arg *arg;
	struct omni_inode *pin = NULL;

	/* NFSv3 writes need no open: refuse rewind placeholders here too. */
	if (handle->ino != NULL && omni_readonly(handle)) {
		done_cb(obj_hdl, fsalstat(ERR_FSAL_ROFS, 0), write_arg,
			caller_arg);
		return;
	}

	/* A write into a file that is not wholly local would mix user
	 * bytes with backend bytes on the next fill: it needs a full fill
	 * first (R1 section 6), answered like a read of the whole file.
	 */
	if (handle->ino != NULL) {
		fsal_status_t st;

		if (omni_guard(export, handle, OMNI_OP_WRITE,
			       write_arg->offset, io_len(write_arg), NULL, NULL,
			       &st) != OMNI_G_PASS) {
			done_cb(obj_hdl, st, write_arg, caller_arg);
			return;
		}
		pin = handle->ino;
	}

	/* Set up async callback */
	arg = gsh_calloc(1, sizeof(*arg), MEM_COMP_FSAL);
	arg->obj_hdl = obj_hdl;
	arg->cb = done_cb;
	arg->cb_arg = caller_arg;
	arg->pin = pin;

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	handle->sub_handle->obj_ops->write2(handle->sub_handle, bypass,
					    omni_async_cb, write_arg, arg);
	op_ctx->fsal_export = &export->export;
}

fsal_status_t omni_seek2(struct fsal_obj_handle *obj_hdl,
			   struct state_t *state, struct io_info *info)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* A stub's hole is not a hole to the user: it is remote data.
	 * Report a partial file as one data extent, so SEEK_HOLE/SEEK_DATA
	 * (NFSv4.2 SEEK, and READ_PLUS's fallback) never tell cp, tar or
	 * rsync that the remote range is zeros (filer spike B2x).
	 */
	/* Pinned like a read, from the marker check until VFS has answered:
	 * an INVALIDATE that narrows the marker waits, so VFS never reports
	 * a hole the daemon punched after the check saw RESIDENT.
	 */
	if (handle->ino != NULL) {
		struct omni_marker m;
		uint32_t reason;
		int err;
		bool cached;
		enum omni_mres r;
		uint64_t off = info->io_content.hole.di_offset;
		fsal_status_t st = fsalstat(ERR_FSAL_NO_ERROR, 0);

		omni_pin(handle->ino);
		r = omni_marker_get(handle->ino, true, &m, &cached, &reason,
				    &err);
		if (r == OMNI_M_OK && cached &&
		    m.state == OMNI_MST_PARTIAL && off >= m.size)
			/* past a cached size: it may have grown */
			r = omni_marker_get(handle->ino, false, &m, &cached,
					    &reason, &err);
		if (r == OMNI_M_BAD) {
			omni_unpin(handle->ino);
			return omni_policy_default(export);
		}
		if (r == OMNI_M_OK && m.state == OMNI_MST_PARTIAL) {
			if (off >= m.size)
				st = posix2fsal_status(ENXIO);
			else if (info->io_content.what == NFS4_CONTENT_DATA) {
				info->io_content.hole.di_offset = off;
				info->io_eof = false;
			} else if (info->io_content.what ==
				   NFS4_CONTENT_HOLE) {
				info->io_content.hole.di_offset = m.size;
				info->io_eof = true;
			} else {
				st = fsalstat(ERR_FSAL_UNION_NOTSUPP, 0);
			}
			omni_unpin(handle->ino);
			return st;
		}
	}

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->seek2(handle->sub_handle, state,
						   info);
	op_ctx->fsal_export = &export->export;
	if (handle->ino != NULL)
		omni_unpin(handle->ino);

	return status;
}

fsal_status_t omni_io_advise2(struct fsal_obj_handle *obj_hdl,
				struct state_t *state, struct io_hints *hints)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->io_advise2(handle->sub_handle,
							state, hints);
	op_ctx->fsal_export = &export->export;

	return status;
}

fsal_status_t omni_commit2(struct fsal_obj_handle *obj_hdl, off_t offset,
			     size_t len)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->commit2(handle->sub_handle, offset,
						     len);
	op_ctx->fsal_export = &export->export;

	return status;
}

fsal_status_t omni_lock_op2(struct fsal_obj_handle *obj_hdl,
			      struct state_t *state, void *p_owner,
			      fsal_lock_op_t lock_op,
			      fsal_lock_param_t *req_lock,
			      fsal_lock_param_t *conflicting_lock)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status = handle->sub_handle->obj_ops->lock_op2(
		handle->sub_handle, state, p_owner, lock_op, req_lock,
		conflicting_lock);
	op_ctx->fsal_export = &export->export;

	return status;
}

fsal_status_t omni_close2(struct fsal_obj_handle *obj_hdl,
			    struct state_t *state)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	struct omni_identity id;
	fsal_openflags_t flags = 0;
	bool known = omni_state_closed(state, &id, &flags);

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->close2(handle->sub_handle, state);
	op_ctx->fsal_export = &export->export;

	/* Tell the detector at once that a writer is done (contract 5.4);
	 * the fanotify group remains the source of truth.
	 */
	if (known && (flags & FSAL_O_WRITE) && handle->ino != NULL &&
	    (omni_conn_features() & OMNI_FEAT_WRITE_CLOSED)) {
		struct omni_obj_ref obj;
		struct omni_buf b;

		omni_obj_ref_of(handle->ino, NULL, export->export_id, &obj);
		omni_build_write_closed(&b, &obj, 0, &id);
		(void)omni_conn_send(&b);
	}

	return status;
}

fsal_status_t omni_fallocate(struct fsal_obj_handle *obj_hdl,
			       struct state_t *state, uint64_t offset,
			       uint64_t length, bool allocate)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	fsal_status_t status;

	if (omni_readonly(handle))
		return fsalstat(ERR_FSAL_ROFS, 0);
	/* ALLOCATE and DEALLOCATE both need a full fill first: a later fill
	 * would overwrite the zeros the user asked for (R1 section 6).
	 */
	if (handle->ino != NULL &&
	    omni_guard(export, handle, OMNI_OP_FALLOCATE, offset, length,
		       NULL, NULL, &status) != OMNI_G_PASS)
		return status;

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	status = handle->sub_handle->obj_ops->fallocate(handle->sub_handle,
							state, offset, length,
							allocate);
	op_ctx->fsal_export = &export->export;
	if (handle->ino != NULL)
		omni_unpin(handle->ino);
	return status;
}

fsal_status_t omni_setattr2(struct fsal_obj_handle *obj_hdl, bool bypass,
			    struct state_t *state,
			    struct fsal_attrlist *attrs)
{
	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	bool size = handle->ino != NULL &&
		    FSAL_TEST_MASK(attrs->valid_mask, ATTR_SIZE);
	bool trunc0 = size && attrs->filesize == 0;
	bool pinned = false;
	struct omni_trunc tr = { 0 };
	fsal_status_t status;

	/* Nothing in a rewind tree changes: mode, owner, times or size. */
	if (omni_readonly(handle))
		return fsalstat(ERR_FSAL_ROFS, 0);
	/* Truncate to 0 is allowed unless the daemon may be filling the file
	 * (fill fence, contract 6.1); any other size change of a file that is
	 * not wholly local needs a full fill first (R1 section 6).
	 */
	if (trunc0) {
		/* the marker goes before the truncate (smb-guard) */
		status = omni_trunc0_begin(export, handle->ino, &tr);
		if (FSAL_IS_ERROR(status))
			return status;
	} else if (size) {
		if (omni_guard(export, handle, OMNI_OP_SETATTR_SIZE,
			       attrs->filesize, 0, NULL, NULL,
			       &status) != OMNI_G_PASS)
			return status;
		pinned = true;
	}

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	status = handle->sub_handle->obj_ops->setattr2(handle->sub_handle,
						       bypass, state, attrs);
	op_ctx->fsal_export = &export->export;

	if (trunc0)
		omni_trunc0_end(export, &tr, !FSAL_IS_ERROR(status));
	if (pinned)
		omni_unpin(handle->ino);
	/* who changed the owner (contract 5.4, NS_HINT SETATTR `what` OWNER) */
	if (!FSAL_IS_ERROR(status) &&
	    FSAL_TEST_MASK(attrs->valid_mask, ATTR_OWNER | ATTR_GROUP))
		omni_ns_hint(export, OMNI_NS_SETATTR, OMNI_NS_WHAT_OWNER, NULL,
			     handle, "", NULL);
	return status;
}

/**
 * @brief Guarded server-side COPY (NFSv4.2), contract 6.4.
 *
 * Ganesha runs COPY on xcopy threads with a blank op context, so the
 * identity comes from the source's open state (or its last opener). A
 * source range that is not wholly local is answered DELAY or EIO and
 * reported to the daemon as COPY_SRC; it is never handed to FSAL_VFS,
 * whose copy_file_range(2) reflinks on xfs and would copy the holes. The
 * destination is checked as a write. Only then does VFS copy, with both
 * files pinned so no narrowing can overtake it.
 */
fsal_status_t omni_copy_file_range(struct fsal_obj_handle *src_obj,
				   struct state_t *src_state,
				   struct fsal_obj_handle *dst_obj,
				   struct state_t *dst_state,
				   uint64_t src_offset, uint64_t dst_offset,
				   uint64_t count, uint64_t *copied)
{
	struct omni_fsal_obj_handle *src =
		container_of(src_obj, struct omni_fsal_obj_handle, obj_handle);
	struct omni_fsal_obj_handle *dst =
		container_of(dst_obj, struct omni_fsal_obj_handle, obj_handle);
	struct omni_fsal_export *export =
		container_of(op_ctx->fsal_export, struct omni_fsal_export,
			     export);
	struct omni_identity sid, did;
	struct omni_inode *pa = NULL, *pb = NULL;
	fsal_status_t status;

	*copied = 0;
	if (omni_readonly(dst))
		return fsalstat(ERR_FSAL_ROFS, 0);
	/* The guard pins each inode it passes. Take them in address order,
	 * and only once if source and destination are the same file.
	 */
	if (src->ino != NULL)
		omni_identity_for_state(src_state, src->ino, &sid);
	if (dst->ino != NULL)
		omni_identity_for_state(dst_state, dst->ino, &did);

	if (src->ino != NULL && dst->ino != NULL && src->ino == dst->ino) {
		if (omni_guard(export, src, OMNI_OP_COPY_SRC, src_offset,
			       count, &sid, NULL, &status) != OMNI_G_PASS)
			return status;
		pa = src->ino;
		omni_unpin(pa);
		if (omni_guard(export, dst, OMNI_OP_WRITE, dst_offset, count,
			       &did, NULL, &status) != OMNI_G_PASS)
			return status;
		/* the destination check re-pinned the shared inode */
	} else {
		struct omni_fsal_obj_handle *first = src, *second = dst;
		uint8_t op1 = OMNI_OP_COPY_SRC, op2 = OMNI_OP_WRITE;
		uint64_t off1 = src_offset, off2 = dst_offset;
		struct omni_identity *id1 = &sid, *id2 = &did;

		if ((uintptr_t)dst->ino < (uintptr_t)src->ino) {
			first = dst;
			second = src;
			op1 = OMNI_OP_WRITE;
			op2 = OMNI_OP_COPY_SRC;
			off1 = dst_offset;
			off2 = src_offset;
			id1 = &did;
			id2 = &sid;
		}
		if (first->ino != NULL) {
			if (omni_guard(export, first, op1, off1, count, id1,
				       NULL, &status) != OMNI_G_PASS)
				return status;
			pa = first->ino;
		}
		if (second->ino != NULL) {
			if (omni_guard(export, second, op2, off2, count, id2,
				       NULL, &status) != OMNI_G_PASS) {
				if (pa != NULL)
					omni_unpin(pa);
				return status;
			}
			pb = second->ino;
		}
	}

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	status = src->sub_handle->obj_ops->copy_file_range(
		src->sub_handle, src_state, dst->sub_handle, dst_state,
		src_offset, dst_offset, count, copied);
	op_ctx->fsal_export = &export->export;

	if (src->ino != NULL && src->ino == dst->ino) {
		omni_unpin(src->ino);
	} else {
		if (pa != NULL)
			omni_unpin(pa);
		if (pb != NULL)
			omni_unpin(pb);
	}
	return status;
}
