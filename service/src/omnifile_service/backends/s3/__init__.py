"""The Amazon S3 backend (M5): a migration source or destination (decision 23)."""

from .backend import S3Backend
from .capabilities import capabilities_for
from .config import AwsAccount, AwsDeployment, BatchOps, ConfigError, S3Config

__all__ = [
    "AwsAccount",
    "AwsDeployment",
    "BatchOps",
    "ConfigError",
    "S3Backend",
    "S3Config",
    "capabilities_for",
]
