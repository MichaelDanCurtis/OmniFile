//! The OmniFile stub marker: one 64-byte binary `trusted.omnifile` xattr per
//! managed file, plus an optional `trusted.omnifile.map` chunk bitmap.
//!
//! The layout is fixed by `docs/contracts/fsal-socket.md` §8 and must stay
//! byte-identical with the C decoder in `fsal/FSAL_OMNI`. The golden vector in
//! the tests below is also asserted on the C side.
//!
//! Rules the daemon follows when it writes markers (R1 §5):
//! - create placeholders with `O_TMPFILE` and the marker set before `linkat`;
//! - narrow the marker, and get acknowledged invalidation, before punching;
//! - widen only after the new bytes are durable (`fsync`).
//!
//! A reader that finds anything it does not understand fails closed: the
//! file is treated as having nothing resident and `BAD_MARKER` is reported.

use std::fmt;

#[cfg(target_os = "linux")]
pub mod dosattr;
#[cfg(target_os = "linux")]
pub mod xattr;

/// The marker's xattr name. `trusted.*` needs `CAP_SYS_ADMIN`: users can
/// neither read nor change it through NFSv4.2 SETXATTR (`user.*`) or SMB.
pub const XATTR_NAME: &str = "trusted.omnifile";
/// The optional chunk bitmap's xattr name (present only with [`Flags::CHUNK_MAP`]).
pub const MAP_XATTR_NAME: &str = "trusted.omnifile.map";

/// Encoded marker length.
pub const MARKER_LEN: usize = 64;
/// `O M F 1`, little-endian.
pub const MAGIC: u32 = 0x3146_4D4F;
/// The only layout this reader knows.
pub const LAYOUT: u8 = 1;
/// Smallest chunk the map may describe (4 KiB).
pub const MIN_CHUNK_SHIFT: u8 = 12;
/// Largest chunk the map may describe (1 TiB).
pub const MAX_CHUNK_SHIFT: u8 = 40;
/// The largest map an xattr value can hold (xfs: 64 KiB); a geometry that
/// needs more is invalid (`fsal-socket.md` §8).
pub const MAP_MAX: usize = 65536;
/// Default chunk size for the map, 4 MiB.
pub const DEFAULT_CHUNK_SHIFT: u8 = 22;

/// Which bytes of the file are local.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum State {
    /// Every byte is local.
    Resident = 1,
    /// `[0, resident_bytes)`, plus any chunks set in the map, are local.
    Partial = 2,
}

/// Marker flag bits. Unknown bits are kept and ignored on read: a change
/// that matters for safety bumps the layout instead.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Flags(pub u16);

impl Flags {
    /// The remote copy is readable by range GET.
    pub const ONLINE: Flags = Flags(1 << 0);
    /// The remote copy needs a restore.
    pub const ARCHIVED: Flags = Flags(1 << 1);
    /// A rewind (point-in-time) placeholder.
    pub const READONLY: Flags = Flags(1 << 2);
    /// A rewind directory whose entries are not created yet.
    pub const DIR_UNPOPULATED: Flags = Flags(1 << 3);
    /// A `trusted.omnifile.map` xattr exists.
    pub const CHUNK_MAP: Flags = Flags(1 << 4);
    /// The share is WORM (informational).
    pub const WORM: Flags = Flags(1 << 5);

    pub const fn empty() -> Flags {
        Flags(0)
    }
    pub const fn bits(self) -> u16 {
        self.0
    }
    pub const fn contains(self, other: Flags) -> bool {
        self.0 & other.0 == other.0
    }
    #[must_use]
    pub const fn with(self, other: Flags) -> Flags {
        Flags(self.0 | other.0)
    }
    #[must_use]
    pub const fn without(self, other: Flags) -> Flags {
        Flags(self.0 & !other.0)
    }
}

impl std::ops::BitOr for Flags {
    type Output = Flags;
    fn bitor(self, rhs: Flags) -> Flags {
        self.with(rhs)
    }
}

