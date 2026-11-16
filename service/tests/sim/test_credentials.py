"""Fake edge credentials bound to one object, a permission and an expiry."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from omnifile_service.backends.base import CredentialMode, EdgeAccess, ObjectRef
from omnifile_service.backends.sim import CredentialRejected, SimBackend, VirtualClock

from .helpers import put, run


def test_azure_sas_is_scoped_to_one_blob(azure: SimBackend, clock: VirtualClock) -> None:
    ref = put(azure, "data", "dir/f.segy", b"x")
    cred = run(azure.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=15)))
    assert cred.mode is CredentialMode.AZURE_USER_DELEGATION_SAS
    assert cred.expires_at == clock.now() + timedelta(minutes=15)
    assert cred.url is not None and cred.url.startswith("https://live.blob.core.sim.invalid/")
    assert "sp=r&" in cred.url
    azure.validate_credential(cred, ref, EdgeAccess.READ)
    with pytest.raises(CredentialRejected, match="permission"):
        azure.validate_credential(cred, ref, EdgeAccess.WRITE)
    with pytest.raises(CredentialRejected):
        azure.validate_credential(cred, replace(ref, key="dir/other"), EdgeAccess.READ)
    with pytest.raises(CredentialRejected):
        azure.validate_credential(cred, replace(ref, container="archive/data"), EdgeAccess.READ)
    with pytest.raises(CredentialRejected, match="signature"):
        azure.validate_credential(cred.url.replace("sp=r", "sp=rw"), ref, EdgeAccess.READ)
    clock.advance(timedelta(minutes=15))
    with pytest.raises(CredentialRejected, match="expired"):
        azure.validate_credential(cred, ref, EdgeAccess.READ)


def test_write_sas_allows_create_and_write(azure: SimBackend) -> None:
    ref = ObjectRef(azure.backend_id, "data", "new.segy")
    cred = run(azure.edge_credential(ref, EdgeAccess.WRITE, timedelta(hours=1)))
    assert cred.url is not None and "sp=cw" in cred.url
    azure.validate_credential(cred, ref, EdgeAccess.WRITE)
    with pytest.raises(CredentialRejected, match="permission"):
        azure.validate_credential(cred, ref, EdgeAccess.READ)


def test_read_sas_is_bound_to_a_version(azure: SimBackend) -> None:
    v1 = put(azure, "data", "f", b"1")
    v2 = put(azure, "data", "f", b"2")
    cred = run(azure.edge_credential(v1, EdgeAccess.READ, timedelta(hours=1)))
    azure.validate_credential(cred, v1, EdgeAccess.READ)
    with pytest.raises(CredentialRejected, match="version"):
        azure.validate_credential(cred, v2, EdgeAccess.READ)


def test_s3_roles_anywhere_carries_no_secret(s3: SimBackend) -> None:
    ref = put(s3, "bucket", "k", b"x")
    cred = run(s3.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=5)))
    assert cred.mode is CredentialMode.S3_ROLES_ANYWHERE and cred.url is None
    assert cred.details["bucket"] == "bucket" and cred.details["version"] == ref.version_id
    with pytest.raises(CredentialRejected):
        s3.validate_credential(cred, ref, EdgeAccess.READ)


def test_s3_presigned_mode(clock: VirtualClock) -> None:
    sim = SimBackend.s3(clock, s3_credential_mode=CredentialMode.S3_PRESIGNED_URL)
    ref = put(sim, "bucket", "k", b"x")
    cred = run(sim.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=5)))
    assert cred.mode is CredentialMode.S3_PRESIGNED_URL
    sim.validate_credential(cred, ref, EdgeAccess.READ)
    other = SimBackend.s3(clock, s3_credential_mode=CredentialMode.S3_PRESIGNED_URL)
    with pytest.raises(CredentialRejected, match="signature"):
        other.validate_credential(cred, ref, EdgeAccess.READ)  # different signing secret
