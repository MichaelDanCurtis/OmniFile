// SPDX-License-Identifier: LGPL-3.0-or-later
/*
 * FSAL_OMNI stub guard (OmniFile Gateway filer spike, part B).
 *
 * Copyright (C) 2026 AiLuxis Research
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public License
 * as published by the Free Software Foundation; either version 3 of
 * the License, or (at your option) any later version.
 *
 * Throwaway spike code. Decides whether a read of a stub may go to the
 * sub-FSAL, and asks the OmniFile daemon over a Unix socket when it may not.
 * Fails closed: if anything is unknown, the read never reaches the hole.
 */

#include "config.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <poll.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/un.h>
#include "fsal.h"
#include "fsal_convert.h"
#include "FSAL/fsal_commonlib.h"
#include "client_mgr.h"
#include "omni_methods.h"

#define OMNI_XA_STATE "user.omnifile.state"
#define OMNI_XA_RESIDENT "user.omnifile.resident_bytes"
#define OMNI_XA_KEY "user.omnifile.key"

static const char *omni_client_addr(void)
{
	if (op_ctx != NULL && op_ctx->client != NULL)
		return op_ctx->client->hostaddr_str;
	return "unknown";
}

/* The error a share returns when it cannot ask the daemon. */
fsal_status_t omni_fail_closed(struct omni_fsal_export *exp)
{
	if (exp->policy_kind == OMNI_POLICY_WAIT)
		return fsalstat(ERR_FSAL_DELAY, EAGAIN);
	return fsalstat(ERR_FSAL_IO, EIO);
}

/* Read one xattr of the sub-FSAL object as a NUL-terminated string.
 * Returns 0, ENODATA, or another errno. Caller has op_ctx on the sub export.
 */
static int omni_get_xattr(struct omni_fsal_obj_handle *h, const char *name,
			  char *buf, size_t buflen)
{
	size_t outsz = 0;
	fsal_status_t st;

	st = h->sub_handle->obj_ops->getextattr_value_by_name(
		h->sub_handle, name, buf, buflen - 1, &outsz);
	if (FSAL_IS_ERROR(st))
		return st.minor != 0 ? (int)st.minor : EIO;
	if (outsz >= buflen)
		outsz = buflen - 1;
	buf[outsz] = '\0';
	return 0;
}

/* Fill *out with the stub state of the object. */
int omni_stub_info(struct omni_fsal_export *exp, struct omni_fsal_obj_handle *h,
		   struct omni_stub_info *out)
{
	char buf[64];
	char *end;
	int rc;

	memset(out, 0, sizeof(*out));

	op_ctx->fsal_export = exp->export.sub_export;
	rc = omni_get_xattr(h, OMNI_XA_STATE, buf, sizeof(buf));
	if (rc == ENODATA) {
		out->kind = OMNI_NOT_MANAGED;
		rc = 0;
		goto out;
	}
	if (rc != 0)
		goto out;
	if (strcmp(buf, "resident") == 0) {
		out->kind = OMNI_RESIDENT;
		goto out;
	}
	if (strcmp(buf, "stub") != 0) {
		rc = EINVAL; /* unknown state: fail closed */
		goto out;
	}
	out->kind = OMNI_STUB;
	rc = omni_get_xattr(h, OMNI_XA_RESIDENT, buf, sizeof(buf));
	if (rc == ENODATA) {
		out->resident_bytes = 0;
		rc = 0;
	} else if (rc == 0) {
		errno = 0;
		out->resident_bytes = strtoull(buf, &end, 10);
		if (errno != 0 || end == buf) {
			rc = EINVAL;
			goto out;
		}
	} else {
		goto out;
	}
	if (omni_get_xattr(h, OMNI_XA_KEY, out->key, sizeof(out->key)) != 0)
		out->key[0] = '\0';
out:
	op_ctx->fsal_export = &exp->export;
	return rc;
}