/// Why a marker was rejected. [`MarkerError::bad_marker_reason`] gives the
/// `BAD_MARKER` reason code of `fsal-socket.md` §5.4.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MarkerError {
    /// The xattr value is not exactly 64 bytes.
    BadLength(usize),
    BadMagic(u32),
    BadChecksum {
        stored: u32,
        computed: u32,
    },
    UnknownLayout(u8),
    BadState(u8),
    /// Fields that contradict each other (for example `resident_bytes > size`).
    Inconsistent(&'static str),
    /// The marker's size differs from the inode's `st_size`.
    SizeMismatch {
        marker: u64,
        inode: u64,
    },
}

impl MarkerError {
    /// `BAD_MARKER` reason: 1 bad magic; 2 bad checksum; 3 unknown layout;
    /// 4 size mismatch with the inode; (5, read error, is the caller's).
    pub fn bad_marker_reason(&self) -> u32 {
        match self {
            MarkerError::BadMagic(_) => 1,
            MarkerError::BadChecksum { .. } => 2,
            MarkerError::BadLength(_)
            | MarkerError::UnknownLayout(_)
            | MarkerError::BadState(_)
            | MarkerError::Inconsistent(_) => 3,
            MarkerError::SizeMismatch { .. } => 4,
        }
    }
}

impl fmt::Display for MarkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MarkerError::BadLength(n) => write!(f, "marker is {n} bytes, expected {MARKER_LEN}"),
            MarkerError::BadMagic(m) => write!(f, "bad marker magic {m:#010x}"),
            MarkerError::BadChecksum { stored, computed } => {
                write!(
                    f,
                    "marker crc32c {stored:#010x} != computed {computed:#010x}"
                )
            }
            MarkerError::UnknownLayout(l) => write!(f, "unknown marker layout {l}"),
            MarkerError::BadState(s) => write!(f, "invalid marker state {s}"),
            MarkerError::Inconsistent(why) => write!(f, "inconsistent marker: {why}"),
            MarkerError::SizeMismatch { marker, inode } => {
                write!(f, "marker size {marker} != inode size {inode}")
            }
        }
    }
}

impl std::error::Error for MarkerError {}

/// A decoded `trusted.omnifile` marker.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Marker {
    pub state: State,
    pub flags: Flags,
    /// Catalog `file_id`.
    pub file_id: u64,
    /// The **content** version these bytes belong to
    /// (`catalog.file.content_version_id`), never the current version.
    pub version_id: u64,
    /// Logical size when the marker was written.
    pub size: u64,
    /// Local prefix `[0, resident_bytes)`.
    pub resident_bytes: u64,
    /// Daemon-wide, increasing on every marker write.
    pub marker_seq: u64,
    /// Catalog `share_id`.
    pub share_id: u64,
    /// log2 of the map's chunk size; 0 without a map.
    pub chunk_shift: u8,
}

impl Marker {
    /// A fully resident marker for an uploaded and verified version.
    pub fn resident(file_id: u64, version_id: u64, size: u64, share_id: u64) -> Marker {
        Marker {
            state: State::Resident,
            flags: Flags::empty(),
            file_id,
            version_id,
            size,
            resident_bytes: size,
            marker_seq: 0,
            share_id,
            chunk_shift: 0,
        }
    }

    /// A partial marker with `[0, resident_bytes)` local.
    pub fn partial(
        file_id: u64,
        version_id: u64,
        size: u64,
        resident_bytes: u64,
        share_id: u64,
        flags: Flags,
    ) -> Marker {
        Marker {
            state: State::Partial,
            flags,
            file_id,
            version_id,
            size,
            resident_bytes,
            marker_seq: 0,
            share_id,
            chunk_shift: 0,
        }
    }

