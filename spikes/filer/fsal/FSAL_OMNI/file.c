// SPDX-License-Identifier: LGPL-3.0-or-later
/*
 * vim:noexpandtab:shiftwidth=8:tabstop=8:
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
 * File I/O methods for NULL module
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
 * @brief Callback arg for NULL async callbacks
 *
 * NULL needs to know what its object is related to the sub-FSAL's object.
 * This wraps the given callback arg with NULL specific info
 */
struct omni_async_arg {
	struct fsal_obj_handle *obj_hdl; /**< NULL's handle */
	fsal_async_cb cb; /**< Wrapped callback */
	void *cb_arg; /**< Wrapped callback data */
};

/**
 * @brief Callback for NULL async calls
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

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status = handle->sub_handle->obj_ops->open2(
		handle->sub_handle, state, openflags, createmode, name,
		attrs_in, verifier, &sub_handle, attrs_out, caller_perm_check,
		parent_pre_attrs_out, parent_post_attrs_out);
	op_ctx->fsal_export = &export->export;

	if (sub_handle) {
		/* wrap the subfsal handle in a nullfs handle. */
		status = omni_alloc_and_check_handle(export, sub_handle,
						     obj_hdl->fs, new_obj,
						     status);
		if (!FSAL_IS_ERROR(status) && name != NULL)
			omni_set_path(*new_obj, handle->path, name);
		return status;
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

	return status;
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

	/* OmniFile stub guard: never let a read reach a stub's hole. */
	if (obj_hdl->type == REGULAR_FILE) {
		uint64_t len = read_arg->io_request;
		fsal_status_t g;
		int i;

		if (len == 0)
			for (i = 0; i < read_arg->iov_count; i++)
				len += read_arg->iov[i].iov_len;
		g = omni_guard_read(export, handle,
				    read_arg->info ? "read_plus" : "read",
				    read_arg->offset, len);
		if (FSAL_IS_ERROR(g)) {
			done_cb(obj_hdl, g, read_arg, caller_arg);
			return;
		}
	}

	/* Set up async callback */
	arg = gsh_calloc(1, sizeof(*arg), MEM_COMP_FSAL);
	arg->obj_hdl = obj_hdl;
	arg->cb = done_cb;
	arg->cb_arg = caller_arg;

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

	/* Set up async callback */
	arg = gsh_calloc(1, sizeof(*arg), MEM_COMP_FSAL);
	arg->obj_hdl = obj_hdl;
	arg->cb = done_cb;
	arg->cb_arg = caller_arg;

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
	struct omni_stub_info si;

	/* A stub's hole is not a hole to the user: it is archived data.
	 * Report the whole file as one data extent so SEEK_HOLE/SEEK_DATA
	 * (NFSv4.2 SEEK, and Ganesha's READ_PLUS fallback) never tell a
	 * client, or cp/tar/rsync, that the archived range is zeros.
	 */
	if (obj_hdl->type == REGULAR_FILE) {
		int rc = omni_stub_info(export, handle, &si);

		if (rc != 0)
			return omni_fail_closed(export);
		if (si.kind == OMNI_STUB) {
			struct fsal_attrlist attrs;
			fsal_status_t st;
			uint64_t off = info->io_content.hole.di_offset;

			fsal_prepare_attrs(&attrs, ATTR_SIZE);
			op_ctx->fsal_export = export->export.sub_export;
			st = handle->sub_handle->obj_ops->getattrs(
				handle->sub_handle, &attrs);
			op_ctx->fsal_export = &export->export;
			fsal_release_attrs(&attrs);
			if (FSAL_IS_ERROR(st))
				return st;
			LogEvent(COMPONENT_FSAL,
				 "OMNI seek on stub: uid=%u path=%s what=%s off=%" PRIu64
				 " size=%" PRIu64 " -> whole file is data",
				 op_ctx->creds.caller_uid,
				 handle->path ? handle->path : "?",
				 info->io_content.what == NFS4_CONTENT_DATA
					 ? "DATA" : "HOLE",
				 off, attrs.filesize);
			if (off >= attrs.filesize)
				return posix2fsal_status(ENXIO);
			if (info->io_content.what == NFS4_CONTENT_DATA) {
				info->io_content.hole.di_offset = off;
				info->io_eof = false;
			} else if (info->io_content.what ==
				   NFS4_CONTENT_HOLE) {
				info->io_content.hole.di_offset =
					attrs.filesize;
				info->io_eof = true;
			} else {
				return fsalstat(ERR_FSAL_UNION_NOTSUPP, 0);
			}
			return fsalstat(ERR_FSAL_NO_ERROR, 0);
		}
	}

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->seek2(handle->sub_handle, state,
						   info);
	op_ctx->fsal_export = &export->export;

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

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	fsal_status_t status =
		handle->sub_handle->obj_ops->close2(handle->sub_handle, state);
	op_ctx->fsal_export = &export->export;

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

	/* calling subfsal method */
	op_ctx->fsal_export = export->export.sub_export;
	status = handle->sub_handle->obj_ops->fallocate(handle->sub_handle,
							state, offset, length,
							allocate);
	op_ctx->fsal_export = &export->export;
	return status;
}
