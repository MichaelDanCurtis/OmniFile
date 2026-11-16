/*
 * vfs_omnifile_prevver: Previous Versions of files in unfilled rewind labels.
 *
 * OmniFile Gateway shows a share's rewind trees (<data>/.omni/snap/<share>/
 * <label>/...) as Windows Previous Versions through vfs_shadow_copy2. The
 * edge daemon fills a rewind directory only when someone opens it: the
 * smb-guard holds smbd's open of an unfilled directory (DIR_UNPOPULATED) while
 * the daemon fills it from the service's as-of listing
 * (docs/design/fsal-namespace.md section 7).
 *
 * Browsing a previous version's folders opens them, so they fill. Opening a
 * file directly ("Previous Versions -> Open", or a @GMT- path) opens no
 * directory: shadow_copy2 only stats the path, which does not exist yet. This
 * module, stacked before shadow_copy2, opens each directory of such a path
 * under shadow:snapdir/<label>/ first. The guard holds each open until that
 * directory is filled, so the path exists when shadow_copy2 resolves it.
 *
 * It talks to nothing: the guard and the daemon do the work. A path it
 * cannot open is simply left to shadow_copy2, which answers as it always
 * does. It only ever opens directories, read-only, as the session's user.
 *
 *   vfs objects = full_audit omnifile_prevver shadow_copy2
 *
 * It reads shadow_copy2's own parameters (shadow:snapdir, shadow:format,
 * shadow:localtime) and needs shadow:snapdir to be absolute, as OmniFile's
 * stanza (ShareConfig::render_smb) sets it.
 *
 * Copyright (C) 2026 the OmniFile Gateway authors.
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation; either version 3 of the License, or (at your option)
 * any later version. See COPYING. It runs inside smbd (GPL-3) and is never
 * linked into any OmniFile binary (decision 36).
 */

#include "includes.h"
#include "smbd/smbd.h"

#define MODULE "omnifile_prevver"

/* Open the directory at path, then close it. Returns false if it can't be
 * opened (missing, or not a directory: the end of the chain). */
static bool omni_touch_dir(const char *path)
{
	int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);

	if (fd < 0) {
		DBG_DEBUG(MODULE ": %s: %s\n", path, strerror(errno));
		return false;
	}
	close(fd);
	return true;
}

/*
 * twrp: the previous version's time. rel: the path inside the share (relative
 * to its root, or absolute under the share's connectpath).
 */
static void omni_prefill(vfs_handle_struct *handle, NTTIME twrp, const char *rel)
{
	TALLOC_CTX *frame = NULL;
	const char *snapdir = NULL;
	const char *fmt = NULL;
	const char *connectpath = NULL;
	bool use_localtime;
	time_t t;
	struct tm tm;
	char label[256];
	char *path = NULL;
	char *copy = NULL;
	char *save = NULL;
	char *comp = NULL;
	size_t cplen;

	if (twrp == 0 || rel == NULL) {
		return;
	}
	snapdir = lp_parm_const_string(SNUM(handle->conn), "shadow", "snapdir", NULL);
	if (snapdir == NULL || snapdir[0] != '/') {
		return;
	}
	fmt = lp_parm_const_string(SNUM(handle->conn), "shadow", "format",
				   "@GMT-%Y.%m.%d-%H.%M.%S");
	use_localtime = lp_parm_bool(SNUM(handle->conn), "shadow", "localtime", false);
	t = nt_time_to_unix(twrp);
	if ((use_localtime ? localtime_r(&t, &tm) : gmtime_r(&t, &tm)) == NULL) {
		return;
	}
	if (strftime(label, sizeof(label), fmt, &tm) == 0) {
		return;
	}

	/* An absolute path must be under the share's root. */
	if (rel[0] == '/') {
		connectpath = handle->conn->connectpath;
		if (connectpath == NULL) {
			return;
		}
		cplen = strlen(connectpath);
		if (strncmp(rel, connectpath, cplen) != 0 ||
		    (rel[cplen] != '/' && rel[cplen] != '\0')) {
			return;
		}
		rel += cplen;
	}

	frame = talloc_stackframe();
	path = talloc_asprintf(frame, "%s/%s", snapdir, label);
	copy = talloc_strdup(frame, rel);
	if (path == NULL || copy == NULL) {
		goto out;
	}
	/* The label, then each directory below it. The guard holds the open
	 * of an unfilled one until it is filled. The last component may be
	 * the file itself: its open fails (ENOTDIR) and the walk ends. */
	if (!omni_touch_dir(path)) {
		goto out;
	}
	for (comp = strtok_r(copy, "/", &save); comp != NULL;
	     comp = strtok_r(NULL, "/", &save)) {
		if (strcmp(comp, ".") == 0) {
			continue;
		}
		if (strcmp(comp, "..") == 0) {
			break; /* never walk out of the label */
		}
		path = talloc_asprintf_append(path, "/%s", comp);
		if (path == NULL || !omni_touch_dir(path)) {
			break;
		}
	}
out:
	TALLOC_FREE(frame);
}