    /// Check the field rules every reader applies (`fsal-socket.md` §8).
    pub fn validate(&self) -> Result<(), MarkerError> {
        if self.resident_bytes > self.size {
            return Err(MarkerError::Inconsistent("resident_bytes > size"));
        }
        if self.state == State::Resident && self.resident_bytes != self.size {
            return Err(MarkerError::Inconsistent(
                "RESIDENT with resident_bytes != size",
            ));
        }
        if self.flags.contains(Flags::CHUNK_MAP) {
            if !(MIN_CHUNK_SHIFT..=MAX_CHUNK_SHIFT).contains(&self.chunk_shift) {
                return Err(MarkerError::Inconsistent("chunk_shift out of range"));
            }
        } else if self.chunk_shift != 0 {
            return Err(MarkerError::Inconsistent("chunk_shift without CHUNK_MAP"));
        }
        Ok(())
    }

    /// Encode to the 64-byte wire form. Refuses to encode an invalid marker,
    /// so the daemon can never write one.
    pub fn encode(&self) -> Result<[u8; MARKER_LEN], MarkerError> {
        self.validate()?;
        let mut b = [0u8; MARKER_LEN];
        b[0..4].copy_from_slice(&MAGIC.to_le_bytes());
        b[4] = LAYOUT;
        b[5] = self.state as u8;
        b[6..8].copy_from_slice(&self.flags.0.to_le_bytes());
        b[8..16].copy_from_slice(&self.file_id.to_le_bytes());
        b[16..24].copy_from_slice(&self.version_id.to_le_bytes());
        b[24..32].copy_from_slice(&self.size.to_le_bytes());
        b[32..40].copy_from_slice(&self.resident_bytes.to_le_bytes());
        b[40..48].copy_from_slice(&self.marker_seq.to_le_bytes());
        b[48..56].copy_from_slice(&self.share_id.to_le_bytes());
        b[56] = self.chunk_shift;
        // 57..60 reserved, zero.
        let crc = crc32c(&b[0..60]);
        b[60..64].copy_from_slice(&crc.to_le_bytes());
        Ok(b)
    }

    /// Decode and validate. Reserved bytes are ignored (but covered by the CRC).
    pub fn decode(b: &[u8]) -> Result<Marker, MarkerError> {
        if b.len() != MARKER_LEN {
            return Err(MarkerError::BadLength(b.len()));
        }
        let magic = u32::from_le_bytes(b[0..4].try_into().unwrap());
        if magic != MAGIC {
            return Err(MarkerError::BadMagic(magic));
        }
        let stored = u32::from_le_bytes(b[60..64].try_into().unwrap());
        let computed = crc32c(&b[0..60]);
        if stored != computed {
            return Err(MarkerError::BadChecksum { stored, computed });
        }
        if b[4] != LAYOUT {
            return Err(MarkerError::UnknownLayout(b[4]));
        }
        let state = match b[5] {
            1 => State::Resident,
            2 => State::Partial,
            s => return Err(MarkerError::BadState(s)),
        };
        let u64_at = |o: usize| u64::from_le_bytes(b[o..o + 8].try_into().unwrap());
        let m = Marker {
            state,
            flags: Flags(u16::from_le_bytes([b[6], b[7]])),
            file_id: u64_at(8),
            version_id: u64_at(16),
            size: u64_at(24),
            resident_bytes: u64_at(32),
            marker_seq: u64_at(40),
            share_id: u64_at(48),
            chunk_shift: b[56],
        };
        m.validate()?;
        Ok(m)
    }

    /// Decode, then check a `PARTIAL` marker against the inode's `st_size`
    /// (`BAD_MARKER` reason 4 when they differ: the marker is stale).
    ///
    /// A `RESIDENT` marker whose size differs is **not** bad (contract §8,
    /// M1 review S7): every byte was local when it was written, and bytes
    /// are removed only after narrowing to `PARTIAL`, so a reader treats the
    /// file as all local up to `st_size`, as `FSAL_OMNI` and the smb-guard
    /// do. The daemon still treats it as stale before stubbing or filling.
    pub fn decode_for_inode(b: &[u8], st_size: u64) -> Result<Marker, MarkerError> {
        let m = Marker::decode(b)?;
        if m.state == State::Partial && m.size != st_size {
            return Err(MarkerError::SizeMismatch {
                marker: m.size,
                inode: st_size,
            });
        }
        Ok(m)
    }

