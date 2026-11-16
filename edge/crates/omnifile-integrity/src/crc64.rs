//! CRC-64/NVME (also called CRC-64/Rocksoft): the checksum that Azure Blob
//! (`x-ms-content-crc64`) and Amazon S3 (`CRC64NVME`) both use.
//!
//! Parameters (reveng catalogue "CRC-64/NVME"):
//!
//! | width | poly (normal)        | poly (reflected)     | init   | refin/refout | xorout | check (`"123456789"`) |
//! |-------|----------------------|----------------------|--------|--------------|--------|-----------------------|
//! | 64    | `0xad93d23594c93659` | `0x9a6c9329ac4bc9b5` | all 1s | true         | all 1s | `0xae8b14860a799888`  |
//!
//! Microsoft describes it as "bit-reflected (based on the non bit-reflected
//! polynomial 0xad93d23594c93659) and inverts the CRC input and output bits"
//! (<https://learn.microsoft.com/rest/api/storageservices/structured-body-format>).
//!
//! The byte crunching is done by `crc-fast` (SIMD carry-less multiply). The
//! combine operation is implemented here, independently, so that the
//! verifier's arithmetic does not depend on the same library that produced
//! the per-block values.

use crc_fast::CrcAlgorithm;

/// Reflected CRC-64/NVME polynomial (bit-reversal of `0xad93d23594c93659`).
pub const POLY_REFLECTED: u64 = 0x9a6c_9329_ac4b_c9b5;

/// Non-reflected ("normal") form of the polynomial, as Microsoft and AWS quote it.
pub const POLY_NORMAL: u64 = 0xad93_d235_94c9_3659;

/// CRC of `b"123456789"` (the catalogue check value).
pub const CHECK: u64 = 0xae8b_1486_0a79_9888;

/// CRC of the empty message. Because init == xorout, it is zero.
pub const EMPTY: u64 = 0;

/// One-shot CRC-64/NVME of `data`.
#[inline]
pub fn crc64_nvme(data: &[u8]) -> u64 {
    crc_fast::checksum(CrcAlgorithm::Crc64Nvme, data)
}

/// Streaming CRC-64/NVME.
#[derive(Clone, Debug)]
pub struct Crc64 {
    inner: crc_fast::Digest,
    len: u64,
}

impl Default for Crc64 {
    fn default() -> Self {
        Self::new()
    }
}

impl Crc64 {
    pub fn new() -> Self {
        Self {
            inner: crc_fast::Digest::new(CrcAlgorithm::Crc64Nvme),
            len: 0,
        }
    }

    #[inline]
    pub fn update(&mut self, data: &[u8]) {
        self.inner.update(data);
        self.len += data.len() as u64;
    }

