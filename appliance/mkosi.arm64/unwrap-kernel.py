#!/usr/bin/env python3
"""mkosi post-installation script, arm64 only (mkosi.conf.d/20-arm64.conf).

Ubuntu 26.04's arm64 vmlinuz is not the kernel itself: it is a stub image (sections .linux,
.dtbauto, .hwids, .sbat, .uname) that carries the EFI zboot kernel in its .linux section,
for boards that pick a devicetree by hardware id. ukify puts that whole file into the UKI's
.linux, and systemd-stub, which loads the inner kernel by hand, refuses it ("Inner kernel
image contains base relocations, which we do not support"): the box never boots.

So the vmlinuz the UKI is built from is replaced with the zboot kernel it wraps (a PE with no
relocations, which systemd-stub starts). The devicetrees are not needed: the appliance runs
in VMs, which describe themselves with ACPI. A vmlinuz that is already a plain kernel is left
alone. Runs on the build host with $BUILDROOT set by mkosi; standard library only.
"""

import glob
import os
import struct
import sys


def sections(pe):
    if pe[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", pe, 0x3C)[0]
    if pe[off:off + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", pe, off + 6)[0]
    optsz = struct.unpack_from("<H", pe, off + 20)[0]
    out = {}
    for i in range(nsec):
        h = off + 24 + optsz + i * 40
        name = pe[h:h + 8].rstrip(b"\0").decode(errors="replace")
        vsize, _va, rsize, roff = struct.unpack_from("<IIII", pe, h + 8)
        out[name] = (roff, min(vsize, rsize) if vsize else rsize)
    return out


def unwrap(path):
    pe = open(path, "rb").read()
    s = sections(pe)
    if not s or ".linux" not in s:
        print(f"unwrap-kernel: {path} is a plain kernel, left alone")
        return
    roff, size = s[".linux"]
    inner = pe[roff:roff + size]
    if sections(inner) is None:
        sys.exit(f"unwrap-kernel: the .linux section of {path} is not a PE image")
    tmp = path + ".unwrapped"
    with open(tmp, "wb") as f:
        f.write(inner)
    st = os.stat(path)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    kind = "EFI zboot" if inner[4:8] == b"zimg" else "kernel"
    print(f"unwrap-kernel: {path}: the stub's {kind} ({len(inner)} of {len(pe)} bytes; dropped {sorted(set(s) - {'.linux'})})")


root = os.environ["BUILDROOT"]
found = sorted(set(glob.glob(f"{root}/usr/lib/modules/*/vmlinuz") + glob.glob(f"{root}/boot/vmlinuz-*")))
found = [p for p in found if not os.path.islink(p)]
if not found:
    sys.exit("unwrap-kernel: no vmlinuz in the image")
for p in found:
    unwrap(p)
