// SPDX-License-Identifier: LGPL-3.0-or-later
/*
 * Test-only LD_PRELOAD shim for ganesha.nfsd (fsal/tests/t_marker_cache.sh).
 *
 * While the file /run/omnifile/test-slow-marker exists and holds a number N,
 * every read of the trusted.omnifile xattr (fgetxattr or getxattr) is done
 * at once and then held for N ms before it returns. That widens the window
 * between FSAL_OMNI reading a marker from disk and acting on it (caching
 * it, or doing the I/O it allows), so races with INVALIDATE can be tested
 * deterministically. Each hold is logged to /run/omnifile/test-slow-marker.log
 * (hold_log). Other xattrs and processes are untouched. It is never
 * installed.
 *
 * While /run/omnifile/test-slow-intent holds N, every second READ_INTENT the
 * module sends (send(2) of a record of type 0x0010) is held N ms before it
 * goes out (fsal/tests/t_resume_race.sh). A read that re-parks sends one, so
 * this keeps a re-parked read inside read2 while pushes arrive.
 *
 * It also defines the module's one test hook, omni_test_parked_return (a
 * weak symbol the shipped module leaves undefined): see below.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define TRIGGER "/run/omnifile/test-slow-marker"
#define TRIGGER_INTENT "/run/omnifile/test-slow-intent"
#define TRIGGER_PARK_RETURN "/run/omnifile/test-slow-park-return"
#define HOLD_LOG "/run/omnifile/test-slow-marker.log"
#define MARKER "trusted.omnifile"

/* The number of ms a trigger file holds: 0 if it is absent or out of range. */
static long trigger_ms(const char *trigger)
{
	FILE *f;
	long ms = 0;
	int saved = errno; /* the caller reads errno after us */

	f = fopen(trigger, "re");
	if (f != NULL) {
		if (fscanf(f, "%ld", &ms) != 1 || ms < 0 || ms > 5000)
			ms = 0;
		fclose(f);
	}
	errno = saved;
	return ms;
}

static void sleep_ms(long ms)
{
	struct timespec ts = { .tv_sec = ms / 1000,
			       .tv_nsec = (ms % 1000) * 1000000L };
	int saved = errno;

	if (ms > 0)
		nanosleep(&ts, NULL);
	errno = saved;
}

/* One line per held marker read, "H <tid> <epoch s>" when the hold starts
 * and "R <tid> <epoch s>" when it ends, appended to HOLD_LOG. Tests compare
 * these with the fake daemon's INVALIDATE times instead of guessing at
 * scheduling delays on a loaded host. */
static void hold_log(char what)
{
	struct timespec ts;
	char line[64];
	int fd, n, saved = errno;

	clock_gettime(CLOCK_REALTIME, &ts);
	n = snprintf(line, sizeof(line), "%c %ld %ld.%06ld\n", what,
		     (long)gettid(), (long)ts.tv_sec, ts.tv_nsec / 1000);
	fd = open(HOLD_LOG, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
	if (fd >= 0) {
		ssize_t w = write(fd, line, n); /* best effort: tests notice */

		(void)w;
		close(fd);
	}
	errno = saved;
}

static void hold(const char *name)
{
	long ms;

	if (name == NULL || strcmp(name, MARKER) != 0)
		return;
	ms = trigger_ms(TRIGGER);
	if (ms <= 0)
		return;
	hold_log('H');
	sleep_ms(ms);
	hold_log('R');
}

/* The module's test hook (FSAL_OMNI/omni_guard.h), weak and undefined in
 * the shipped module: called as a parking read2 returns. While
 * /run/omnifile/test-slow-park-return holds N, it sleeps N ms there, as a
 * worker descheduled between read2's return and the protocol marking the
 * request as waiting (fsal/tests/t_resume_race.sh).
 */
void omni_test_parked_return(void)
{
	sleep_ms(trigger_ms(TRIGGER_PARK_RETURN));
}

ssize_t send(int fd, const void *buf, size_t len, int flags)
{
	static ssize_t (*real)(int, const void *, size_t, int);
	static unsigned long n;
	const unsigned char *p = buf;
	long ms;

	if (real == NULL)
		real = (ssize_t (*)(int, const void *, size_t, int))dlsym(
			RTLD_NEXT, "send");
	/* READ_INTENT: u16 type 0x0010, little-endian, first in the header */
	if (len >= 2 && p[0] == 0x10 && p[1] == 0x00) {
		ms = trigger_ms(TRIGGER_INTENT);
		if (ms > 0 && __atomic_add_fetch(&n, 1, __ATOMIC_RELAXED) % 2 == 0)
			sleep_ms(ms);
	}
	return real(fd, buf, len, flags);
}

ssize_t fgetxattr(int fd, const char *name, void *value, size_t size)
{
	static ssize_t (*real)(int, const char *, void *, size_t);
	ssize_t n;

	if (real == NULL)
		real = (ssize_t (*)(int, const char *, void *, size_t))dlsym(
			RTLD_NEXT, "fgetxattr");
	n = real(fd, name, value, size);
	if (n >= 0 || errno != EBADF) /* EBADF: the FSAL retries by path */
		hold(name);
	return n;
}

ssize_t getxattr(const char *path, const char *name, void *value, size_t size)
{
	static ssize_t (*real)(const char *, const char *, void *, size_t);
	ssize_t n;

	if (real == NULL)
		real = (ssize_t (*)(const char *, const char *, void *,
				    size_t))dlsym(RTLD_NEXT, "getxattr");
	n = real(path, name, value, size);
	hold(name);
	return n;
}
