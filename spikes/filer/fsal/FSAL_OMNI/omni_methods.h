/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * vim:noexpandtab:shiftwidth=8:tabstop=8:
 *
 * Copyright (C) Panasas Inc., 2011
 * Author: Jim Lieb jlieb@panasas.com
 *
 * contributeur : Philippe DENIEL   philippe.deniel@cea.fr
 *                Thomas LEIBOVICI  thomas.leibovici@cea.fr
 *
 *   This library is free software; you can redistribute it and/or modify
 *   it under the terms of the GNU Lesser General Public License as published
 *   by the Free Software Foundation; either version 2.1 of the License, or
 *   (at your option) any later version.
 *
 *   This library is distributed in the hope that it will be useful,
 *   but WITHOUT ANY WARRANTY; without even the implied warranty of
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See
 *   the GNU Lesser General Public License for more details.
 *
 *   You should have received a copy of the GNU Lesser General Public License
 *   along with this library; if not, write to the Free Software
 *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
 */

/**
 * @brief OMNIFS methods for handles
 */

/* OMNIFS methods for handles
 */

#ifndef OMNI_METHODS_H
#define OMNI_METHODS_H

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
	const char *dir_path; /*< OmniFile: best-effort path of the dir */
};

extern struct fsal_up_vector fsal_up_top;
void omni_handle_ops_init(struct fsal_obj_ops *ops);

/*
 * OMNIFS internal export
 */
enum omni_policy_kind {
	OMNI_POLICY_WAIT = 0,
	OMNI_POLICY_FAILFAST = 1,
	OMNI_POLICY_WAITQUICK = 2,
};

struct omni_fsal_export {
	struct fsal_export export;
	/* OmniFile share settings (FSAL { Name = OMNI; ... }) */
	char *policy; /*< "wait" | "failfast" | "waitquick:<ms>" */
	int policy_kind; /*< enum omni_policy_kind */
	uint32_t quick_ms; /*< threshold for waitquick */
	char *socket_path; /*< daemon socket */
	uint32_t timeout_ms; /*< daemon connect/send/recv timeout */
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
	struct fsal_obj_handle obj_handle; /*< Handle containing nullfs data.*/
	struct fsal_obj_handle *sub_handle; /*< Handle of the sub fsal.*/
	int32_t refcnt; /*< Reference count.  This is signed to make
				   mistakes easy to see. */
	char *path; /*< best-effort path, for the daemon (may be NULL) */
};

/* OmniFile stub guard (omni_guard.c) */
enum omni_kind { OMNI_NOT_MANAGED, OMNI_RESIDENT, OMNI_STUB };

struct omni_stub_info {
	enum omni_kind kind;
	uint64_t resident_bytes;
	char key[512];
};

int omni_stub_info(struct omni_fsal_export *exp,
		   struct omni_fsal_obj_handle *h, struct omni_stub_info *out);
fsal_status_t omni_fail_closed(struct omni_fsal_export *exp);
fsal_status_t omni_guard_read(struct omni_fsal_export *exp,
			      struct omni_fsal_obj_handle *h, const char *op,
			      uint64_t offset, uint64_t len);
void omni_set_path(struct fsal_obj_handle *obj, const char *parent,
		   const char *name);
int omni_parse_policy(const char *policy, uint32_t *quick_ms);

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
