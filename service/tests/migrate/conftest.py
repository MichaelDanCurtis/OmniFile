"""Migration engine tests: a real Postgres (the retention tests' fixtures),
the S3 backend over the S3 fake as the source, and a sim Azure home."""

import pytest
from retention.conftest import connect, db, retention_db_url  # noqa: F401

#: Modules whose `Rig` tests run twice: in place, and through the real consumer.
_BOTH_WAYS = ("test_engine", "test_held_cutover", "test_lifecycle", "test_plans")


def pytest_generate_tests(metafunc) -> None:  # type: ignore[no-untyped-def]
    if "rig_mode" in metafunc.fixturenames and metafunc.module.__name__.endswith(_BOTH_WAYS):
        metafunc.parametrize("rig_mode", ["in_place", "consumer"], indirect=True)


@pytest.fixture(autouse=True)
def rig_mode(request):  # type: ignore[no-untyped-def]
    from .test_engine import Rig

    Rig.through_consumer = getattr(request, "param", "in_place") == "consumer"
    yield
    Rig.through_consumer = False
