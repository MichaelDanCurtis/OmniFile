//! Parsing `/proc/self/fdinfo/<group fd>` (`fs/notify/fdinfo.c`).
//!
//! This is how the guard proves, rather than assumes, that a group it took
//! back from the fd store is a pre-content group with an unlimited queue,
//! and that its filesystem mark sits on the data volume (`sdev`) and not on
//! the root filesystem.
//!
//! ```text
//! fanotify flags:18 event-flags:8000
//! fanotify sdev:700003 mflags:0 mask:100000 ignored_mask:0
//! fanotify mnt_id:52 mflags:2 mask:0 ignored_mask:100000
//! fanotify ino:83 sdev:700003 mflags:4 mask:2 ignored_mask:100000 fhandle-bytes:...
//! ```

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct GroupInfo {
    /// `fanotify_init` flags (`FAN_CLASS_*`, `FAN_UNLIMITED_QUEUE`, ...).
    pub flags: Option<u32>,
    pub event_flags: Option<u32>,
    /// Filesystem marks: `(sdev, mask, ignored_mask)`, `sdev` in the
    /// kernel's `dev_t` encoding (`major << 20 | minor`).
    pub sb_marks: Vec<(u32, u32, u32)>,
    /// Mount marks: `(mnt_id, mask, ignored_mask)`.
    pub mnt_marks: Vec<(u32, u32, u32)>,
    /// Inode marks: `(ino, sdev, mask, ignored_mask)`.
    pub inode_marks: Vec<(u64, u32, u32, u32)>,
}

fn field(line: &str, name: &str) -> Option<u64> {
    line.split_whitespace()
        .find_map(|w| w.strip_prefix(name))
        .and_then(|v| u64::from_str_radix(v, 16).ok())
}

/// Parse the text of a fanotify group's fdinfo.
pub fn parse(text: &str) -> GroupInfo {
    let mut g = GroupInfo::default();
    for line in text.lines() {
        let Some(rest) = line.strip_prefix("fanotify ") else {
            continue;
        };
        let mask = field(rest, "mask:").unwrap_or(0) as u32;
        let ign = field(rest, "ignored_mask:").unwrap_or(0) as u32;
        if rest.starts_with("flags:") {
            g.flags = field(rest, "flags:").map(|v| v as u32);
            g.event_flags = field(rest, "event-flags:").map(|v| v as u32);
        } else if rest.starts_with("ino:") {
            if let (Some(ino), Some(sdev)) = (field(rest, "ino:"), field(rest, "sdev:")) {
                g.inode_marks.push((ino, sdev as u32, mask, ign));
            }
        } else if rest.starts_with("sdev:") {
            if let Some(sdev) = field(rest, "sdev:") {
                g.sb_marks.push((sdev as u32, mask, ign));
            }
        } else if rest.starts_with("mnt_id:")
            && let Some(id) = field(rest, "mnt_id:")
        {
            g.mnt_marks.push((id as u32, mask, ign));
        }
    }
    g
}

/// The kernel's `dev_t` encoding of a userspace `st_dev` (as printed in
/// fdinfo): `major << 20 | minor`.
pub fn kernel_dev(major: u32, minor: u32) -> u32 {
    (major << 20) | (minor & 0xfffff)
}

/// `mnt_id:` from `/proc/self/fdinfo/<fd>` of an ordinary file or directory.
pub fn mnt_id(text: &str) -> Option<u32> {
    text.lines()
        .find_map(|l| l.strip_prefix("mnt_id:"))
        .and_then(|v| v.trim().parse().ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "pos:\t0\nflags:\t02000002\nmnt_id:\t15\nino:\t1057\n\
fanotify flags:98 event-flags:8000\n\
fanotify sdev:700003 mflags:0 mask:100000 ignored_mask:0\n\
fanotify mnt_id:52 mflags:2 mask:0 ignored_mask:100000\n\
fanotify ino:83 sdev:700003 mflags:4 mask:2 ignored_mask:100000 fhandle-bytes:c fhandle-type:81 f_handle:8300000000000000c4a1e2f0\n";

    #[test]
    fn parses_every_mark_kind() {
        let g = parse(SAMPLE);
        assert_eq!(g.flags, Some(0x98));
        assert_eq!(g.event_flags, Some(0x8000));
        assert_eq!(g.sb_marks, vec![(0x70_0003, 0x10_0000, 0)]);
        assert_eq!(g.mnt_marks, vec![(0x52, 0, 0x10_0000)]);
        assert_eq!(g.inode_marks, vec![(0x83, 0x70_0003, 2, 0x10_0000)]);
    }

    #[test]
    fn kernel_dev_encoding() {
        assert_eq!(kernel_dev(7, 3), 0x70_0003);
        assert_eq!(kernel_dev(253, 0), 0xfd0_0000);
    }

    #[test]
    fn mount_id_of_a_file() {
        assert_eq!(mnt_id(SAMPLE), Some(15));
        assert_eq!(mnt_id("pos:\t0\n"), None);
    }
}
