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
 * 51 Franklin Street, Fifth Floor, Boston, MA
 * 02110-1301 USA
 *
 */

/* export.c
 * NULL FSAL export object
 */

#include "config.h"

#include "fsal.h"
#include <libgen.h> /* used for 'dirname' */
#include <pthread.h>
#include <string.h>
#include <sys/types.h>
#include <os/mntent.h>
#include <os/quota.h>
#include <dlfcn.h>
#include "gsh_list.h"
#include "config_parsing.h"
#include "fsal_convert.h"
#include "FSAL/fsal_commonlib.h"
#include "FSAL/fsal_config.h"
#include "omni_methods.h"
#include "nfs_exports.h"
#include "export_mgr.h"

/* helpers to/from other NULL objects
 */

/* export object methods
 */

static void release(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *myself;
	struct fsal_module *sub_fsal;

	myself = container_of(exp_hdl, struct omni_fsal_export, export);
	sub_fsal = myself->export.sub_export->fsal;

	/* Release the sub_export */
	myself->export.sub_export->exp_ops.release(myself->export.sub_export);
	fsal_put(sub_fsal);

	LogFullDebug(COMPONENT_FSAL, "FSAL %s fsal_refcount %" PRIu32,
		     sub_fsal->name, atomic_fetch_int32_t(&sub_fsal->refcount));

	fsal_detach_export(exp_hdl->fsal, &exp_hdl->exports);
	free_export_ops(exp_hdl);

	gsh_free(myself->policy, MEM_COMP_EXPORT);
	gsh_free(myself->socket_path, MEM_COMP_EXPORT);
	gsh_free(myself, MEM_COMP_EXPORT); /* elvis has left the building */
}

static fsal_status_t get_dynamic_info(struct fsal_export *exp_hdl,
				      struct fsal_obj_handle *obj_hdl,
				      fsal_dynamicfsinfo_t *infop)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	struct omni_fsal_obj_handle *handle =
		container_of(obj_hdl, struct omni_fsal_obj_handle,
			     obj_handle);

	/* calling subfsal method */
	op_ctx->fsal_export = exp->export.sub_export;
	fsal_status_t status = op_ctx->fsal_export->exp_ops.get_fs_dynamic_info(
		op_ctx->fsal_export, handle->sub_handle, infop);
	op_ctx->fsal_export = &exp->export;

	return status;
}

