/* SPDX-License-Identifier: LGPL-3.0-or-later */
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
 * The upstream file (nullfs_methods.h) is LGPL-2.1-or-later; this derived
 * version is distributed under version 3 of that licence, as its "or (at
 * your option) any later version" clause permits, to match the rest of the
 * module.
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

/**
 * @brief OMNIFS methods for handles
 */

/* OMNIFS methods for handles
 */

#ifndef OMNI_METHODS_H
#define OMNI_METHODS_H

#include "omni_guard.h"

struct omni_fsal_module {
	struct fsal_module module;
	struct fsal_obj_ops handle_ops;
};

extern struct omni_fsal_module OMNIFS;

struct omni_fsal_obj_handle;

/* Internal OMNIFS method linkage to export object
 */

fsal_status_t omni_create_export(struct fsal_module *fsal_hdl,
				   void *parse_node,
				   struct config_error_type *err_type,
				   const struct fsal_up_vector *up_ops);

fsal_status_t omni_update_export(struct fsal_module *fsal_hdl,
				   void *parse_node,
				   struct config_error_type *err_type,
				   struct fsal_export *original,
				   struct fsal_module *updated_super);

/**
 * Structure used to store data for read_dirents callback.
 *
 * Before executing the upper level callback (it might be another
 * stackable fsal or the inode cache), the context has to be restored.
 */
struct omni_readdir_state {
	fsal_readdir_cb cb; /*< Callback to the upper layer. */
	struct omni_fsal_export *exp; /*< Export of the current omnifsal. */
	void *dir_state; /*< State to be sent to the next callback. */
	bool live; /*< The directory is outside every rewind tree. */
};

extern struct fsal_up_vector fsal_up_top;
void omni_handle_ops_init(struct fsal_obj_ops *ops);

/*
 * OMNIFS internal export
 */
struct omni_fsal_export {
	struct fsal_export export;
	/* The share's policy for non-local data (contract section 10):
	 * FSAL { Name = OMNI; Policy = wait | failfast | waitquick:<ms>;
	 *        Park_Ms = <ms>; FSAL { Name = VFS; } }
	 */
	char policy_str[64];
	uint8_t policy; /* enum omni_policy */
	uint32_t quick_ms;
	uint32_t park_ms; /* 0: never park */
	uint32_t export_id;
	int32_t parked; /* parked reads now; atomic */
	struct omni_rl rl_closed; /* fail-closed warnings */
	struct omni_rl rl_cap; /* missing CAP_SYS_ADMIN */
	struct omni_rl rl_rewind; /* POPULATE_DIR not sent */
	/* The share's rewind trees, shown as .snapshots in its root
	 * (Snap_Dir = <data>/.omni/snap/<share>); NULL: none. Set at
	 * creation only.
	 */
	char *snap_dir;
};

fsal_status_t omni_lookup_path(struct fsal_export *exp_hdl, const char *path,
				 struct fsal_obj_handle **handle,
				 struct fsal_attrlist *attrs_out);

fsal_status_t omni_create_handle(struct fsal_export *exp_hdl,
				   struct gsh_buffdesc *hdl_desc,
				   struct fsal_obj_handle **handle,
				   struct fsal_attrlist *attrs_out);

fsal_status_t omni_alloc_and_check_handle(struct omni_fsal_export *export,
					    struct fsal_obj_handle *sub_handle,
					    struct fsal_filesystem *fs,
					    struct fsal_obj_handle **new_handle,
					    fsal_status_t subfsal_status);

/*
 * OMNIFS internal object handle
 *
 * It contains a pointer to the fsal_obj_handle used by the subfsal.
 *
 * AF_UNIX sockets are strange ducks.  I personally cannot see why they
 * are here except for the ability of a client to see such an animal with
 * an 'ls' or get rid of one with an 'rm'.  You can't open them in the
 * usual file way so open_by_handle_at leads to a deadend.  To work around
 * this, we save the args that were used to mknod or lookup the socket.
 */

struct omni_fsal_obj_handle {
	struct fsal_obj_handle obj_handle; /*< Handle containing omni data.*/
	struct fsal_obj_handle *sub_handle; /*< Handle of the sub fsal.*/
	int32_t refcnt; /*< Reference count.  This is signed to make
				   mistakes easy to see. */
	struct omni_inode *ino; /*< Guard state; regular files only. */
	struct omni_inode *dino; /*< Rewind state; directories only. */
};

/* Rewind trees (omni_rewind.c; fsal-namespace.md section 7). */
#define OMNI_SNAPSHOTS ".snapshots"

enum omni_rw {
	OMNI_RW_LIVE, /* no READONLY marker: an ordinary object */
	OMNI_RW_READONLY, /* part of a rewind tree */
	OMNI_RW_UNPOPULATED, /* a rewind directory not filled yet */
};

enum omni_rw omni_rewind_of(struct omni_fsal_obj_handle *h);
/* obj was found from a live directory (or is the export's root), so it is
 * live too: rewind trees are reached only through .snapshots.
 */
void omni_mark_live(struct fsal_obj_handle *obj);
/* Any change to h, or inside it, is ERR_FSAL_ROFS. */
bool omni_readonly(struct omni_fsal_obj_handle *h);
/* Is name the reserved .snapshots of this export's root? */
bool omni_snapshots_name(struct omni_fsal_export *exp,
			 struct omni_fsal_obj_handle *dir, const char *name);
/* The sub-FSAL handle of Snap_Dir. */
fsal_status_t omni_snapshots_lookup(struct omni_fsal_export *exp,
				    struct fsal_obj_handle **sub_handle,
				    struct fsal_attrlist *attrs_out);
/* After a sub-FSAL lookup of ".." in a READONLY directory: if it left the
 * rewind tree, replace it with the export's root.
 */
