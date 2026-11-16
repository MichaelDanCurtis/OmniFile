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
 *
 */

/* main.c
 * Module core functions
 */

#include "config.h"

#include "fsal.h"
#include <libgen.h> /* used for 'dirname' */
#include <pthread.h>
#include <string.h>
#include <limits.h>
#include <sys/types.h>
#include "gsh_list.h"
#include "FSAL/fsal_init.h"
#include "config_parsing.h"
#include "omni_methods.h"

/* FSAL name determines name of shared library: libfsal<name>.so */
static const char myname[] = "OMNI";

/* my module private storage
 */

struct omni_fsal_module
	OMNIFS = { .module = { .fs_info = {
				       .maxfilesize = UINT64_MAX,
				       .maxlink = _POSIX_LINK_MAX,
				       .maxnamelen = 1024,
				       .maxpathlen = 1024,
				       .no_trunc = true,
				       .chown_restricted = true,
				       .case_insensitive = false,
				       .case_preserving = true,
				       .link_support = true,
				       .symlink_support = true,
				       .lock_support = true,
				       .lock_support_async_block = false,
				       .named_attr = true,
				       .unique_handles = true,
				       .acl_support = FSAL_ACLSUPPORT_ALLOW,
				       .cansettime = true,
				       .homogenous = true,
				       .supported_attrs = ALL_ATTRIBUTES,
				       .maxread = FSAL_MAXIOSIZE,
				       .maxwrite = FSAL_MAXIOSIZE,
				       .umask = 0,
				       .auth_exportpath_xdev = false,
				       .link_supports_permission_checks = true,
				       .expire_time_parent = -1,
			       } } };

/* Module methods
 */

/* init_config
 * must be called with a reference taken (via lookup_fsal)
 */

/* Module-wide settings, in an optional top-level block:
 *
 *   OMNI {
 *       Socket_Path = "/run/omnifile/fsal.sock";  # the daemon (contract 2)
 *       Completion_Threads = 4;                   # parked-read completions
 *       Park_Max_Per_Client = 16;                 # parked reads per client
 *   }
 */
struct omni_module_conf {
	char *socket_path;
	uint32_t completion_threads;
	uint32_t park_per_client;
};

static struct omni_module_conf omni_conf;

static struct config_item omni_params[] = {
	CONF_ITEM_PATH("Socket_Path", 1, 107, "/run/omnifile/fsal.sock",
		       omni_module_conf, socket_path),
	CONF_ITEM_UI32("Completion_Threads", 1, 64, 4, omni_module_conf,
		       completion_threads),
	CONF_ITEM_UI32("Park_Max_Per_Client", 1, 256, 16, omni_module_conf,
		       park_per_client),
	CONFIG_EOL
};

static struct config_block omni_param = {
	.dbus_interface_name = "org.ganesha.nfsd.config.fsal.omni",
	.blk_desc.name = "OMNI",
	.blk_desc.type = CONFIG_BLOCK,
	.blk_desc.flags = CONFIG_UNIQUE,
	.blk_desc.u.blk.init = noop_conf_init,
	.blk_desc.u.blk.params = omni_params,
	.blk_desc.u.blk.commit = noop_conf_commit
};

static fsal_status_t init_config(struct fsal_module *omni_fsal_module,
				 config_file_t config_struct,
				 struct config_error_type *err_type)
{
	(void)load_config_from_parse(config_struct, &omni_param, &omni_conf,
				     true, err_type);
	if (!config_error_is_harmless(err_type))
		return fsalstat(ERR_FSAL_INVAL, 0);
	omni_conn_configure(omni_conf.socket_path,
			    omni_conf.completion_threads,
			    omni_conf.park_per_client);

	display_fsinfo(omni_fsal_module);
	LogDebug(COMPONENT_FSAL,
		 "FSAL INIT: Supported attributes mask = 0x%" PRIx64,
		 omni_fsal_module->fs_info.supported_attrs);
	return fsalstat(ERR_FSAL_NO_ERROR, 0);
}

/* Module initialization.
 * Called by dlopen() to register the module
 * keep a private pointer to me in myself
 */

/* linkage to the exports and handle ops initializers
 */
MODULE_INIT void omni_init(void)
{
	int retval;
	struct fsal_module *myself = &OMNIFS.module;

	retval = register_fsal(myself, myname, FSAL_MAJOR_VERSION,
			       FSAL_MINOR_VERSION, FSAL_ID_NO_PNFS);
	if (retval != 0) {
		fprintf(stderr, "OMNIFS module failed to register");
		return;
	}
	myself->m_ops.create_export = omni_create_export;
	myself->m_ops.update_export = omni_update_export;
	myself->m_ops.init_config = init_config;

	/* Initialize the fsal_obj_handle ops for FSAL OMNI */
	omni_handle_ops_init(&OMNIFS.handle_ops);
}

MODULE_FINI void omni_unload(void)
{
	int retval;

	omni_conn_stop();

	retval = unregister_fsal(&OMNIFS.module);
	if (retval != 0) {
		fprintf(stderr, "OMNIFS module failed to unregister");
		return;
	}
}