    /// True when `[offset, offset+len)` (clamped to the file size) is local
    /// by the prefix watermark alone. A read at or past EOF needs no bytes.
    pub fn covers(&self, offset: u64, len: u64) -> bool {
        if self.state == State::Resident {
            return true;
        }
        let end = offset.saturating_add(len).min(self.size);
        if offset >= end {
            return true;
        }
        end <= self.resident_bytes
    }

    /// As [`Marker::covers`], also consulting a chunk map when the marker has one.
    pub fn covers_with_map(&self, map: Option<&ChunkMap>, offset: u64, len: u64) -> bool {
        if self.covers(offset, len) {
            return true;
        }
        match map {
            Some(map) if self.flags.contains(Flags::CHUNK_MAP) && map.shift == self.chunk_shift => {
                let end = offset.saturating_add(len).min(self.size);
                // Bytes below the watermark are local; check the rest by chunk.
                let start = offset.max(self.resident_bytes);
                map.covers(start, end, self.size)
            }
            _ => false,
        }
    }

    /// True when every byte is local.
    pub fn is_fully_resident(&self) -> bool {
        self.state == State::Resident || self.resident_bytes == self.size
    }
}

/// `trusted.omnifile.map`: bit *i* set means chunk *i* is local and durable.
/// Least significant bit first within each byte.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChunkMap {
    pub shift: u8,
    pub bits: Vec<u8>,
}

impl ChunkMap {
    /// Number of chunks for a file of `size` bytes.
    pub fn chunk_count(size: u64, shift: u8) -> u64 {
        if size == 0 {
            0
        } else {
            ((size - 1) >> shift) + 1
        }
    }

    pub fn empty(size: u64, shift: u8) -> ChunkMap {
        let n = Self::chunk_count(size, shift);
        ChunkMap {
            shift,
            bits: vec![0u8; n.div_ceil(8) as usize],
        }
    }

    /// Decode a map for a file of `size` bytes (`fsal-socket.md` §8, M5):
    /// the length must match exactly, no bit may be set past the last
    /// chunk, and the map must fit an xattr value. Any of these is invalid,
    /// which a reader treats as a bad marker (reason 3, fail closed).
    pub fn decode(b: &[u8], size: u64, shift: u8) -> Result<ChunkMap, MarkerError> {
        if !(MIN_CHUNK_SHIFT..=MAX_CHUNK_SHIFT).contains(&shift) {
            return Err(MarkerError::Inconsistent("chunk_shift out of range"));
        }
        let chunks = Self::chunk_count(size, shift);
        let want = chunks.div_ceil(8) as usize;
        if want == 0 || want > MAP_MAX {
            return Err(MarkerError::Inconsistent("chunk map does not fit an xattr"));
        }
        if b.len() != want {
            return Err(MarkerError::Inconsistent(
                "chunk map length does not match size",
            ));
        }
        let used = (chunks % 8) as u32;
        if used != 0 && b[want - 1] >> used != 0 {
            return Err(MarkerError::Inconsistent(
                "chunk map has a bit past the last chunk",
            ));
        }
        Ok(ChunkMap {
            shift,
            bits: b.to_vec(),
        })
    }

    pub fn encode(&self) -> &[u8] {
        &self.bits
    }

    pub fn is_set(&self, chunk: u64) -> bool {
        let byte = (chunk / 8) as usize;
        byte < self.bits.len() && self.bits[byte] & (1 << (chunk % 8)) != 0
    }

    pub fn set(&mut self, chunk: u64) {
        let byte = (chunk / 8) as usize;
        if byte < self.bits.len() {
            self.bits[byte] |= 1 << (chunk % 8);
        }
    }