fsal_status_t omni_rewind_parent(struct omni_fsal_export *exp,
				 struct fsal_obj_handle **sub_handle,
				 struct fsal_attrlist *attrs_out);
/* May name be created, removed or renamed in dir? ERR_FSAL_ROFS inside a
 * rewind tree, and for .snapshots in the export's root.
 */
fsal_status_t omni_dir_change(struct omni_fsal_export *exp,
			      struct omni_fsal_obj_handle *dir,
			      const char *name);
/* Ask the daemon to fill dir (rate-limited); the answer for the client. */
fsal_status_t omni_populate(struct omni_fsal_export *exp,
			    struct omni_fsal_obj_handle *dir);

int omni_fsal_open(struct omni_fsal_obj_handle *, int, fsal_errors_t *);
int omni_fsal_readlink(struct omni_fsal_obj_handle *, fsal_errors_t *);

static inline bool omni_unopenable_type(object_file_type_t type)
{
	if ((type == SOCKET_FILE) || (type == CHARACTER_FILE) ||
	    (type == BLOCK_FILE)) {
		return true;
	} else {
		return false;
	}
}

/* I/O management */
fsal_status_t omni_close(struct fsal_obj_handle *obj_hdl);

/* Multi-FD */
fsal_status_t omni_open2(struct fsal_obj_handle *obj_hdl,
			   struct state_t *state, fsal_openflags_t openflags,
			   enum fsal_create_mode createmode, const char *name,
			   struct fsal_attrlist *attrs_in,
			   fsal_verifier_t verifier,
			   struct fsal_obj_handle **new_obj,
			   struct fsal_attrlist *attrs_out,
			   bool *caller_perm_check,
			   struct fsal_attrlist *parent_pre_attrs_out,
			   struct fsal_attrlist *parent_post_attrs_out);
bool omni_check_verifier(struct fsal_obj_handle *obj_hdl,
			   fsal_verifier_t verifier);
fsal_openflags_t omni_status2(struct fsal_obj_handle *obj_hdl,
				struct state_t *state);
fsal_status_t omni_reopen2(struct fsal_obj_handle *obj_hdl,
			     struct state_t *state, fsal_openflags_t openflags);
void omni_read2(struct fsal_obj_handle *obj_hdl, bool bypass,
		  fsal_async_cb done_cb, struct fsal_io_arg *read_arg,
		  void *caller_arg);
void omni_write2(struct fsal_obj_handle *obj_hdl, bool bypass,
		   fsal_async_cb done_cb, struct fsal_io_arg *write_arg,
		   void *caller_arg);
fsal_status_t omni_seek2(struct fsal_obj_handle *obj_hdl,
			   struct state_t *state, struct io_info *info);
fsal_status_t omni_io_advise2(struct fsal_obj_handle *obj_hdl,
				struct state_t *state, struct io_hints *hints);
fsal_status_t omni_commit2(struct fsal_obj_handle *obj_hdl, off_t offset,
			     size_t len);
fsal_status_t omni_lock_op2(struct fsal_obj_handle *obj_hdl,
			      struct state_t *state, void *p_owner,
			      fsal_lock_op_t lock_op,
			      fsal_lock_param_t *req_lock,
			      fsal_lock_param_t *conflicting_lock);
fsal_status_t omni_close2(struct fsal_obj_handle *obj_hdl,
			    struct state_t *state);
fsal_status_t omni_fallocate(struct fsal_obj_handle *obj_hdl,
			       struct state_t *state, uint64_t offset,
			       uint64_t length, bool allocate);
fsal_status_t omni_copy_file_range(struct fsal_obj_handle *src_obj,
				   struct state_t *src_state,
				   struct fsal_obj_handle *dst_obj,
				   struct state_t *dst_state,
				   uint64_t src_offset, uint64_t dst_offset,
				   uint64_t count, uint64_t *copied);
fsal_status_t omni_setattr2(struct fsal_obj_handle *obj_hdl, bool bypass,
			    struct state_t *state,
			    struct fsal_attrlist *attrs);

/* extended attributes management */
fsal_status_t omni_list_ext_attrs(struct fsal_obj_handle *obj_hdl,
				    unsigned int cookie,
				    fsal_xattrent_t *xattrs_tab,
				    unsigned int xattrs_tabsize,
				    unsigned int *p_nb_returned,
				    int *end_of_list);
fsal_status_t omni_getextattr_id_by_name(struct fsal_obj_handle *obj_hdl,
					   const char *xattr_name,
					   unsigned int *pxattr_id);
fsal_status_t omni_getextattr_value_by_name(struct fsal_obj_handle *obj_hdl,
					      const char *xattr_name,
					      void *buffer_addr,
					      size_t buffer_size,
					      size_t *p_output_size);
fsal_status_t omni_getextattr_value_by_id(struct fsal_obj_handle *obj_hdl,
					    unsigned int xattr_id,
					    void *buffer_addr,
					    size_t buffer_size,
					    size_t *p_output_size);
fsal_status_t omni_setextattr_value(struct fsal_obj_handle *obj_hdl,
				      const char *xattr_name, void *buffer_addr,
				      size_t buffer_size, int create);
fsal_status_t omni_setextattr_value_by_id(struct fsal_obj_handle *obj_hdl,
					    unsigned int xattr_id,
					    void *buffer_addr,
					    size_t buffer_size);
fsal_status_t omni_remove_extattr_by_id(struct fsal_obj_handle *obj_hdl,
					  unsigned int xattr_id);
fsal_status_t omni_remove_extattr_by_name(struct fsal_obj_handle *obj_hdl,
					    const char *xattr_name);

#endif /* OMNI_METHODS_H */
