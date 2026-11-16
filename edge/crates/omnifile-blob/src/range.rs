//! The backend-neutral range reader the edge's cache and fills use
//! (M5 interface "data path → namespace", `docs/milestones/M5.md`).
//!
//! A [`ReadTarget`] names where one content version's bytes are: an Azure
//! blob behind a SAS, an S3 object version (signed by the edge, or, as the
//! operator's fallback, a presigned URL), or a backend this edge does not
//! know. [`RangeReader::read_range`] returns exactly the bytes asked for, or
//! an error; it never returns a short buffer, and an unknown backend is a
//! clean [`BlobError::Unsupported`] ("upgrade the edge", decision 35 F4).

use std::future::Future;

use bytes::{Bytes, BytesMut};

use crate::client::{BlobClient, MAX_RANGE_CRC};
use crate::error::{BlobError, Result};
use crate::s3::{S3Client, S3Object};

/// Where one content version's bytes are.
#[derive(Clone, PartialEq, Eq)]
pub enum ReadTarget {
    /// An Azure blob (or blob version) URL with a read SAS.
    AzureSas { url: String },
    /// An S3 object version, read with SigV4.
    S3(S3Object),
    /// An S3 presigned GetObject URL for one object version: the fallback
    /// when the operator sets `edge_credentials = presigned` (control API
    /// §3). Its ranges are unverified, like [`ReadTarget::S3`]'s.
    S3Presigned { url: String },
    /// A backend or credential mode this edge doesn't know. Every read fails
    /// with [`BlobError::Unsupported`].
    Unsupported { mode: String },
}

impl std::fmt::Debug for ReadTarget {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            // The SAS is a secret: never in a log line.
            ReadTarget::AzureSas { url } => f
                .debug_struct("AzureSas")
                .field(
                    "url",
                    &crate::client::strip_query(url).unwrap_or_else(|_| "<bad url>".into()),
                )
                .finish(),
            ReadTarget::S3(o) => f.debug_tuple("S3").field(o).finish(),
            // The signature is a secret too.
            ReadTarget::S3Presigned { url } => match crate::client::PresignedRead::decode(url) {
                Ok(r) => f.debug_tuple("S3Presigned").field(&r).finish(),
                Err(_) => f.write_str("S3Presigned(<bad>)"),
            },
            ReadTarget::Unsupported { mode } => {
                f.debug_struct("Unsupported").field("mode", mode).finish()
            }
        }
    }
}

impl ReadTarget {
    /// True when every range read is checked against the backend's own
    /// checksum of that range (Azure: `x-ms-range-get-content-crc64`). S3
    /// ranged GETs carry none; their bytes are checked only as a whole file.
    pub fn verifies_ranges(&self) -> bool {
        matches!(self, ReadTarget::AzureSas { .. })
    }

    pub fn is_unsupported(&self) -> bool {
        matches!(self, ReadTarget::Unsupported { .. })
    }

    /// The bytes can't change between two range reads: an Azure SAS (the
    /// blob or version it names, CRC-checked per range), an S3 object
    /// pinned by version or ETag, a presigned URL that names a version or
    /// carries a signed `If-Match`. Chunk reads need it; whole-file fills
    /// are verified by the whole-file CRC instead.
    pub fn is_pinned(&self) -> bool {
        match self {
            ReadTarget::AzureSas { .. } => true,
            ReadTarget::S3(o) => o.is_pinned(),
            ReadTarget::S3Presigned { url } => {
                crate::client::PresignedRead::decode(url).is_ok_and(|r| r.is_pinned())
            }
            ReadTarget::Unsupported { .. } => false,
        }
    }
}

/// Reads byte ranges of a content version. Implemented by [`EdgeReader`];
/// tests and the daemon's adapters may provide their own.
pub trait RangeReader: Send + Sync {
    /// Exactly `len` bytes at `offset`, or an error.
    fn read_range(
        &self,
        target: &ReadTarget,
        offset: u64,
        len: u64,
    ) -> impl Future<Output = Result<Bytes>> + Send;
}

/// The production reader: Azure through [`BlobClient`], S3 through
/// [`S3Client`].
#[derive(Clone)]
pub struct EdgeReader {
    pub blob: BlobClient,
    pub s3: S3Client,
}

impl EdgeReader {
    pub fn new(blob: BlobClient, s3: S3Client) -> Self {
        Self { blob, s3 }
    }
}

impl RangeReader for EdgeReader {
    async fn read_range(&self, target: &ReadTarget, offset: u64, len: u64) -> Result<Bytes> {
        let out = match target {
            ReadTarget::AzureSas { url } => {
                // Azure checks a range's CRC only up to 4 MiB: split, so every
                // byte read from Azure is verified on arrival.
                if len <= MAX_RANGE_CRC {
                    self.blob.get_range(url, offset, len).await?
                } else {
                    let mut buf = BytesMut::with_capacity(len as usize);
                    let mut at = offset;
                    let end = offset
                        .checked_add(len)
                        .ok_or_else(|| BlobError::Invalid("range overflows".into()))?;
                    while at < end {
                        let n = MAX_RANGE_CRC.min(end - at);
                        buf.extend_from_slice(&self.blob.get_range(url, at, n).await?);
                        at += n;
                    }
                    buf.freeze()
                }
            }
            ReadTarget::S3(o) => self.s3.get_range(o, offset, len).await?,
            ReadTarget::S3Presigned { url } => self.blob.get_s3_range(url, offset, len).await?,
            ReadTarget::Unsupported { mode } => {
                return Err(BlobError::Unsupported(format!("backend mode {mode:?}")));
            }
        };
        if out.len() as u64 != len {
            return Err(BlobError::BadResponse {
                op: "read_range",
                detail: format!("asked {len} bytes at {offset}, got {}", out.len()),
            });
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_never_prints_a_sas() {
        let t = ReadTarget::AzureSas {
            url: "https://a.blob.core.windows.net/c/k?sv=1&sig=SECRET".into(),
        };
        assert!(!format!("{t:?}").contains("SECRET"));
        assert!(t.verifies_ranges());
        let t = ReadTarget::S3Presigned {
            url: "https://b.vpce-1.s3.eu-west-1.vpce.amazonaws.com/k?X-Amz-Signature=SECRET".into(),
        };
        assert!(!format!("{t:?}").contains("SECRET"));
        assert!(!t.verifies_ranges());
    }

    #[tokio::test]
    async fn unsupported_is_a_clean_error() {
        let r = EdgeReader::new(
            BlobClient::new(Default::default()).unwrap(),
            S3Client::new(Default::default()).unwrap(),
        );
        let e = r
            .read_range(
                &ReadTarget::Unsupported {
                    mode: "ontap_s3".into(),
                },
                0,
                10,
            )
            .await
            .unwrap_err();
        assert!(e.is_unsupported());
        assert!(e.to_string().contains("upgrade the edge"), "{e}");
    }
}
