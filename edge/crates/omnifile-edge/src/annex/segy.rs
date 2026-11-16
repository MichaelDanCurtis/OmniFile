//! The SEG-Y structure check (`annex.md` §2.2): a `.segy` / `.sgy` file is
//! copied only when its size is the headers plus a whole number of traces,
//! so a survey still being written is never copied half-way.
//!
//! SEG-Y rev 1 and 2 (SEG Technical Standards Committee): a 3,200-byte
//! textual header, a 400-byte binary header, `N` extended textual headers of
//! 3,200 bytes (binary header bytes 3505-3506; `-1` means "a variable
//! number", which this check can't count), then traces of 240 bytes plus
//! `samples` samples of the format's size. Samples per trace are at bytes
//! 3221-3222, the format code at 3225-3226, big-endian (rev 2's
//! little-endian files are told by the byte-order mark at 3297-3300).

use std::io::Read;

/// What the check says.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Segy {
    /// Not a SEG-Y name: no check.
    NotSegy,
    /// Headers plus whole traces.
    Complete,
    /// The size isn't whole traces (still being written, or truncated).
    Incomplete,
    /// The headers can't be read as SEG-Y: copied as any file, with a note.
    Unrecognised,
}

pub fn is_segy_name(path: &std::path::Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("segy") || e.eq_ignore_ascii_case("sgy"))
}

fn sample_bytes(format: u16) -> Option<u64> {
    Some(match format {
        1 | 2 | 4 | 5 | 10 => 4,
        3 | 11 => 2,
        6 | 9 | 12 => 8,
        7 | 15 => 3,
        8 | 16 => 1,
        _ => return None,
    })
}

/// Check the header of a file of `size` bytes.
pub fn check(header: &[u8], size: u64) -> Segy {
    if header.len() < 3600 || size < 3600 {
        return Segy::Incomplete;
    }
    // Rev 2: 0x01020304 read as written means big-endian; 0x04030201
    // means little-endian. Older files have zeros there (big-endian).
    let bom = &header[3296..3300];
    let le = bom == [4, 3, 2, 1];
    let u16_at = |at: usize| {
        let b = [header[at], header[at + 1]];
        if le {
            u16::from_le_bytes(b)
        } else {
            u16::from_be_bytes(b)
        }
    };
    let i16_at = |at: usize| u16_at(at) as i16;
    let samples = u64::from(u16_at(3220));
    let Some(width) = sample_bytes(u16_at(3224)) else {
        return Segy::Unrecognised;
    };
    let extended = i16_at(3504);
    if extended < 0 || samples == 0 {
        return Segy::Unrecognised;
    }
    let head = 3600 + 3200 * extended as u64;
    let trace = 240 + samples * width;
    if size < head {
        return Segy::Incomplete;
    }
    if (size - head).is_multiple_of(trace) {
        Segy::Complete
    } else {
        Segy::Incomplete
    }
}

/// Read the first 3,600 bytes of `rel` under the annex root and check it.
pub fn check_file(root: &super::nasfs::Root, rel: &str, size: u64) -> std::io::Result<Segy> {
    if !is_segy_name(std::path::Path::new(rel)) {
        return Ok(Segy::NotSegy);
    }
    let mut f = root.open_file(rel)?;
    let mut buf = vec![0u8; 3600];
    let mut got = 0;
    while got < buf.len() {
        let n = f.read(&mut buf[got..])?;
        if n == 0 {
            break;
        }
        got += n;
    }
    buf.truncate(got);
    Ok(check(&buf, size))
}

/// A SEG-Y file of `traces` traces of `samples` IEEE floats (format 5), for
/// tests and the exit test's fixtures.
pub fn synthetic(traces: u64, samples: u16, fill: u8) -> Vec<u8> {
    let mut v = vec![b' '; 3200];
    v[..3].copy_from_slice(b"C 1");
    let mut bin = vec![0u8; 400];
    bin[20..22].copy_from_slice(&samples.to_be_bytes());
    bin[24..26].copy_from_slice(&5u16.to_be_bytes());
    v.extend(bin);
    let trace = 240 + usize::from(samples) * 4;
    for i in 0..traces {
        v.extend(std::iter::repeat_n(fill.wrapping_add(i as u8), trace));
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whole_traces_are_complete_and_a_partial_trace_is_not() {
        let f = synthetic(10, 100, 7);
        assert_eq!(check(&f[..3600], f.len() as u64), Segy::Complete);
        assert_eq!(check(&f[..3600], f.len() as u64 - 17), Segy::Incomplete);
        assert_eq!(check(&f[..3600], 3600), Segy::Complete); // headers, no traces yet
        assert_eq!(check(&f[..100], 100), Segy::Incomplete);
    }

    #[test]
    fn an_unknown_format_is_unrecognised() {
        let mut f = synthetic(1, 10, 0);
        f[3224..3226].copy_from_slice(&99u16.to_be_bytes());
        assert_eq!(check(&f[..3600], f.len() as u64), Segy::Unrecognised);
    }

    #[test]
    fn only_segy_names_are_checked() {
        assert!(is_segy_name(std::path::Path::new("a/line_1042.SEGY")));
        assert!(is_segy_name(std::path::Path::new("x.sgy")));
        assert!(!is_segy_name(std::path::Path::new("x.segy.gz")));
    }
}