static bool fs_supports(struct fsal_export *exp_hdl,
			fsal_fsinfo_options_t option)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	bool result = exp->export.sub_export->exp_ops.fs_supports(
		exp->export.sub_export, option);

	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint64_t fs_maxfilesize(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint64_t result = exp->export.sub_export->exp_ops.fs_maxfilesize(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint32_t fs_maxread(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_maxread(
		exp->export.sub_export);

	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint32_t fs_maxwrite(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_maxwrite(
		exp->export.sub_export);

	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint32_t fs_maxlink(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_maxlink(
		exp->export.sub_export);

	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint32_t fs_maxnamelen(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_maxnamelen(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint32_t fs_maxpathlen(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_maxpathlen(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static fsal_aclsupp_t fs_acl_support(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	fsal_aclsupp_t result = exp->export.sub_export->exp_ops.fs_acl_support(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static attrmask_t fs_supported_attrs(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	attrmask_t result = exp->export.sub_export->exp_ops.fs_supported_attrs(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static uint32_t fs_umask(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_umask(
		exp->export.sub_export);

	op_ctx->fsal_export = &exp->export;

	return result;
}

static int32_t fs_expiretimeparent(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_expiretimeparent(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

/**
 * @brief Get the configured readdir modes for parent handle
 *
 * @param[in] exp_hdl   Export to query
 * @return Readdir modes for parent handle
 */
static enum fsal_readdir_mode fs_readdir_mode(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	uint32_t result = exp->export.sub_export->exp_ops.fs_readdir_mode(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;

	return result;
}

/* get_quota
 * return quotas for this export.
 * path could cross a lower mount boundary which could
 * mask lower mount values with those of the export root
 * if this is a real issue, we can scan each time with setmntent()
 * better yet, compare st_dev of the file with st_dev of root_fd.
 * on linux, can map st_dev -> /proc/partitions name -> /dev/<name>
 */

static fsal_status_t get_quota(struct fsal_export *exp_hdl,
			       const char *filepath, int quota_type,
			       int quota_id, fsal_quota_t *pquota)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	fsal_status_t result = exp->export.sub_export->exp_ops.get_quota(
		exp->export.sub_export, filepath, quota_type, quota_id, pquota);
	op_ctx->fsal_export = &exp->export;

	return result;
}

/* set_quota
 * same lower mount restriction applies
 */

static fsal_status_t set_quota(struct fsal_export *exp_hdl,
			       const char *filepath, int quota_type,
			       int quota_id, fsal_quota_t *pquota,
			       fsal_quota_t *presquota)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	fsal_status_t result = exp->export.sub_export->exp_ops.set_quota(
		exp->export.sub_export, filepath, quota_type, quota_id, pquota,
		presquota);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static struct state_t *omni_alloc_state(struct fsal_export *exp_hdl,
					  enum state_type state_type,
					  struct state_t *related_state)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	state_t *state = exp->export.sub_export->exp_ops.alloc_state(
		exp->export.sub_export, state_type, related_state);
	op_ctx->fsal_export = exp_hdl;

	return state;
}

static bool omni_is_superuser(struct fsal_export *exp_hdl,
				const struct user_cred *creds)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);
	bool rv;

	op_ctx->fsal_export = exp->export.sub_export;
	rv = exp->export.sub_export->exp_ops.is_superuser(
		exp->export.sub_export, creds);
	op_ctx->fsal_export = &exp->export;

	return rv;
}

/* extract a file handle from a buffer.
 * do verification checks and flag any and all suspicious bits.
 * Return an updated fh_desc into whatever was passed.  The most
 * common behavior, done here is to just reset the length.
 */

static fsal_status_t wire_to_host(struct fsal_export *exp_hdl,
				  fsal_digesttype_t in_type,
				  struct gsh_buffdesc *fh_desc, int flags)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	fsal_status_t result = exp->export.sub_export->exp_ops.wire_to_host(
		exp->export.sub_export, in_type, fh_desc, flags);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static fsal_status_t omni_host_to_key(struct fsal_export *exp_hdl,
					struct gsh_buffdesc *fh_desc)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	fsal_status_t result = exp->export.sub_export->exp_ops.host_to_key(
		exp->export.sub_export, fh_desc);
	op_ctx->fsal_export = &exp->export;

	return result;
}

static void omni_prepare_unexport(struct fsal_export *exp_hdl)
{
	struct omni_fsal_export *exp =
		container_of(exp_hdl, struct omni_fsal_export, export);

	op_ctx->fsal_export = exp->export.sub_export;
	exp->export.sub_export->exp_ops.prepare_unexport(
		exp->export.sub_export);
	op_ctx->fsal_export = &exp->export;
}

/* omni_export_ops_init
 * overwrite vector entries with the methods that we support
 */

void omni_export_ops_init(struct export_ops *ops)
{
	ops->release = release;
	ops->prepare_unexport = omni_prepare_unexport;
	ops->lookup_path = omni_lookup_path;
	ops->wire_to_host = wire_to_host;
	ops->host_to_key = omni_host_to_key;
	ops->create_handle = omni_create_handle;
	ops->get_fs_dynamic_info = get_dynamic_info;
	ops->fs_supports = fs_supports;
	ops->fs_maxfilesize = fs_maxfilesize;
	ops->fs_maxread = fs_maxread;
	ops->fs_maxwrite = fs_maxwrite;
	ops->fs_maxlink = fs_maxlink;
	ops->fs_maxnamelen = fs_maxnamelen;
	ops->fs_maxpathlen = fs_maxpathlen;
	ops->fs_acl_support = fs_acl_support;
	ops->fs_supported_attrs = fs_supported_attrs;
	ops->fs_umask = fs_umask;
	ops->fs_expiretimeparent = fs_expiretimeparent;
	ops->fs_readdir_mode = fs_readdir_mode;
	ops->get_quota = get_quota;
	ops->set_quota = set_quota;
	ops->alloc_state = omni_alloc_state;
	ops->is_superuser = omni_is_superuser;
}

struct omnifsal_args {
	struct subfsal_args subfsal;
	char *policy;
	char *socket_path;
	uint32_t timeout_ms;
};

static struct config_item sub_fsal_params[] = {
	CONF_ITEM_STR("name", 1, 10, NULL, subfsal_args, name), CONFIG_EOL
};

static struct config_item export_params[] = {
	CONF_ITEM_NOOP("name"),
	CONF_ITEM_STR("policy", 1, 64, "wait", omnifsal_args, policy),
	CONF_ITEM_PATH("socket", 1, 107, "/run/omnifile/fsal.sock",
		       omnifsal_args, socket_path),
	CONF_ITEM_UI32("timeout_ms", 1, 600000, 2000, omnifsal_args,
		       timeout_ms),
	CONF_RELAX_BLOCK("FSAL", sub_fsal_params, noop_conf_init,
			 subfsal_commit, omnifsal_args, subfsal),
	CONFIG_EOL
};

static struct config_block export_param = {
	.dbus_interface_name = "org.ganesha.nfsd.config.fsal.omni-export%d",
	.blk_desc.name = "FSAL",
	.blk_desc.type = CONFIG_BLOCK,
	.blk_desc.u.blk.init = noop_conf_init,
	.blk_desc.u.blk.params = export_params,
	.blk_desc.u.blk.commit = noop_conf_commit,
	.mem_comp = MEM_COMP_EXPORT
};

/* create_export
 * Create an export point and return a handle to it to be kept
 * in the export list.
 * First lookup the fsal, then create the export and then put the fsal back.
 * returns the export with one reference taken.
 */

fsal_status_t omni_create_export(struct fsal_module *fsal_hdl,
				   void *parse_node,
				   struct config_error_type *err_type,
				   const struct fsal_up_vector *up_ops)
{
	fsal_status_t expres;
	struct fsal_module *fsal_stack;
	struct omni_fsal_export *myself;
	struct omnifsal_args omnifsal;
	int retval, kind;
	uint32_t quick_ms;

	memset(&omnifsal, 0, sizeof(omnifsal));
	/* process our FSAL block to get the name of the fsal
	 * underneath us.
	 */
	retval = load_config_from_node(parse_node, &export_param, &omnifsal,
				       true, err_type);
	if (retval != 0)
		return fsalstat(ERR_FSAL_INVAL, 0);
	kind = omni_parse_policy(omnifsal.policy, &quick_ms);
	if (kind < 0) {
		LogCrit(COMPONENT_FSAL,
			"OMNI: bad Policy \"%s\" (wait|failfast|waitquick:<ms>)",
			omnifsal.policy);
		return fsalstat(ERR_FSAL_INVAL, EINVAL);
	}
	fsal_stack = lookup_fsal(omnifsal.subfsal.name);
	if (fsal_stack == NULL) {
		LogMajor(COMPONENT_FSAL,
			 "nullfs create export failed to lookup for FSAL %s",
			 omnifsal.subfsal.name);
		return fsalstat(ERR_FSAL_INVAL, EINVAL);
	}

	myself = gsh_calloc(1, sizeof(struct omni_fsal_export),
			    MEM_COMP_EXPORT);
	expres = fsal_stack->m_ops.create_export(fsal_stack,
						 omnifsal.subfsal.fsal_node,
						 err_type, up_ops);
	fsal_put(fsal_stack);

	LogFullDebug(COMPONENT_FSAL, "FSAL %s fsal_refcount %" PRIu32,
		     fsal_stack->name,
		     atomic_fetch_int32_t(&fsal_stack->refcount));

	if (FSAL_IS_ERROR(expres)) {
		LogMajor(COMPONENT_FSAL,
			 "Failed to call create_export on underlying FSAL %s",
			 omnifsal.subfsal.name);
		gsh_free(myself, MEM_COMP_EXPORT);
		return expres;
	}

	fsal_export_stack(op_ctx->fsal_export, &myself->export);

	fsal_export_init(&myself->export);
	omni_export_ops_init(&myself->export.exp_ops);
#ifdef EXPORT_OPS_INIT
	/*** FIX ME!!!
	 * Need to iterate through the lists to save and restore.
	 */
	omni_handle_ops_init(myself->export.obj_ops);
#endif /* EXPORT_OPS_INIT */
	myself->export.up_ops = up_ops;
	myself->export.fsal = fsal_hdl;
	myself->policy = omnifsal.policy;
	myself->policy_kind = kind;
	myself->quick_ms = quick_ms;
	myself->socket_path = omnifsal.socket_path;
	myself->timeout_ms = omnifsal.timeout_ms;
	LogEvent(COMPONENT_FSAL,
		 "OMNI export over %s: policy=%s socket=%s timeout_ms=%u",
		 omnifsal.subfsal.name, myself->policy, myself->socket_path,
		 myself->timeout_ms);

	/* lock myself before attaching to the fsal.
	 * keep myself locked until done with creating myself.
	 */
	op_ctx->fsal_export = &myself->export;
	return fsalstat(ERR_FSAL_NO_ERROR, 0);
}

fsal_status_t omni_update_export(struct fsal_module *fsal_hdl,
				   void *parse_node,
				   struct config_error_type *err_type,
				   struct fsal_export *original,
				   struct fsal_module *updated_super)
{
	fsal_status_t status;
	struct fsal_module *fsal_stack;
	struct omnifsal_args omnifsal;
	int retval;

	/* Check for changes in stacking by calling default update_export. */
	status = update_export(fsal_hdl, parse_node, err_type, original,
			       updated_super);

	if (FSAL_IS_ERROR(status))
		return status;

	/* process our FSAL block to get the name of the fsal
	 * underneath us.
	 */
	retval = load_config_from_node(parse_node, &export_param, &omnifsal,
				       true, err_type);

	if (retval != 0)
		return fsalstat(ERR_FSAL_INVAL, 0);

	fsal_stack = lookup_fsal(omnifsal.subfsal.name);

	if (fsal_stack == NULL) {
		LogMajor(COMPONENT_FSAL,
			 "nullfs update export failed to lookup for FSAL %s",
			 omnifsal.subfsal.name);
		return fsalstat(ERR_FSAL_INVAL, EINVAL);
	}

	status = fsal_stack->m_ops.update_export(fsal_stack,
						 omnifsal.subfsal.fsal_node,
						 err_type, original->sub_export,
						 fsal_hdl);
	fsal_put(fsal_stack);

	if (FSAL_IS_ERROR(status)) {
		LogMajor(COMPONENT_FSAL,
			 "Failed to call update_export on underlying FSAL %s",
			 omnifsal.subfsal.name);
	}

	return status;
}
