use std::time::Duration;

use crate::types::Problem;

#[derive(Debug, thiserror::Error)]
pub enum ControlError {
    /// The service answered with RFC 9457 problem details.
    #[error("{op}: {} {}{}", .problem.status, .problem.code, .problem.detail.as_deref().map(|d| format!(": {d}")).unwrap_or_default())]
    Problem {
        op: &'static str,
        problem: Box<Problem>,
    },
    /// The service could not be reached, timed out, or said it is unavailable.
    /// The edge keeps serving local data and retries later.
    #[error("{op}: service unavailable: {detail}")]
    Unavailable {
        op: &'static str,
        detail: String,
        retry_after: Option<Duration>,
    },
    /// The service answered something this client can't read.
    #[error("{op}: bad response: {detail}")]
    Decode { op: &'static str, detail: String },
    /// Keys, certificates, TLS configuration.
    #[error("identity: {0}")]
    Identity(String),
    #[error("I/O: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid: {0}")]
    Invalid(String),
}

impl ControlError {
    /// The service is unreachable or overloaded: answer from local state.
    pub fn is_unavailable(&self) -> bool {
        match self {
            ControlError::Unavailable { .. } => true,
            ControlError::Problem { problem, .. } => {
                matches!(problem.code.as_str(), "unavailable" | "throttled")
                    || problem.status == 503
            }
            _ => false,
        }
    }

    /// Worth retrying with the same idempotency key.
    pub fn is_retryable(&self) -> bool {
        match self {
            ControlError::Problem { problem, .. } => {
                problem.retryable
                    || matches!(
                        problem.code.as_str(),
                        "unavailable" | "throttled" | "request_in_progress"
                    )
            }
            ControlError::Unavailable { .. } => true,
            _ => false,
        }
    }

    pub fn problem(&self) -> Option<&Problem> {
        match self {
            ControlError::Problem { problem, .. } => Some(problem.as_ref()),
            _ => None,
        }
    }

    /// The problem `code`, when the service answered with one.
    pub fn code(&self) -> Option<&str> {
        self.problem().map(|p| p.code.as_str())
    }

    pub fn status(&self) -> Option<u16> {
        self.problem().map(|p| p.status)
    }

    pub fn retry_after(&self) -> Option<Duration> {
        match self {
            ControlError::Problem { problem, .. } => problem.retry_after_s.map(Duration::from_secs),
            ControlError::Unavailable { retry_after, .. } => *retry_after,
            _ => None,
        }
    }

    /// The feed cursor is older than retention: re-bootstrap.
    pub fn is_reset_required(&self) -> bool {
        self.code() == Some("cursor_expired")
    }
}

pub type Result<T, E = ControlError> = std::result::Result<T, E>;
