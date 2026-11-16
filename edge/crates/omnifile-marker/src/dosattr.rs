//! `FILE_ATTRIBUTE_OFFLINE` for SMB clients, kept in step with the marker.
//!
//! Windows treats a file with the offline attribute as one whose data "has
//! been physically moved to offline storage" (hierarchical storage
//! management): Explorer shows it as offline and does not open it for
//! thumbnails or previews, and the Windows indexer skips it. That is what a
//! stub is, and it keeps someone browsing a folder of seismic data from
//! starting a recall of every file in it (pilot risk R4).
//!
//! Samba keeps a file's DOS attributes in the `user.DOSATTRIB` xattr
//! (`store dos attributes = yes`, its default) and reports the offline bit
//! from it. The value is Samba's NDR `xattr_DOSATTRIB`: a NUL-terminated
//! hex string (empty in what Samba writes now), a `u16` version, the union's
//! `u16` level, then the version's struct, aligned to 4. Its `attrib` is the
//! struct's first field in version 1, and follows `valid_flags` in versions
//! 3, 4 and 5. Only that bit is changed: the creation time and every other
//! attribute Samba stored stay as they were. A value in a layout this does
//! not know is left alone.
//!
//! The bit is advisory. A user may clear it over SMB, and nothing relies on
//! it: the marker and the guard decide what a read gets. When the FSAL drops
//! a marker itself (an NFS truncate of a stub), the bit stays until the
//! daemon commits the new content and writes its marker.
//!
//! Cost: about 36 bytes of xattr per placeholder. On xfs with 512-byte inodes
//! a placeholder's attributes stay in the inode with up to 8 named entries in
//! its access ACL (11 without the bit); beyond that they take a 4 KiB block.
//! Shares use three groups (Owners, Writers, Readers).

use std::io;
use std::os::fd::{AsRawFd, BorrowedFd};
use std::sync::atomic::{AtomicBool, Ordering};

use crate::xattr::{get_raw, set_raw};
use crate::{Marker, State};

pub const NAME: &str = "user.DOSATTRIB";
pub const FILE_ATTRIBUTE_ARCHIVE: u32 = 0x20;
pub const FILE_ATTRIBUTE_OFFLINE: u32 = 0x1000;
/// `XATTR_DOSINFO_ATTRIB` in `valid_flags`: `attrib` is set.
const VALID_ATTRIB: u32 = 0x1;
/// Larger than any `xattr_DOSATTRIB` Samba writes.
const CAP: usize = 256;

static ENABLED: AtomicBool = AtomicBool::new(true);

/// Mark stubs offline for SMB clients (the default) or leave DOS attributes
/// alone (`[shares] smb_offline = false`).
pub fn set_enabled(on: bool) {
    ENABLED.store(on, Ordering::Relaxed);
}

pub fn enabled() -> bool {
    ENABLED.load(Ordering::Relaxed)
}

/// Whether a file with this marker is offline: some of its bytes are not local.
pub fn offline(m: &Marker) -> bool {
    m.state == State::Partial
}

/// Set or clear the offline bit on a regular file. Returns whether the
/// value changed. Writes nothing when the bit is already as asked, so a
/// fill that widens the marker range by range costs one `fgetxattr` each.
/// Turned off (`set_enabled(false)`), it only ever clears the bit.
///
/// smbd may write the same xattr at the same moment (a user setting Hidden
/// or ReadOnly over SMB). The write is conditional on what was read being
/// there or not (`XATTR_CREATE` / `XATTR_REPLACE`) and is tried once more
/// after a race; a change smbd makes between this read and write can still
/// be lost. The bit itself is advisory.
pub fn sync(fd: BorrowedFd<'_>, offline: bool) -> io::Result<bool> {
    if !is_regular(fd)? {
        return Ok(false);
    }
    let offline = offline && enabled();
    let mut tries = 0;
    loop {
        tries += 1;
        let current = get_raw(fd, NAME, CAP)?;
        let (new, flag) = match &current {
            None if !offline => return Ok(false),
            None => (
                fresh(FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_OFFLINE),
                libc::XATTR_CREATE,
            ),
            Some(v) => match with_offline(v, offline) {
                Some(n) if n != *v => (n, libc::XATTR_REPLACE),
                _ => return Ok(false),
            },
        };
        match set_raw(fd, NAME, &new, flag) {
            Ok(()) => return Ok(true),
            Err(e)
                if tries < 2
                    && matches!(e.raw_os_error(), Some(libc::EEXIST) | Some(libc::ENODATA)) =>
            {
                continue;
            }
            Err(e) => return Err(e),
        }
    }
}

/// The attributes in a `user.DOSATTRIB` value, if its layout is known.
pub fn attrib(v: &[u8]) -> Option<u32> {
    let at = attrib_offset(v)?;
    Some(u32::from_le_bytes(v[at..at + 4].try_into().ok()?))
}

