"""policy_checks() and share_checks(): codes, fields and messages."""

from __future__ import annotations

import pytest

from omnifile_service.policy.checks import policy_checks, share_checks

from .support import SCHEDULED, SMART, TWO, WORM, codes, ctx, share


def by_code(body: dict, **kw) -> dict:
    return {c.code: c for c in policy_checks(body, ctx(**kw))}


@pytest.mark.parametrize(
    "body, code, field",
    [
        ({"mode": "fast", "archive_day": 9}, "invalid_body", "mode"),
        ({"clock": "never", "archive_day": 9}, "invalid_body", "clock"),
        ({"stub_at": "now", "archive_day": 9}, "invalid_body", "stub_at"),
        ({"warm_day": -1, "archive_day": 9}, "invalid_body", "warm_day"),
        ({"warm_day": True, "archive_day": 9}, "invalid_body", "warm_day"),
        ({"archive_day": 9, "resident_mb": "4"}, "invalid_body", "resident_mb"),
        ({"archive_day": 9, "notify": {"owners": "yes"}}, "invalid_body", "notify.owners"),
        (
            {"archive_day": 9, "notify": {"watchers_group_sid": "admins"}},
            "invalid_body",
            "notify.watchers_group_sid",
        ),
        ({"mode": "smart"}, "archive_day_required", "archive_day"),
        ({"warm_day": 30, "cold_day": 30, "archive_day": 90}, "days_not_increasing", "cold_day"),
        ({"cold_day": 200, "archive_day": 100}, "days_not_increasing", "archive_day"),
        ({"mode": "smart", "cold_day": 5, "archive_day": 9}, "smart_has_no_warm_cold", "cold_day"),
    ],
)
def test_body_errors_are_placed(body: dict, code: str, field: str) -> None:
    got = [c for c in policy_checks(body, ctx()) if c.code == code]
    assert got and got[0].severity == "error" and got[0].field == field


def test_every_model_problem_is_included() -> None:
    from omnifile_service.policy import Policy, problems

    body = {"warm_day": 1, "cold_day": 10, "archive_day": 50}
    want = [p.code for p in problems(Policy.from_body(body), TWO)]
    got = codes(policy_checks(body, ctx()))
    for code in want:
        assert got.count(code) == want.count(code)


def test_minimum_duration_is_placed_with_a_fee_per_tb() -> None:
    body = {"warm_day": 1, "cold_day": 10, "archive_day": 50}
    mins = [c for c in policy_checks(body, ctx()) if c.code == "minimum_duration"]
    assert [c.field for c in mins] == ["cold_day", "archive_day"]
    assert "Cool has a 30-day minimum" in mins[0].message
    assert all(c.fee is not None and c.fee.amount > 0 for c in mins)
    # Cool ZRS 0.019 x 1,024 GB x 21 days short, per month of 365/12 days.
    assert mins[0].fee.to_json()["amount"] == "13.43"  # type: ignore[union-attr]


def test_smart_caveats() -> None:
    got = by_code({**SMART, "clock": "last_read"})
    assert got["smart_one_way"].severity == "info"
    assert "scheduled policies only" in got["smart_clock_written"].message
    assert "ZRS" in got["archive_copy_two_accounts"].message
    assert "leaves_hot_under_smart" in by_code({**SMART, "stub_at": "leaves_hot"})


def test_smart_layout_errors() -> None:
    # A WORM share: Smart isn't offered.
    worm = by_code(SMART, caps=WORM, layout="single_account", shares=(share(worm_enabled=True),))
    assert worm["smart_on_worm"].severity == "error"
    assert "worm_tiers_in_place" in worm and "smart_one_way" not in worm
    # One account, no WORM: the layout has no Smart tier.
    assert by_code(SMART, caps=WORM, layout="single_account")["smart_unavailable"].field == "mode"
    # A layout that offers Smart but keeps one account (the catalog's OF004).
    single = by_code(SMART, layout="single_account")
    assert single["smart_archive_needs_two_accounts"].severity == "error"


def test_scheduled_on_two_accounts_explains_the_copy() -> None:
    got = by_code(SCHEDULED)
    assert "archive_copy_two_accounts" in got and "smart_one_way" not in got
    assert "archive_copy_two_accounts" not in by_code(SCHEDULED, caps=WORM, layout="single_account")


def test_notify() -> None:
    quiet = {"requester": False, "owners": False, "watchers_group_sid": None}
    got = by_code({**SCHEDULED, "notify": {**quiet, "email_fallback": False}})
    assert got["nobody_told"].severity == "warning" and got["nobody_told"].field == "notify"
    assert got["no_email_fallback"].field == "notify.email_fallback"
    watchers = {**quiet, "watchers_group_sid": "S-1-5-21-1-2-3-1105"}
    assert "nobody_told" not in by_code({**SCHEDULED, "notify": watchers})
    assert "nobody_told" not in by_code(SCHEDULED)  # the requester is told by default


def test_open_policy_against_the_backend() -> None:
    wait = by_code(SCHEDULED, shares=(share(on_open="wait"),))["wait_without_promise"]
    assert "10 GB" in wait.message and wait.share_id == 7
    low = by_code(SCHEDULED, shares=(share(on_open="auto", wait_threshold_minutes=30),))
    assert "1 hour" in low["auto_threshold_below_eta"].message
    unset = by_code(SCHEDULED, shares=(share(on_open="auto"),))
    assert "no threshold" in unset["auto_threshold_below_eta"].message
    ok = by_code(SCHEDULED, shares=(share(on_open="auto", wait_threshold_minutes=60),))
    assert "auto_threshold_below_eta" not in ok
    assert "wait_without_promise" not in by_code(SCHEDULED, shares=(share(on_open="fail"),))
    # Files that never become stubs never wait for a recall.
    never = by_code({**SCHEDULED, "stub_at": "never"}, shares=(share(on_open="wait"),))
    assert "wait_without_promise" not in never


def test_kerberos_note_numbers() -> None:
    (c,) = share_checks(share(nfs_sec=("krb5", "krb5p")), restricted_groups=False)
    assert c.code == "kerberos_throughput" and c.severity == "info"
    assert "−53%" in c.message and "−77%" in c.message and c.field == "nfs_sec"
    assert share_checks(share(protocols=("smb",)), restricted_groups=False) == []


def test_host_trusted() -> None:
    s = share(nfs_sec=("sys",), trust_level="host_trusted")
    assert codes(share_checks(s, restricted_groups=False)) == ["host_trusted"]
    got = share_checks(s, restricted_groups=True)
    assert codes(got) == ["host_trusted", "host_trusted_restricted"]
    assert got[1].severity == "error" and got[1].share_id == 7
    # Policy previews show the share notes too (without the restricted flag).
    assert "host_trusted" in by_code(SCHEDULED, shares=(s,))


def test_check_json_shape() -> None:
    from .support import schema_errors

    for c in policy_checks({"warm_day": 1, "archive_day": 9}, ctx(shares=(share(),))):
        assert schema_errors("Check", c.to_json()) == []