    pub fn clear(&mut self, chunk: u64) {
        let byte = (chunk / 8) as usize;
        if byte < self.bits.len() {
            self.bits[byte] &= !(1 << (chunk % 8));
        }
    }

    /// True when every chunk overlapping `[start, end)` is set.
    pub fn covers(&self, start: u64, end: u64, size: u64) -> bool {
        let end = end.min(size);
        if start >= end {
            return true;
        }
        let first = start >> self.shift;
        let last = (end - 1) >> self.shift;
        (first..=last).all(|c| self.is_set(c))
    }
}

/// CRC-32C (Castagnoli): reflected polynomial 0x82F63B78, init and final XOR
/// 0xFFFFFFFF. Check value for `"123456789"` is `0xE3069283`.
pub fn crc32c(data: &[u8]) -> u32 {
    const TABLE: [u32; 256] = {
        let mut t = [0u32; 256];
        let mut i = 0;
        while i < 256 {
            let mut c = i as u32;
            let mut k = 0;
            while k < 8 {
                c = if c & 1 != 0 {
                    (c >> 1) ^ 0x82F6_3B78
                } else {
                    c >> 1
                };
                k += 1;
            }
            t[i] = c;
            i += 1;
        }
        t
    };
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc = TABLE[((crc ^ byte as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    crc ^ 0xFFFF_FFFF
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// The golden vector shared with `fsal/tests` and `fsal-socket.md` §8.
    pub(crate) fn golden() -> Marker {
        Marker {
            state: State::Partial,
            flags: Flags::ONLINE,
            file_id: 0x0102_0304_0506_0708,
            version_id: 42,
            size: 1_048_576,
            resident_bytes: 65_536,
            marker_seq: 7,
            share_id: 3,
            chunk_shift: 0,
        }
    }

    pub(crate) const GOLDEN_HEX: &str = "4f4d4631010201000807060504030201\
                                         2a000000000000000000100000000000\
                                         00000100000000000700000000000000\
                                         0300000000000000000000000f6500c4";

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    #[test]
    fn crc32c_check_value() {
        assert_eq!(crc32c(b"123456789"), 0xE306_9283);
        assert_eq!(crc32c(b""), 0);
    }

    #[test]
    fn crc32c_matches_crc_fast() {
        use crc_fast::{CrcAlgorithm, checksum};
        for data in [&b""[..], b"a", b"123456789", &[0u8; 60], &[0xFFu8; 61]] {
            assert_eq!(
                crc32c(data) as u64,
                checksum(CrcAlgorithm::Crc32Iscsi, data)
            );
        }
    }

    #[test]
    fn golden_vector() {
        let enc = golden().encode().unwrap();
        assert_eq!(
            hex(&enc),
            GOLDEN_HEX,
            "golden marker changed: update fsal/tests and §8"
        );
        assert_eq!(&enc[0..4], b"OMF1");
        assert_eq!(Marker::decode(&enc).unwrap(), golden());
    }

    #[test]
    fn rejects_with_reason_codes() {
        let good = golden().encode().unwrap();
        let mut b = good;
        b[0] ^= 1;
        assert_eq!(Marker::decode(&b).unwrap_err().bad_marker_reason(), 1);
        let mut b = good;
        b[30] ^= 1;
        assert_eq!(Marker::decode(&b).unwrap_err().bad_marker_reason(), 2);
        assert_eq!(
            Marker::decode(&good[..63]).unwrap_err().bad_marker_reason(),
            3
        );
        let mut long = good.to_vec();
        long.push(0);
        assert_eq!(Marker::decode(&long).unwrap_err().bad_marker_reason(), 3);

        // Re-sign bad field values so only the field rule trips.
        let resign = |mut b: [u8; 64]| {
            let crc = crc32c(&b[..60]);
            b[60..].copy_from_slice(&crc.to_le_bytes());
            b
        };
        let mut b = good;
        b[4] = 2;
        assert_eq!(
            Marker::decode(&resign(b)).unwrap_err(),
            MarkerError::UnknownLayout(2)
        );
        for s in [0u8, 3, 255] {
            let mut b = good;
            b[5] = s;
            assert_eq!(
                Marker::decode(&resign(b)).unwrap_err(),
                MarkerError::BadState(s)
            );
        }
        let mut b = good;
        b[56] = 22; // chunk_shift without CHUNK_MAP
        assert_eq!(
            Marker::decode(&resign(b)).unwrap_err().bad_marker_reason(),
            3
        );
        let mut b = good;
        b[5] = 1; // RESIDENT but resident_bytes != size
        assert_eq!(
            Marker::decode(&resign(b)).unwrap_err().bad_marker_reason(),
            3
        );
        let err = Marker::decode_for_inode(&good, 5).unwrap_err();
        assert_eq!(err.bad_marker_reason(), 4);
    }

    #[test]
    fn a_resized_resident_marker_is_not_bad_but_a_resized_partial_one_is() {
        // Contract §8 / M1 review S7: the FSAL and the smb-guard treat a
        // RESIDENT file as all local whatever its size now.
        let r = Marker::resident(1, 2, 100, 3).encode().unwrap();
        for st_size in [0, 50, 100, 4096] {
            let m = Marker::decode_for_inode(&r, st_size).unwrap();
            assert!(m.is_fully_resident() && m.covers(0, st_size));
        }
        let mut p = Marker::resident(1, 2, 100, 3);
        p.state = State::Partial;
        p.resident_bytes = 10;
        let p = p.encode().unwrap();
        assert!(Marker::decode_for_inode(&p, 100).is_ok());
        for st_size in [0, 99, 101] {
            assert_eq!(
                Marker::decode_for_inode(&p, st_size)
                    .unwrap_err()
                    .bad_marker_reason(),
                4
            );
        }
    }

    #[test]
    fn unknown_flag_bits_and_reserved_bytes_are_ignored() {
        let mut m = golden();
        m.flags = Flags(m.flags.0 | 0x8000);
        let enc = m.encode().unwrap();
        assert_eq!(Marker::decode(&enc).unwrap().flags.0, 0x8001);
        let mut b = golden().encode().unwrap();
        b[57] = 9;
        let crc = crc32c(&b[..60]);
        b[60..].copy_from_slice(&crc.to_le_bytes());
        assert_eq!(Marker::decode(&b).unwrap(), golden());
    }

    #[test]
    fn encode_refuses_invalid() {
        let mut m = golden();
        m.resident_bytes = m.size + 1;
        assert!(m.encode().is_err());
    }

    #[test]
    fn covers_prefix_and_eof() {
        let m = golden();
        assert!(m.covers(0, 65_536));
        assert!(!m.covers(0, 65_537));
        assert!(!m.covers(65_536, 1));
        assert!(m.covers(1_048_576, 4096), "read at EOF needs no bytes");
        assert!(m.covers(10, 0));
        let r = Marker::resident(1, 2, 10, 3);
        assert!(r.covers(0, u64::MAX));
    }

    #[test]
    fn map_golden_vector() {
        // fsal-socket.md §8: 10 MiB at 4 MiB chunks, chunks 0 and 2 local.
        // Also asserted by fsal/tests/unit/test_wire.c, omni_proto.py and
        // the smb-guard's classify tests.
        let mut m = Marker::partial(
            0x0102_0304_0506_0708,
            42,
            10 << 20,
            0,
            3,
            Flags::ONLINE.with(Flags::CHUNK_MAP),
        );
        m.marker_seq = 8;
        m.chunk_shift = 22;
        assert_eq!(
            hex(&m.encode().unwrap()),
            "4f4d46310102110008070605040302012a000000000000000000a00000000000\
             0000000000000000080000000000000003000000000000001600000\
             0a2fb3ce0"
        );
        let map = ChunkMap::decode(&[0x05], 10 << 20, 22).unwrap();
        assert!(m.covers_with_map(Some(&map), 0, 4 << 20));
        assert!(m.covers_with_map(Some(&map), 8 << 20, 2 << 20));
        assert!(!m.covers_with_map(Some(&map), 4 << 20, 4 << 20));
        assert!(!m.covers_with_map(Some(&map), 3 << 20, 2 << 20));
        assert!(
            ChunkMap::decode(&[0x05, 0], 10 << 20, 22).is_err(),
            "length"
        );
        assert!(
            ChunkMap::decode(&[0x0D], 10 << 20, 22).is_err(),
            "bit past end"
        );
        assert!(
            ChunkMap::decode(&[0; 32768], 1 << 40, 22).is_ok(),
            "1 TiB fits"
        );
        assert!(
            ChunkMap::decode(&[0; 8], 1 << 40, 12).is_err(),
            "a map over 64 KiB is invalid"
        );
    }

    #[test]
    fn chunk_map_coverage() {
        let size = 10 << 20; // 10 MiB, 3 chunks of 4 MiB (last short)
        let mut m = golden();
        m.size = size;
        m.resident_bytes = 0;
        m.flags = Flags::ONLINE | Flags::CHUNK_MAP;
        m.chunk_shift = DEFAULT_CHUNK_SHIFT;
        let mut map = ChunkMap::empty(size, DEFAULT_CHUNK_SHIFT);
        assert_eq!(map.bits.len(), 1);
        map.set(2);
        assert!(m.covers_with_map(Some(&map), 9 << 20, 1 << 20));
        assert!(
            m.covers_with_map(Some(&map), 9 << 20, 100 << 20),
            "clamped to EOF"
        );
        assert!(!m.covers_with_map(Some(&map), (8 << 20) - 1, 2));
        assert!(!m.covers_with_map(None, 9 << 20, 1));
        let dec = ChunkMap::decode(map.encode(), size, DEFAULT_CHUNK_SHIFT).unwrap();
        assert_eq!(dec, map);
        assert!(ChunkMap::decode(&[0, 0], size, DEFAULT_CHUNK_SHIFT).is_err());
    }

    fn any_marker() -> impl Strategy<Value = Marker> {
        (
            any::<bool>(),
            any::<u16>(),
            any::<[u64; 4]>(),
            any::<u64>(),
            any::<u64>(),
            prop::option::of(MIN_CHUNK_SHIFT..=MAX_CHUNK_SHIFT),
        )
            .prop_map(|(resident, flags, ids, size, res, shift)| {
                let res = if size == 0 {
                    0
                } else {
                    res % (size + 1).max(1)
                };
                let mut flags = Flags(flags).without(Flags::CHUNK_MAP);
                let chunk_shift = match shift {
                    Some(s) => {
                        flags = flags.with(Flags::CHUNK_MAP);
                        s
                    }
                    None => 0,
                };
                Marker {
                    state: if resident {
                        State::Resident
                    } else {
                        State::Partial
                    },
                    flags,
                    file_id: ids[0],
                    version_id: ids[1],
                    size,
                    resident_bytes: if resident { size } else { res },
                    marker_seq: ids[2],
                    share_id: ids[3],
                    chunk_shift,
                }
            })
    }

    proptest! {
        #[test]
        fn roundtrip(m in any_marker()) {
            let enc = m.encode().unwrap();
            prop_assert_eq!(Marker::decode(&enc).unwrap(), m);
        }

        #[test]
        fn any_single_bit_flip_is_rejected_or_benign(m in any_marker(), bit in 0usize..512) {
            let mut enc = m.encode().unwrap();
            enc[bit / 8] ^= 1 << (bit % 8);
            // CRC-32C detects every single-bit error.
            prop_assert!(Marker::decode(&enc).is_err());
        }

        #[test]
        fn decode_never_panics(b in prop::collection::vec(any::<u8>(), 0..80)) {
            let _ = Marker::decode(&b);
        }
    }
}
