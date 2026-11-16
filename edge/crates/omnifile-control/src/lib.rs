//! OmniFile Gateway edge: the control-API client (edge side of
//! `docs/contracts/control-api.md`, `service/openapi/control-api.yaml`).
//!
//! * [`identity`]: the enrolment bundle, key and CSR (sealed to the TPM by
//!   [`seal`] from M6), [`identity::enrol`] with a pinned service CA, and the stored
//!   identity that [`ControlClient`] uses for mutual TLS.
//! * [`ControlClient`]: one-attempt calls for every edge endpoint used in M1,
//!   plus certificate renewal and a SAS cache honouring `cache_until`.
//! * [`roles_anywhere`]: the IAM Roles Anywhere certificate from OmniFile's
//!   edge CA, requested and renewed over mTLS (decision 50).
//! * [`feed::FeedPoller`]: the opaque-cursor feed, with backoff.
//! * [`outbox::UploadOutbox`]: the durable uploads/commit outbox, driving
//!   [`omnifile_blob`] uploads.
//! * [`reads::ReadsBatcher`]: `/reads`, one entry per file per hour.
//!
//! Nothing here blocks the FSAL path: every call is async with a timeout,
//! and [`ControlError::is_unavailable`] tells the daemon to answer from local
//! state.

pub mod annex;
pub mod client;
pub mod creds;
pub mod error;
pub mod feed;
pub mod identity;
pub mod outbox;
pub mod reads;
pub mod roles_anywhere;
pub mod seal;
pub mod types;
pub mod util;

pub use client::{ControlClient, ControlConfig, StubPrepare, Timeouts};
pub use error::{ControlError, Result};
pub use feed::FeedPoller;
pub use identity::{EnrolmentBundle, Identity, enrol};
pub use outbox::{NewUpload, UploadJob, UploadOutbox, UploadSink, UploadState};
pub use reads::ReadsBatcher;
pub use roles_anywhere::RolesAnywhereFiles;
pub use util::Backoff;