/* Append s to the buffer as a JSON string (with quotes). */
static void json_str(char *dst, size_t dstlen, size_t *pos, const char *s)
{
	size_t p = *pos;

	if (s == NULL) {
		p += snprintf(dst + p, p < dstlen ? dstlen - p : 0, "null");
		*pos = p;
		return;
	}
	if (p < dstlen)
		dst[p] = '"';
	p++;
	for (; *s != '\0'; s++) {
		unsigned char c = (unsigned char)*s;

		if (p + 8 >= dstlen)
			break;
		if (c == '"' || c == '\\') {
			dst[p++] = '\\';
			dst[p++] = c;
		} else if (c < 0x20) {
			p += snprintf(dst + p, dstlen - p, "\\u%04x", c);
		} else {
			dst[p++] = c;
		}
	}
	if (p < dstlen)
		dst[p] = '"';
	p++;
	*pos = p;
}

/* One request, one reply, one connection. Returns 0 and fills reply,
 * or an errno (ECONNREFUSED, ENOENT, ETIMEDOUT, EPROTO ...).
 */
static int omni_ask(struct omni_fsal_export *exp, const char *req,
		    char *reply, size_t replylen)
{
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	struct timeval tv;
	size_t len = strlen(req), done = 0;
	int fd, rc = 0;
	ssize_t n;

	fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return errno;
	tv.tv_sec = exp->timeout_ms / 1000;
	tv.tv_usec = (exp->timeout_ms % 1000) * 1000;
	(void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	(void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	strncpy(sa.sun_path, exp->socket_path, sizeof(sa.sun_path) - 1);
	if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
		rc = errno;
		goto out;
	}
	while (done < len) {
		n = send(fd, req + done, len - done, MSG_NOSIGNAL);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			rc = errno == EAGAIN ? ETIMEDOUT : errno;
			goto out;
		}
		done += n;
	}
	done = 0;
	while (done < replylen - 1) {
		n = recv(fd, reply + done, replylen - 1 - done, 0);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			rc = errno == EAGAIN ? ETIMEDOUT : errno;
			goto out;
		}
		if (n == 0)
			break;
		done += n;
		if (memchr(reply, '\n', done) != NULL)
			break;
	}
	reply[done] = '\0';
	if (done == 0)
		rc = EPROTO;
out:
	close(fd);
	return rc;
}