/// `v` with the offline bit set or cleared, or `None` for a layout this
/// does not know.
pub fn with_offline(v: &[u8], offline: bool) -> Option<Vec<u8>> {
    let at = attrib_offset(v)?;
    let mut out = v.to_vec();
    let mut a = u32::from_le_bytes(out[at..at + 4].try_into().ok()?);
    if offline {
        a |= FILE_ATTRIBUTE_OFFLINE;
    } else {
        a &= !FILE_ATTRIBUTE_OFFLINE;
    }
    out[at..at + 4].copy_from_slice(&a.to_le_bytes());
    // Versions 3 to 5: `attrib` counts only with its valid flag. When it
    // wasn't valid, Samba derived the attributes from the mode bits; ARCHIVE
    // is what it derives by default.
    if offline && version_of(v)? >= 3 {
        let f = at - 4;
        let flags = u32::from_le_bytes(out[f..f + 4].try_into().ok()?);
        if flags & VALID_ATTRIB == 0 {
            out[f..f + 4].copy_from_slice(&(flags | VALID_ATTRIB).to_le_bytes());
            out[at..at + 4].copy_from_slice(&(FILE_ATTRIBUTE_ARCHIVE | a).to_le_bytes());
        }
    }
    Some(out)
}

/// A version 5 value holding `attrib` alone (no creation time: Samba then
/// takes it from the inode).
pub fn fresh(attrib: u32) -> Vec<u8> {
    let mut v = vec![0u8; 24]; // the creation time stays 0, not marked valid
    v[2..4].copy_from_slice(&5u16.to_le_bytes()); // version
    v[4..6].copy_from_slice(&5u16.to_le_bytes()); // union level
    v[8..12].copy_from_slice(&VALID_ATTRIB.to_le_bytes());
    v[12..16].copy_from_slice(&attrib.to_le_bytes());
    v
}

fn version_of(v: &[u8]) -> Option<u16> {
    let nul = v.iter().position(|&b| b == 0)?;
    let ver_at = (nul + 1).next_multiple_of(2);
    Some(u16::from_le_bytes(
        v.get(ver_at..ver_at + 2)?.try_into().ok()?,
    ))
}

fn attrib_offset(v: &[u8]) -> Option<usize> {
    let nul = v.iter().position(|&b| b == 0)?;
    let ver_at = (nul + 1).next_multiple_of(2);
    let version = version_of(v)?;
    let level = u16::from_le_bytes(v.get(ver_at + 2..ver_at + 4)?.try_into().ok()?);
    if level != version {
        return None;
    }
    let info = (ver_at + 4).next_multiple_of(4);
    let (at, len) = match version {
        1 => (info, 40),
        // valid_flags, attrib, ea_size, size, alloc_size, create_time.
        3 => (info + 4, 36),
        4 => (info + 4, 24),
        5 => (info + 4, 16),
        _ => return None,
    };
    (v.len() >= info + len).then_some(at)
}

fn is_regular(fd: BorrowedFd<'_>) -> io::Result<bool> {
    // SAFETY: st is written by fstat before it is read.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd.as_raw_fd(), &mut st) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(st.st_mode & libc::S_IFMT == libc::S_IFREG)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// What Samba 4.23 wrote for a file set hidden over SMB (e2e container,
    /// 2026-10-16): version 5, attrib and creation time valid.
    const SAMBA_V5: &str = "00000500050000001100000002000000f77d67b2415ddd01";

    fn hex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn sets_and_clears_only_the_offline_bit_in_what_samba_wrote() {
        let v = hex(SAMBA_V5);
        assert_eq!(attrib(&v), Some(0x2));
        let on = with_offline(&v, true).unwrap();
        assert_eq!(attrib(&on), Some(0x1002));
        // The flags and the creation time are untouched.
        assert_eq!(on[..12], v[..12]);
        assert_eq!(on[16..], v[16..]);
        assert_eq!(with_offline(&on, false).unwrap(), v);
    }

    #[test]
    fn a_fresh_value_is_version_5_with_attrib_valid() {
        let v = fresh(FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_OFFLINE);
        assert_eq!(attrib(&v), Some(0x1020));
        assert_eq!(&v[8..12], &1u32.to_le_bytes());
    }

    #[test]
    fn an_attrib_not_marked_valid_becomes_valid_with_archive() {
        let mut v = hex(SAMBA_V5);
        v[8..12].copy_from_slice(&0x10u32.to_le_bytes()); // create time only
        v[12..16].copy_from_slice(&0u32.to_le_bytes());
        let on = with_offline(&v, true).unwrap();
        assert_eq!(&on[8..12], &0x11u32.to_le_bytes());
        assert_eq!(attrib(&on), Some(0x1020));
    }

    #[test]
    fn a_legacy_hex_string_prefix_is_skipped() {
        // An older Samba's "0x20" string before a version 1 struct.
        let mut v = b"0x20\0".to_vec();
        v.resize(6, 0);
        v.extend_from_slice(&1u16.to_le_bytes());
        v.extend_from_slice(&1u16.to_le_bytes());
        v.resize(12, 0);
        v.extend_from_slice(&0x20u32.to_le_bytes());
        v.resize(12 + 40, 0);
        assert_eq!(attrib(&v), Some(0x20));
        assert_eq!(attrib(&with_offline(&v, true).unwrap()), Some(0x1020));
    }

    #[test]
    fn unknown_or_short_layouts_are_left_alone() {
        assert_eq!(with_offline(b"", true), None);
        assert_eq!(with_offline(b"0x1020", true), None); // no NUL
        let v = hex(SAMBA_V5);
        assert_eq!(with_offline(&v[..14], true), None);
        let mut v2 = v.clone();
        v2[2] = 9; // version 9
        assert_eq!(with_offline(&v2, true), None);
    }
}
