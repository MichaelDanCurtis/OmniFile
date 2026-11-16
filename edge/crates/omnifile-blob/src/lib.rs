//! OmniFile Gateway edge: Azure Blob data-plane client.
//!
//! The edge holds no Azure secrets: every request is authorised by a
//! user-delegation SAS the service minted for one blob (control API §3).
//!
//! * [`BlobClient`]: Put Block (with `x-ms-content-crc64`), Put Block List
//!   (no explicit tier unless asked; POSIX metadata), ranged Get Blob with
//!   retries, Get Blob Properties, and [`BlobClient::upload_file`], a
//!   parallel, resumable upload driven by an [`UploadJournal`].
//! * [`s3`] (M5): the S3 reader, ranged GETs of one object version over an
//!   interface endpoint, signed with SigV4 from the local Roles Anywhere
//!   helper's temporary credentials. Presigned URLs remain as a fallback
//!   ([`BlobClient::get_s3_range`]).
//! * [`range`] (M5): [`ReadTarget`] and the backend-neutral [`RangeReader`].
//! * [`tls`]: the rustls configuration every edge HTTPS client shares (ring
//!   provider, TLS 1.2 allowed).

pub mod client;
pub mod error;
pub mod journal;
pub mod net;
pub mod range;
pub mod s3;
pub mod tls;

pub use client::{
    BlobClient, BlobOptions, BlobProperties, CommitInfo, CommitProps, CommittedBlock,
    DEFAULT_X_MS_VERSION, PosixMeta, PresignedRead, RetryPolicy, UploadOutcome, UploadRequest,
    strip_query,
};
pub use error::{BlobError, Result};
pub use journal::UploadJournal;
pub use range::{EdgeReader, RangeReader, ReadTarget};
pub use s3::{Addressing, AwsCredentials, CredentialSource, S3Client, S3Object, S3Options};
pub use tls::{TlsOptions, TlsVersions};