/* The same for a name relative to dirfsp. */
static void omni_prefill_at(vfs_handle_struct *handle,
			    const struct files_struct *dirfsp,
			    const struct smb_filename *smb_fname)
{
	struct smb_filename *full = NULL;
	NTTIME twrp = smb_fname->twrp;

	if (twrp == 0 && dirfsp != NULL && dirfsp->fsp_name != NULL) {
		twrp = dirfsp->fsp_name->twrp;
	}
	if (twrp == 0) {
		return;
	}
	if (dirfsp == NULL) {
		omni_prefill(handle, twrp, smb_fname->base_name);
		return;
	}
	full = full_path_from_dirfsp_atname(talloc_tos(), dirfsp, smb_fname);
	if (full == NULL) {
		return;
	}
	omni_prefill(handle, twrp, full->base_name);
	TALLOC_FREE(full);
}

static int omni_stat(vfs_handle_struct *handle, struct smb_filename *smb_fname)
{
	omni_prefill(handle, smb_fname->twrp, smb_fname->base_name);
	return SMB_VFS_NEXT_STAT(handle, smb_fname);
}

static int omni_lstat(vfs_handle_struct *handle, struct smb_filename *smb_fname)
{
	omni_prefill(handle, smb_fname->twrp, smb_fname->base_name);
	return SMB_VFS_NEXT_LSTAT(handle, smb_fname);
}

static int omni_fstatat(vfs_handle_struct *handle,
			const struct files_struct *dirfsp,
			const struct smb_filename *smb_fname,
			SMB_STRUCT_STAT *sbuf,
			int flags)
{
	omni_prefill_at(handle, dirfsp, smb_fname);
	return SMB_VFS_NEXT_FSTATAT(handle, dirfsp, smb_fname, sbuf, flags);
}

static int omni_openat(vfs_handle_struct *handle,
		       const struct files_struct *dirfsp,
		       const struct smb_filename *smb_fname,
		       struct files_struct *fsp,
		       const struct vfs_open_how *how)
{
	omni_prefill_at(handle, dirfsp, smb_fname);
	return SMB_VFS_NEXT_OPENAT(handle, dirfsp, smb_fname, fsp, how);
}

/*
 * The VFS interface version and Samba release this object was compiled
 * against, as a plain string in the .so. smbd refuses a module built for
 * another VFS interface (and then every share that names it stops
 * connecting), so omnifile-acl (smb_module.rs) and samba-vfs/build.sh read it
 * without loading the module and compare it with the running smbd. Keep the
 * prefix and the key=value form: both are parsed. build.sh passes the release
 * it builds in as OMNIFILE_SAMBA_{MAJOR,MINOR,PATCH}.
 */
#define OMNI_STR2(x) #x
#define OMNI_STR(x) OMNI_STR2(x)
#ifdef OMNIFILE_SAMBA_MAJOR
#define OMNI_SAMBA_RELEASE                                                  \
	OMNI_STR(OMNIFILE_SAMBA_MAJOR) "." OMNI_STR(OMNIFILE_SAMBA_MINOR) \
	"." OMNI_STR(OMNIFILE_SAMBA_PATCH)
#else
#define OMNI_SAMBA_RELEASE "unknown"
#endif
const char omnifile_prevver_build_info[]
	__attribute__((used, visibility("default"))) =
	"OMNIFILE-PREVVER-BUILD vfs_interface="
	OMNI_STR(SMB_VFS_INTERFACE_VERSION)
	" samba=" OMNI_SAMBA_RELEASE;

static struct vfs_fn_pointers vfs_omnifile_prevver_fns = {
	.stat_fn = omni_stat,
	.lstat_fn = omni_lstat,
	.fstatat_fn = omni_fstatat,
	.openat_fn = omni_openat,
};

static_decl_vfs;
NTSTATUS vfs_omnifile_prevver_init(TALLOC_CTX *ctx)
{
	return smb_register_vfs(SMB_VFS_INTERFACE_VERSION,
				MODULE, &vfs_omnifile_prevver_fns);
}
