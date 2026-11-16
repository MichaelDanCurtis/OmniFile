use std::time::Duration;

use omnifile_integrity::PlanError;

/// Errors from the Blob data-plane client.
#[derive(Debug, thiserror::Error)]
pub enum BlobError {
    /// Azure answered with an error status. `code` is `x-ms-error-code`.
    #[error("{op}: HTTP {status} {code}: {message}", code = code.as_deref().unwrap_or("-"))]
    Http {
        op: &'static str,
        status: u16,
        code: Option<String>,
        message: String,
        request_id: Option<String>,
        retry_after: Option<Duration>,
    },
    /// Connection, TLS or timeout failure: the request may not have arrived.
    /// Built with [`BlobError::transport`], which drops the URL: a SAS
    /// signature must never reach a log line, the outbox or the service.
    #[error("{op}: transport: {source}")]
    Transport {
        op: &'static str,
        #[source]
        source: reqwest::Error,
    },
    /// A response that does not match what was asked (short range, bad header).
    #[error("{op}: bad response: {detail}")]
    BadResponse { op: &'static str, detail: String },
    /// Local data does not match what it should be (source file changed under
    /// an upload, or a downloaded range failed its CRC).
    #[error("integrity: {0}")]
    Integrity(String),
    #[error("block plan: {0}")]
    Plan(#[from] PlanError),
    #[error("upload journal {path}: {detail}")]
    Journal { path: String, detail: String },
    #[error("I/O: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid request: {0}")]
    Invalid(String),
    /// The service named a backend or credential mode this edge doesn't know
    /// (decision 35 F4). Never retried; never read as zeros.
    #[error("unsupported backend: upgrade the edge ({0})")]
    Unsupported(String),
    /// Temporary cloud credentials could not be had (the local Roles
    /// Anywhere helper is down, or answered nonsense).
    #[error("credentials: {0}")]
    Credentials(String),
    /// An S3 object with neither a version id nor an ETag: nothing pins
    /// the bytes, so they are never read (fail closed).
    #[error("unpinned object, not read: {0}")]
    Unpinned(String),
}

impl BlobError {
    /// A transport error without its URL (and so without the SAS query).
    pub fn transport(op: &'static str, source: reqwest::Error) -> BlobError {
        BlobError::Transport {
            op,
            source: source.without_url(),
        }
    }

    /// HTTP status, when Azure answered.
    pub fn status(&self) -> Option<u16> {
        match self {
            BlobError::Http { status, .. } => Some(*status),
            _ => None,
        }
    }

    /// Azure error code (`x-ms-error-code`), when Azure answered.
    pub fn code(&self) -> Option<&str> {
        match self {
            BlobError::Http { code, .. } => code.as_deref(),
            _ => None,
        }
    }

    /// Worth retrying the same request: throttling, server errors, timeouts,
    /// connection failures, and integrity failures on download (a corrupted
    /// transfer).
    pub fn is_retryable(&self) -> bool {
        match self {
            BlobError::Http { status, .. } => matches!(status, 408 | 429 | 500 | 502 | 503 | 504),
            BlobError::Transport { source, .. } => {
                source.is_timeout()
                    || source.is_connect()
                    || source.is_request()
                    || source.is_body()
            }
            BlobError::BadResponse { .. } => true,
            _ => false,
        }
    }

    /// The server's `Retry-After`, if any.
    pub fn retry_after(&self) -> Option<Duration> {
        match self {
            BlobError::Http { retry_after, .. } => *retry_after,
            _ => None,
        }
    }

    /// The blob is in the Archive tier or still rehydrating (S3: archived
    /// and not restored): reading it needs a recall, which only the service
    /// can start.
    pub fn is_archived(&self) -> bool {
        matches!(
            self.code(),
            Some("BlobArchived" | "BlobBeingRehydrated" | "InvalidObjectState")
        )
    }

    /// The SAS or signature was refused (expired, wrong permission): get a
    /// new credential. An S3 `InvalidObjectState` 403 is not an auth error.
    pub fn is_auth(&self) -> bool {
        matches!(self.status(), Some(401 | 403)) && !self.is_archived()
    }

    /// The source object is not the one the catalog recorded: S3 refused
    /// the read's `If-Match` (`412 PreconditionFailed`). Cached bytes of
    /// the file must be discarded.
    pub fn is_source_changed(&self) -> bool {
        matches!(self, BlobError::Http { status: 412, .. })
    }

    /// The backend or credential mode is unknown to this edge.
    pub fn is_unsupported(&self) -> bool {
        matches!(self, BlobError::Unsupported(_))
    }

    pub fn into_io(self) -> std::io::Error {
        match self {
            BlobError::Io(e) => e,
            other => std::io::Error::other(other),
        }
    }
}

pub type Result<T, E = BlobError> = std::result::Result<T, E>;
