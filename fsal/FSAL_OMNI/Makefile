# SPDX-License-Identifier: LGPL-3.0-or-later
#
# Out-of-tree build of FSAL_OMNI (libfsalomni.so) against a pinned
# NFS-Ganesha source, build and install tree. Normally run inside the image
# from fsal/build/Containerfile, which sets the GANESHA_* variables:
#
#   make                      # build ./libfsalomni.so (warnings are errors)
#   make install              # copy it to $(GANESHA_LIB)/ganesha/
#   make O=/tmp/omni          # put objects and the .so somewhere else
#
# The module must be built against the exact Ganesha that loads it, with the
# same config.h options: register_fsal() only checks the FSAL API version,
# and struct layouts change with build options (filer spike B, step 4).

GANESHA_SRC    ?= /usr/src/nfs-ganesha/src
GANESHA_BUILD  ?= /usr/src/nfs-ganesha-build
GANESHA_LIB    ?= /opt/ganesha/lib
O              ?= .

# Defines and include paths are the ones Ganesha's own cmake uses for the
# in-tree FSAL_NULL (see $(GANESHA_BUILD)/FSAL/Stackable_FSALs/FSAL_NULL/
# CMakeFiles/fsalnull.dir/flags.make); keep them in step when bumping
# GANESHA_VERSION. Ganesha's headers are included as system headers so that
# the warning flags apply to our code only.
WARNINGS ?= -Wall -Wextra -Werror
CFLAGS   ?= -O2 -g
CPPFLAGS += -DHAS_DOFF -D_GNU_SOURCE=1 -D__USE_GNU -DNDEBUG \
            -D_LARGEFILE64_SOURCE -D_FILE_OFFSET_BITS=64 \
            -isystem $(GANESHA_BUILD)/include \
            -isystem $(GANESHA_SRC)/include \
            -isystem $(GANESHA_BUILD)/libntirpc/src/monitoring/include \
            -isystem $(GANESHA_SRC)/libntirpc/src/monitoring/include \
            -isystem $(GANESHA_BUILD)/monitoring/include \
            -isystem $(GANESHA_SRC)/monitoring/include \
            -isystem $(GANESHA_SRC)/include/os/linux \
            -isystem $(GANESHA_SRC)/libntirpc/ntirpc
ALL_CFLAGS = $(CFLAGS) $(WARNINGS) -fPIC -fno-strict-aliasing

# omni_guard.c mirrors two Ganesha-private structs checked against V15.6 and
# refuses to build against another version. After re-checking them for a new
# Ganesha (README, "Bumping Ganesha"), opt in with, e.g.,
#   make OMNI_READ_MIRRORS_VERIFIED_FOR=15.7
ifdef OMNI_READ_MIRRORS_VERIFIED_FOR
CPPFLAGS += -DOMNI_READ_MIRRORS_VERIFIED_FOR='"$(OMNI_READ_MIRRORS_VERIFIED_FOR)"'
endif

# Link jemalloc explicitly, as Ganesha does. Without it the module binds
# calloc@GLIBC and Ganesha's jemalloc malloc_usable_size() segfaults on the
# first gsh_calloc() in create_export (filer spike B, step 3).
LDFLAGS += -shared -Wl,--no-undefined -Wl,-z,relro -Wl,-z,now -L$(GANESHA_LIB)
LDLIBS  += -lganesha_nfsd -lntirpc -ljemalloc

SRCS = main.c export.c handle.c file.c xattrs.c omni_wire.c omni_guard.c omni_conn.c \
       omni_rewind.c
OBJS = $(addprefix $(O)/,$(SRCS:.c=.o))
LIB  = $(O)/libfsalomni.so

all: $(LIB)

$(LIB): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $(OBJS) $(LDLIBS)
	@# Guard against the spike-B crash: the module must import jemalloc.
	@readelf -d $@ | grep -q 'libjemalloc' || { echo "$@: not linked with jemalloc" >&2; exit 1; }

# config.h is a prerequisite: a Ganesha rebuilt with other options (say,
# ENABLE_VFS_POSIX_ACL) changes struct layouts, so every object rebuilds.
$(O)/%.o: %.c omni_methods.h omni_guard.h omni_wire.h $(GANESHA_BUILD)/include/config.h
	@mkdir -p $(O)
	$(CC) $(CPPFLAGS) $(ALL_CFLAGS) -c -o $@ $<

install: $(LIB)
	install -D -m 0755 $(LIB) $(DESTDIR)$(GANESHA_LIB)/ganesha/libfsalomni.so

clean:
	rm -f $(OBJS) $(LIB)

.PHONY: all install clean
# A failed link check must not leave a usable-looking .so behind.
.DELETE_ON_ERROR:
