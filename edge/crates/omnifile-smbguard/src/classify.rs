//! What one pre-content event needs, decided from the marker alone.
//!
//! The rules match `FSAL_OMNI`'s (`docs/contracts/fsal-socket.md` §6.1,
//! `docs/design/fsal-namespace.md` §6), with the differences that come from
//! fanotify not saying whether an access is a read or a write:
//!
//! | Marker | Event | Verdict |
//! |---|---|---|
//! | none, or not a regular file | any | allow (an ordinary file) |
//! | invalid (magic, CRC, layout, unreadable) | any | deny `EIO` (fail closed, `BAD_MARKER`) |
//! | every byte local (`RESIDENT`, or `PARTIAL` with `resident_bytes == size`) | any | allow |
//! | `PARTIAL`, `st_size < size` | any | **truncated**: overwritten with `O_TRUNC`, which raises no pre-content event; the stub state is cleared first (`linux::truncwatch`) |
//! | `PARTIAL`, `st_size > size` | any | **extended**: an `O_APPEND` write landed (its event carries `f_pos`, inside the prefix, not the end of file); fail closed and ask the broker to repair |
//! | `PARTIAL` | range inside `[0, resident_bytes)` | allow |
//! | `PARTIAL` | range past `resident_bytes` | needs `[off, min(off+len, size))` |
//! | `PARTIAL` | range starting at or past EOF | needs the whole file: it can only be a write that extends the file, and the FSAL fills before any write to a non-local file |
//! | `PARTIAL` | `(0,0)`: truncate to 0 **or** whole-file clone | needs the whole file (spike A10) |
//! | `PARTIAL` | `(L,0)`: truncate to `L` | needs the whole file (as the FSAL's `setattr2` size change) |
//!
//! A directory raises an event only when it carries the guard's
//! open-permission mark: the daemon puts one on each rewind directory it
//! creates unfilled (`DIR_UNPOPULATED`). Opening one needs it filled
//! ([`Verdict::Populate`]); smbd would otherwise list it empty. A filled
//! directory, or one with no marker, is allowed; a bad marker fails closed.
//!
//! A need larger than [`Limits::max_wait_bytes`] is flagged `huge`: an
//! `mmap()`, clone or `copy_file_range` of a 200 GB stub is one event, and
//! the caller applies the size-cap policy instead of a silent 200 GB wait.
//!
//! M5: a `PARTIAL` marker with `CHUNK_MAP` is read with its
//! `trusted.omnifile.map` ([`classify_mapped`]): a range is local when the
//! prefix or the set chunks cover it, as `FSAL_OMNI` decides with `CHUNK_MAP`
//! negotiated (`fsal-socket.md` §6.1). A missing or invalid map is a bad
//! marker (reason 3): fail closed. Whole-file accesses still need every byte.
//! [`classify`] (no map) counts the prefix alone: stricter, never looser.

use omnifile_marker::{ChunkMap, Flags, Marker};

use crate::uapi::Access;

/// The marker as read through the event's descriptor.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MarkerView {
    Unmanaged,
    /// Present but invalid; the `BAD_MARKER` reason (`fsal-socket.md` §8).
    Bad(u32),
    Valid(Marker),
}

impl MarkerView {
    #[cfg(target_os = "linux")]
    pub fn from_read(r: std::io::Result<omnifile_marker::xattr::ReadOutcome>) -> MarkerView {
        use omnifile_marker::xattr::ReadOutcome;
        match r {
            Ok(ReadOutcome::Unmanaged) => MarkerView::Unmanaged,
            Ok(ReadOutcome::Marker(m)) => MarkerView::Valid(m),
            Ok(ReadOutcome::Bad(e)) => MarkerView::Bad(e.bad_marker_reason()),
            // Unreadable (other than "no such attribute"): reason 5.
            Err(_) => MarkerView::Bad(5),
        }
    }
}

/// 10 GiB.
pub const DEFAULT_MAX_WAIT_BYTES: u64 = 10 << 30;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Needs larger than this (bytes still to fetch) are `huge`.
    pub max_wait_bytes: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            // Microsoft promises archive recall times only below 10 GB
            // (AGENTS.md): never make a reader wait silently for more.
            max_wait_bytes: DEFAULT_MAX_WAIT_BYTES,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// Every byte the access can touch is local. `ignorable`: the whole
    /// file is local (or unmanaged), so an ignore mark may be added.
    Allow { ignorable: bool },
    /// Fail closed with `EIO`; the `BAD_MARKER` reason.
    BadMarker(u32),
    /// A `PARTIAL` marker larger than the inode: the file was truncated by
    /// an `O_TRUNC` open (no pre-content event).
    Truncated(Marker),
    /// A `PARTIAL` marker smaller than the inode: an `O_APPEND` write
    /// landed past the non-local bytes.
    Extended(Marker),
    /// An unfilled rewind directory is being opened: it must be filled
    /// (the daemon's `POPULATE_DIR`) before it may be listed.
    Populate(Marker),
    /// Bytes `[start, end)` must be local before the access may proceed.
    Need {
        marker: Marker,
        start: u64,
        end: u64,
        whole_file: bool,
        /// Bytes still to fetch exceed [`Limits::max_wait_bytes`].
        huge: bool,
    },
}

/// Decide one event. `regular`: the inode is a regular file. The prefix
/// alone counts (no chunk map): stricter, never looser.
pub fn classify(
    regular: bool,
    st_size: u64,
    marker: &MarkerView,
    access: Access,
    limits: Limits,
) -> Verdict {
    classify_mapped(regular, st_size, marker, None, access, limits)
}

/// [`classify`] with the marker's chunk map (M5), when it has one.
pub fn classify_mapped(
    regular: bool,
    st_size: u64,
    marker: &MarkerView,
    map: Option<&ChunkMap>,
    access: Access,
    limits: Limits,
) -> Verdict {
    if access == Access::DirOpen {
        return match marker {
            MarkerView::Valid(m) if m.flags.contains(Flags::DIR_UNPOPULATED) => {
                Verdict::Populate(*m)
            }
            MarkerView::Bad(reason) => Verdict::BadMarker(*reason),
            _ => Verdict::Allow { ignorable: false },
        };
    }
    if !regular {
        return Verdict::Allow { ignorable: false };
    }
    let m = match marker {
        MarkerView::Unmanaged => return Verdict::Allow { ignorable: true },
        MarkerView::Bad(reason) => return Verdict::BadMarker(*reason),
        MarkerView::Valid(m) => *m,
    };
    if m.is_fully_resident() {
        return Verdict::Allow { ignorable: true };
    }
    if st_size < m.size {
        return Verdict::Truncated(m);
    }
    if st_size > m.size {
        return Verdict::Extended(m);
    }
    let whole = |m: Marker| need(m, 0, m.size, true, limits);
    match access {
        Access::Range { off, .. } if off >= m.size => whole(m),
        Access::Range { off, len } => {
            if m.covers_with_map(map, off, len) {
                Verdict::Allow { ignorable: false }
            } else {
                let end = off.saturating_add(len).min(m.size);
                need(m, off, end, false, limits)
            }
        }
        // DirOpen was decided above; here it can only be a file.
        Access::WholeFile | Access::Truncate { .. } | Access::Unknown | Access::DirOpen => whole(m),
    }
}

fn need(m: Marker, start: u64, end: u64, whole_file: bool, limits: Limits) -> Verdict {
    let missing = end.saturating_sub(start.max(m.resident_bytes));
    Verdict::Need {
        marker: m,
        start,
        end,
        whole_file,
        huge: missing > limits.max_wait_bytes,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use omnifile_marker::{Flags, State};

    const MIB: u64 = 1 << 20;

    fn partial(size: u64, resident: u64) -> Marker {
        Marker::partial(1, 2, size, resident, 3, Flags::ARCHIVED)
    }

    fn c(m: &MarkerView, st: u64, a: Access) -> Verdict {
        classify(true, st, m, a, Limits::default())
    }

    #[test]
    fn map_golden_vector_classifies() {
        // fsal-socket.md §8: 10 MiB at 4 MiB chunks, chunks 0 and 2 local
        let mut m = Marker::partial(
            0x0102_0304_0506_0708,
            42,
            10 * MIB,
            0,
            3,
            Flags::ONLINE.with(Flags::CHUNK_MAP),
        );
        m.marker_seq = 8;
        m.chunk_shift = 22;
        let map = ChunkMap::decode(&[0x05], 10 * MIB, 22).unwrap();
        let v = MarkerView::Valid(m);
        let cm = |off, len| {
            classify_mapped(
                true,
                10 * MIB,
                &v,
                Some(&map),
                Access::Range { off, len },
                Limits::default(),
            )
        };
        assert_eq!(cm(0, 4 * MIB), Verdict::Allow { ignorable: false });
        assert_eq!(cm(8 * MIB, 2 * MIB), Verdict::Allow { ignorable: false });
        assert!(matches!(cm(4 * MIB, MIB), Verdict::Need { start, .. } if start == 4 * MIB));
        assert!(matches!(cm(3 * MIB, 2 * MIB), Verdict::Need { .. }));
        // without the map: the prefix (0) alone, so even chunk 0 is needed
        assert!(matches!(
            c(&v, 10 * MIB, Access::Range { off: 0, len: MIB }),
            Verdict::Need { .. }
        ));
        // a whole-file access needs every byte whatever the map says
        assert!(matches!(
            classify_mapped(
                true,
                10 * MIB,
                &v,
                Some(&map),
                Access::WholeFile,
                Limits::default()
            ),
            Verdict::Need {
                whole_file: true,
                ..
            }
        ));
    }

    #[test]
    fn unmanaged_and_non_regular_are_allowed() {
        let a = Access::Range { off: 0, len: 4096 };
        assert_eq!(
            c(&MarkerView::Unmanaged, 10, a),
            Verdict::Allow { ignorable: true }
        );
        assert_eq!(
            classify(false, 0, &MarkerView::Bad(1), a, Limits::default()),
            Verdict::Allow { ignorable: false }
        );
    }

    #[test]
    fn bad_marker_fails_closed() {
        let v = c(&MarkerView::Bad(2), 10, Access::Range { off: 0, len: 1 });
        assert_eq!(v, Verdict::BadMarker(2));
    }

    #[test]
    fn resident_prefix_is_allowed_without_ignore() {
        let m = MarkerView::Valid(partial(64 * MIB, MIB));
        assert_eq!(
            c(&m, 64 * MIB, Access::Range { off: 0, len: MIB }),
            Verdict::Allow { ignorable: false }
        );
        assert_eq!(
            c(
                &m,
                64 * MIB,
                Access::Range {
                    off: 4096,
                    len: 3600
                }
            ),
            Verdict::Allow { ignorable: false }
        );
    }

    #[test]
    fn past_the_prefix_needs_the_range() {
        let m = partial(64 * MIB, MIB);
        let v = c(
            &MarkerView::Valid(m),
            64 * MIB,
            Access::Range {
                off: 20 * MIB,
                len: 2 * MIB,
            },
        );
        assert_eq!(
            v,
            Verdict::Need {
                marker: m,
                start: 20 * MIB,
                end: 22 * MIB,
                whole_file: false,
                huge: false
            }
        );
        // A range straddling EOF is clipped to the size.
        let v = c(
            &MarkerView::Valid(m),
            64 * MIB,
            Access::Range {
                off: 63 * MIB,
                len: 4 * MIB,
            },
        );
        assert!(matches!(v, Verdict::Need { end, .. } if end == 64 * MIB));
    }

    #[test]
    fn zero_zero_and_truncates_are_whole_file() {
        let m = partial(64 * MIB, MIB);
        for a in [
            Access::WholeFile,
            Access::Truncate { to: 4096 },
            Access::Truncate { to: 128 * MIB },
            Access::Unknown,
        ] {
            let v = c(&MarkerView::Valid(m), 64 * MIB, a);
            assert_eq!(
                v,
                Verdict::Need {
                    marker: m,
                    start: 0,
                    end: 64 * MIB,
                    whole_file: true,
                    huge: false
                },
                "{a:?}"
            );
        }
    }

    #[test]
    fn access_at_or_past_eof_is_whole_file() {
        let m = partial(64 * MIB, MIB);
        let v = c(
            &MarkerView::Valid(m),
            64 * MIB,
            Access::Range {
                off: 64 * MIB,
                len: 4096,
            },
        );
        assert!(matches!(
            v,
            Verdict::Need {
                whole_file: true,
                ..
            }
        ));
    }

    #[test]
    fn fully_resident_is_ignorable_even_if_size_moved() {
        let r = MarkerView::Valid(Marker::resident(1, 2, 64 * MIB, 3));
        assert_eq!(
            c(&r, 1, Access::WholeFile),
            Verdict::Allow { ignorable: true }
        );
        let mut p = partial(64 * MIB, 64 * MIB);
        p.state = State::Partial;
        assert_eq!(
            c(&MarkerView::Valid(p), 64 * MIB, Access::WholeFile),
            Verdict::Allow { ignorable: true }
        );
    }

    #[test]
    fn size_mismatch_on_partial() {
        let m = partial(64 * MIB, MIB);
        assert_eq!(
            c(
                &MarkerView::Valid(m),
                0,
                Access::Range { off: 0, len: 4096 }
            ),
            Verdict::Truncated(m)
        );
        assert_eq!(
            c(
                &MarkerView::Valid(m),
                64 * MIB + 4,
                Access::Range { off: 0, len: 4 }
            ),
            Verdict::Extended(m)
        );
    }

    #[test]
    fn opening_an_unfilled_directory_needs_it_filled() {
        let mut m = partial(0, 0);
        m.flags = Flags::READONLY.with(Flags::DIR_UNPOPULATED);
        let v = MarkerView::Valid(m);
        assert_eq!(c(&v, 0, Access::DirOpen), Verdict::Populate(m));
        // Not regular, but a directory open still decides.
        let d = classify(false, 0, &v, Access::DirOpen, Limits::default());
        assert_eq!(d, Verdict::Populate(m));
        // Filled, unmanaged, or another kind of access: allowed.
        let mut filled = m;
        filled.flags = Flags::READONLY;
        let ok = Verdict::Allow { ignorable: false };
        assert_eq!(
            classify(
                false,
                0,
                &MarkerView::Valid(filled),
                Access::DirOpen,
                Limits::default()
            ),
            ok
        );
        assert_eq!(
            classify(
                false,
                0,
                &MarkerView::Unmanaged,
                Access::DirOpen,
                Limits::default()
            ),
            ok
        );
        // A bad marker on a directory being opened fails closed.
        assert_eq!(
            classify(
                false,
                0,
                &MarkerView::Bad(2),
                Access::DirOpen,
                Limits::default()
            ),
            Verdict::BadMarker(2)
        );
    }

    #[test]
    fn default_size_cap_is_at_most_10_gib() {
        assert!(Limits::default().max_wait_bytes <= 10 << 30);
    }

    #[test]
    fn huge_needs_are_flagged() {
        let m = partial(200 << 30, MIB);
        let v = c(&MarkerView::Valid(m), 200 << 30, Access::WholeFile);
        assert!(matches!(v, Verdict::Need { huge: true, .. }));
        // A small read deep in the same file is not huge.
        let v = c(
            &MarkerView::Valid(m),
            200 << 30,
            Access::Range {
                off: 100 << 30,
                len: MIB,
            },
        );
        assert!(matches!(v, Verdict::Need { huge: false, .. }));
    }
}
