#!/bin/bash
# Build NFS-Ganesha V15.6 and the out-of-tree FSAL_OMNI module inside the spike VM.
#   limactl shell --workdir / omnifile-spike -- sudo /work/spikes/filer/fsal/build.sh
set -euo pipefail
TAG=${TAG:-V15.6}
B=/srv/omni-b
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -o DPkg::Lock::Timeout=600 build-essential cmake bison flex pkg-config \
	libkrb5-dev libcap-dev libblkid-dev libdbus-1-dev liburcu-dev libjemalloc-dev libnsl-dev \
	libacl1-dev uuid-dev libnfsidmap-dev libsqlite3-dev libssl-dev libattr1-dev \
	xfsprogs nfs-common rpcbind attr python3 >/dev/null
mkdir -p $B/src $B/build/ganesha $B/build/omni
[ -d $B/src/nfs-ganesha ] || git clone -q --depth 1 --branch $TAG --recurse-submodules \
	--shallow-submodules https://github.com/nfs-ganesha/nfs-ganesha.git $B/src/nfs-ganesha
cd $B/build/ganesha
cmake $B/src/nfs-ganesha/src -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=/opt/ganesha \
	-DSYSCONFDIR=/opt/ganesha/etc -DUSE_FSAL_PROXY_V4=OFF -DUSE_FSAL_PROXY_V3=OFF \
	-DUSE_FSAL_LUSTRE=OFF -DUSE_FSAL_KVSFS=OFF -DUSE_FSAL_CEPH=OFF -DUSE_FSAL_GPFS=OFF \
	-DUSE_FSAL_GLUSTER=OFF -DUSE_FSAL_RGW=OFF -DUSE_FSAL_MEM=OFF -DUSE_FSAL_SAUNAFS=OFF \
	-DUSE_FSAL_NULL=ON -DUSE_MONITORING=OFF -DUSE_9P=OFF -DUSE_GUI_ADMIN_TOOLS=OFF \
	-DUSE_RADOS_RECOV=OFF -DRADOS_URLS=OFF -DUSE_LTTNG=OFF -D_MSPAC_SUPPORT=OFF \
	-DUSE_UNWIND=OFF -DUSE_DBUS=ON >/dev/null
make -j"$(nproc)" >/dev/null && make install >/dev/null
mkdir -p /opt/ganesha/var/lib/nfs/ganesha /opt/ganesha/var/run/ganesha
# FSAL_OMNI: copied out of the repo so build products stay out of /work
cp /work/spikes/filer/fsal/FSAL_OMNI/* $B/build/omni/
make -C $B/build/omni clean >/dev/null
make -C $B/build/omni GANESHA_SRC=$B/src/nfs-ganesha/src GANESHA_BUILD=$B/build/ganesha \
	GANESHA_LIB=/opt/ganesha/lib install
echo "built: $(ls /opt/ganesha/lib/ganesha)"