static uint64_t now_us(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* Guard a read of [offset, offset+len) on a regular file.
 * Returns NO_ERROR to pass through, DELAY or IO otherwise.
 */
fsal_status_t omni_guard_read(struct omni_fsal_export *exp,
			      struct omni_fsal_obj_handle *h, const char *op,
			      uint64_t offset, uint64_t len)
{
	struct omni_stub_info si;
	char req[2048], reply[256];
	size_t pos = 0;
	const char *decision = "?";
	fsal_status_t ret;
	uint64_t t0;
	int rc;

	rc = omni_stub_info(exp, h, &si);
	if (rc != 0) {
		LogCrit(COMPONENT_FSAL,
			"OMNI %s: cannot read stub xattrs of ino=%" PRIu64
			" path=%s (errno %d); failing closed",
			op, h->obj_handle.fileid, h->path ? h->path : "?", rc);
		return omni_fail_closed(exp);
	}
	if (si.kind != OMNI_STUB)
		return fsalstat(ERR_FSAL_NO_ERROR, 0);

	if (offset + len <= si.resident_bytes) {
		LogEvent(COMPONENT_FSAL,
			 "OMNI %s local: uid=%u gid=%u client=%s nfs=%u.%u path=%s off=%" PRIu64
			 " len=%" PRIu64 " resident=%" PRIu64,
			 op, op_ctx->creds.caller_uid, op_ctx->creds.caller_gid,
			 omni_client_addr(), op_ctx->nfs_vers,
			 op_ctx->nfs_minorvers, h->path ? h->path : "?", offset,
			 len, si.resident_bytes);
		return fsalstat(ERR_FSAL_NO_ERROR, 0);
	}

	pos += snprintf(req + pos, sizeof(req) - pos, "{\"op\":");
	json_str(req, sizeof(req), &pos, op);
	pos += snprintf(req + pos, sizeof(req) - pos, ",\"path\":");
	json_str(req, sizeof(req), &pos, h->path);
	pos += snprintf(req + pos, sizeof(req) - pos, ",\"key\":");
	json_str(req, sizeof(req), &pos, si.key);
	pos += snprintf(req + pos, sizeof(req) - pos,
			",\"ino\":%" PRIu64 ",\"offset\":%" PRIu64
			",\"len\":%" PRIu64 ",\"uid\":%u,\"gid\":%u"
			",\"nfs\":\"%u.%u\",\"client\":",
			h->obj_handle.fileid, offset, len,
			op_ctx->creds.caller_uid, op_ctx->creds.caller_gid,
			op_ctx->nfs_vers, op_ctx->nfs_minorvers);
	json_str(req, sizeof(req), &pos, omni_client_addr());
	pos += snprintf(req + pos, sizeof(req) - pos, ",\"policy\":");
	json_str(req, sizeof(req), &pos, exp->policy);
	if (pos + 2 >= sizeof(req)) {
		LogCrit(COMPONENT_FSAL, "OMNI request too long; failing closed");
		return omni_fail_closed(exp);
	}
	req[pos++] = '}';
	req[pos++] = '\n';
	req[pos] = '\0';

	t0 = now_us();
	rc = omni_ask(exp, req, reply, sizeof(reply));
	if (rc != 0) {
		ret = omni_fail_closed(exp);
		decision = ret.major == ERR_FSAL_DELAY ? "delay(fail-closed)"
						       : "eio(fail-closed)";
		LogWarn(COMPONENT_FSAL,
			"OMNI %s: daemon %s unreachable (%s); uid=%u client=%s path=%s off=%" PRIu64
			" -> %s",
			op, exp->socket_path, strerror(rc),
			op_ctx->creds.caller_uid, omni_client_addr(),
			h->path ? h->path : "?", offset, decision);
		return ret;
	}
	if (strstr(reply, "\"allow\"") != NULL) {
		ret = fsalstat(ERR_FSAL_NO_ERROR, 0);
		decision = "allow";
	} else if (strstr(reply, "\"delay\"") != NULL) {
		ret = fsalstat(ERR_FSAL_DELAY, EAGAIN);
		decision = "delay";
	} else if (strstr(reply, "\"eio\"") != NULL) {
		ret = fsalstat(ERR_FSAL_IO, EIO);
		decision = "eio";
	} else {
		ret = omni_fail_closed(exp);
		decision = "bad-reply(fail-closed)";
	}
	LogEvent(COMPONENT_FSAL,
		 "OMNI %s stub: uid=%u gid=%u client=%s nfs=%u.%u path=%s ino=%" PRIu64
		 " off=%" PRIu64 " len=%" PRIu64 " resident=%" PRIu64
		 " policy=%s decision=%s rtt_us=%" PRIu64,
		 op, op_ctx->creds.caller_uid, op_ctx->creds.caller_gid,
		 omni_client_addr(), op_ctx->nfs_vers, op_ctx->nfs_minorvers,
		 h->path ? h->path : "?", h->obj_handle.fileid, offset, len,
		 si.resident_bytes, exp->policy, decision, now_us() - t0);
	return ret;
}

/* Remember a best-effort path for the daemon (stale after rename). */
void omni_set_path(struct fsal_obj_handle *obj, const char *parent,
		   const char *name)
{
	struct omni_fsal_obj_handle *h;
	size_t n;

	if (obj == NULL)
		return;
	h = container_of(obj, struct omni_fsal_obj_handle, obj_handle);
	if (h->path != NULL)
		return;
	if (parent == NULL) {
		h->path = gsh_strdup(name, MEM_COMP_FSAL);
		return;
	}
	n = strlen(parent) + strlen(name) + 2;
	h->path = gsh_malloc(n, MEM_COMP_FSAL);
	snprintf(h->path, n, "%s/%s", parent, name);
}

int omni_parse_policy(const char *policy, uint32_t *quick_ms)
{
	char *end;

	*quick_ms = 0;
	if (strcmp(policy, "wait") == 0)
		return OMNI_POLICY_WAIT;
	if (strcmp(policy, "failfast") == 0)
		return OMNI_POLICY_FAILFAST;
	if (strncmp(policy, "waitquick:", 10) == 0) {
		errno = 0;
		*quick_ms = strtoul(policy + 10, &end, 10);
		if (errno == 0 && end != policy + 10 && *end == '\0')
			return OMNI_POLICY_WAITQUICK;
	}
	return -1;
}
