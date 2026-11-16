# SPDX-License-Identifier: LGPL-3.0-or-later
# Out-of-tree build of FSAL_OMNI against an NFS-Ganesha source + build tree.
#   make GANESHA_SRC=/path/to/nfs-ganesha/src GANESHA_BUILD=/path/to/build
# The module must be built against the exact Ganesha that loads it
# (FSAL_MAJOR_VERSION/FSAL_MINOR_VERSION are checked at register_fsal()).
GANESHA_SRC   ?= /srv/omni-b/src/nfs-ganesha/src
GANESHA_BUILD ?= /srv/omni-b/build/ganesha
GANESHA_LIB   ?= /opt/ganesha/lib

CFLAGS  ?= -O2 -g
CFLAGS  += -fPIC -Wall -Wno-unused-parameter -D_GNU_SOURCE=1 -D__USE_GNU -DHAS_DOFF \
           -D_LARGEFILE64_SOURCE -D_FILE_OFFSET_BITS=64 -fno-strict-aliasing \
           -I$(GANESHA_BUILD)/include -I$(GANESHA_SRC)/include \
           -I$(GANESHA_SRC)/include/os/linux -I$(GANESHA_SRC)/libntirpc/ntirpc \
           -I$(GANESHA_BUILD)/libntirpc/src/monitoring/include \
           -I$(GANESHA_SRC)/libntirpc/src/monitoring/include \
           -I$(GANESHA_BUILD)/monitoring/include -I$(GANESHA_SRC)/monitoring/include
# Link jemalloc explicitly, before libc, exactly as Ganesha does. Without it
# the module binds calloc@GLIBC and Ganesha's jemalloc malloc_usable_size()
# crashes on the first gsh_calloc() (seen in this spike: SIGSEGV at startup).
LDFLAGS += -shared -Wl,--no-undefined -L$(GANESHA_LIB) -lganesha_nfsd -lntirpc -ljemalloc

SRCS = main.c export.c handle.c file.c xattrs.c omni_guard.c
OBJS = $(SRCS:.c=.o)

libfsalomni.so: $(OBJS)
	$(CC) -o $@ $(OBJS) $(LDFLAGS)

%.o: %.c omni_methods.h
	$(CC) $(CFLAGS) -c -o $@ $<

install: libfsalomni.so
	install -m 0755 libfsalomni.so $(GANESHA_LIB)/ganesha/libfsalomni.so

clean:
	rm -f $(OBJS) libfsalomni.so
.PHONY: install clean