    /// Bytes fed so far.
    pub fn len(&self) -> u64 {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// The CRC of everything fed so far. Does not consume the state.
    pub fn finalize(&self) -> u64 {
        self.inner.finalize()
    }
}

// ---------------------------------------------------------------------------
// Combine
// ---------------------------------------------------------------------------
//
// For a CRC with init == xorout (true here), the final CRC values obey
//
//     crc(A || B) = (crc(A) · x^(8·|B|) mod P) ⊕ crc(B)
//
// Proof sketch: the raw register update is linear, R(s, M) = Lₘ(s) ⊕ R(0, M),
// where Lₘ multiplies by x^(8·|M|) mod P. With crc(M) = X ⊕ R(I, M):
//   crc(AB) = X ⊕ L_B(R(I,A)) ⊕ R(0,B) = L_B(crc(A)) ⊕ crc(B) ⊕ L_B(X ⊕ I),
// and the last term vanishes when X == I.
//
// The multiplication is done in the reflected domain (bit 63 is x^0), as in
// zlib's `crc32_combine` (multmodp / x2nmodp), widened to 64 bits.

const X0: u64 = 1 << 63; // the polynomial "1" in reflected form

/// a·b mod P, both reflected.
const fn multmodp(a: u64, mut b: u64) -> u64 {
    let mut m = X0;
    let mut p = 0u64;
    loop {
        if a & m != 0 {
            p ^= b;
            if a & (m - 1) == 0 {
                break;
            }
        }
        m >>= 1;
        b = if b & 1 != 0 {
            (b >> 1) ^ POLY_REFLECTED
        } else {
            b >> 1
        };
        if m == 0 {
            break;
        }
    }
    p
}

/// X2N[k] = x^(2^k) mod P.
const X2N: [u64; 64] = {
    let mut t = [0u64; 64];
    let mut p = X0 >> 1; // x^1
    t[0] = p;
    let mut k = 1;
    while k < 64 {
        p = multmodp(p, p);
        t[k] = p;
        k += 1;
    }
    t
};

/// x^(n·2^k) mod P.
const fn x2nmodp(mut n: u64, mut k: usize) -> u64 {
    let mut p = X0;
    while n != 0 {
        if n & 1 != 0 {
            p = multmodp(X2N[k & 63], p);
        }
        n >>= 1;
        k += 1;
    }
    p
}

/// The operator that appends `len` bytes: x^(8·len) mod P. Precompute it with
/// this when many blocks share one length, then use [`combine_op`].
pub const fn combine_operator(len: u64) -> u64 {
    x2nmodp(len, 3)
}

/// Combine with a precomputed operator from [`combine_operator`].
#[inline]
pub const fn combine_op(crc_a: u64, crc_b: u64, op: u64) -> u64 {
    multmodp(op, crc_a) ^ crc_b
}

/// CRC of `A || B` from `crc(A)`, `crc(B)` and `|B|`, without the data.
#[inline]
pub const fn combine(crc_a: u64, crc_b: u64, len_b: u64) -> u64 {
    combine_op(crc_a, crc_b, combine_operator(len_b))
}

/// Whole-object CRC from consecutive `(crc, len)` pieces (for example Azure
/// blocks, in block-list order). Empty input yields the CRC of the empty
/// object (0).
pub fn combine_all<I>(pieces: I) -> u64
where
    I: IntoIterator<Item = (u64, u64)>,
{
    let mut acc = Crc64Combiner::new();
    for (crc, len) in pieces {
        acc.push(crc, len);
    }
    acc.crc()
}

/// Incremental combiner: push `(crc, len)` pieces in order.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Crc64Combiner {
    crc: u64,
    len: u64,
    // Cache the operator for the most recent length: block plans use one
    // length for every block but the last.
    last_len: u64,
    last_op: u64,
}

impl Crc64Combiner {
    pub const fn new() -> Self {
        Self {
            crc: EMPTY,
            len: 0,
            last_len: 0,
            last_op: X0,
        }
    }

    pub fn push(&mut self, crc: u64, len: u64) {
        if len != self.last_len {
            self.last_len = len;
            self.last_op = combine_operator(len);
        }
        self.crc = combine_op(self.crc, crc, self.last_op);
        self.len += len;
    }

    /// Combined CRC of everything pushed so far.
    pub const fn crc(&self) -> u64 {
        self.crc
    }

    /// Total length of everything pushed so far.
    pub const fn len(&self) -> u64 {
        self.len
    }

    pub const fn is_empty(&self) -> bool {
        self.len == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_agree() {
        assert_eq!(POLY_NORMAL.reverse_bits(), POLY_REFLECTED);
        assert_eq!(crc64_nvme(b"123456789"), CHECK);
        assert_eq!(crc64_nvme(b""), EMPTY);
    }

    #[test]
    fn x2n_table_starts_right() {
        // x^1, x^2, x^4 in reflected form are single bits.
        assert_eq!(X2N[0], 1 << 62);
        assert_eq!(X2N[1], 1 << 61);
        assert_eq!(X2N[2], 1 << 59);
        assert_eq!(combine_operator(0), X0);
    }

    #[test]
    fn combine_identity() {
        let a = crc64_nvme(b"hello");
        assert_eq!(combine(a, EMPTY, 0), a);
        assert_eq!(combine(EMPTY, a, 5), a);
    }
}
