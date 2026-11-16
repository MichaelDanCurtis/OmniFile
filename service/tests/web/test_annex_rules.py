"""Annex rule sets and dry runs (portal-api.md 5.23; annex.md 4.2-4.5;
phase A2.1, which removes nothing from any NAS)."""

from __future__ import annotations

import pytest

from omnifile_service.annex import rules as R

from .conftest import World, _acl, seed
from .test_annex import _annex, _dataset, _filer

DS = 41


def _body(*rules: dict) -> dict:
    return {"rules": list(rules)}


COPY = {"copy_to": {"kind": "tier"}}


def _norm(body: dict) -> dict:
    return R.normalize(body, DS)


# --- the checks (annex/rules.py) ------------------------------------------------------------


def test_templates_pass_their_checks_and_say_what_they_remove() -> None:
    for name, days in (("copy", None), ("archive_after", 30), ("move_now", None)):
        body = _norm(R.template(name, DS, days))
        assert R.checks(body, quiet_minutes=10) == [], name
        assert R.mode_of(body) == name
        assert R.removes(body) is (name != "copy")


def test_the_checks_that_block_saving() -> None:
    def problems(*rules: dict, quiet: int = 10) -> set[tuple[str, str]]:
        return {
            (p["rule"], p["check"]) for p in R.checks(_norm(_body(*rules)), quiet_minutes=quiet)
        }

    # Every trigger in use ends with a default rule.
    assert ("only", "missing_default") in problems(
        {"id": "only", "trigger": "arrived", "conditions": {"extension": "segy"}, "actions": [COPY]}
    )
    # A rule an earlier one covers never matches.
    assert ("big", "unreachable") in problems(
        {"id": "segy", "trigger": "arrived", "conditions": {"extension": "segy"},
         "actions": [COPY]},
        {"id": "big", "trigger": "arrived", "conditions": {"extension": "segy", "size_gte": 9},
         "actions": [COPY]},
        {"id": "rest", "trigger": "arrived", "conditions": {}, "actions": [COPY]},
    )  # fmt: skip
    # A removal needs its copy, an age, and the quiet period behind it.
    assert ("rm", "remove_without_copy") in problems(
        {"id": "rm", "trigger": "age", "conditions": {"age_since_arrival_days_gt": 3},
         "actions": [{"remove_from_source": True}]},
        {"id": "stay", "trigger": "age", "conditions": {}, "actions": [{"exclude": True}]},
    )  # fmt: skip
    assert ("rm", "remove_without_age") in problems(
        {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [COPY]},
        {"id": "rm", "trigger": "age", "conditions": {"extension": "tmp"},
         "actions": [{"remove_from_source": True}]},
        {"id": "stay", "trigger": "age", "conditions": {}, "actions": [{"exclude": True}]},
    )  # fmt: skip
    assert ("soon", "remove_before_quiet") in problems(
        {"id": "soon", "trigger": "arrived", "conditions": {},
         "actions": [COPY, {"remove_after_days": 0.001}]},
        quiet=60,
    )  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [
        {},
        _body({"trigger": "recall_requested", "conditions": {}, "actions": [COPY]}),
        _body({"trigger": "arrived", "conditions": {"colour": "red"}, "actions": [COPY]}),
        _body({"trigger": "arrived", "conditions": {"path": "../x"}, "actions": [COPY]}),
        _body({"trigger": "arrived", "conditions": {}, "actions": [{"teleport": True}]}),
        _body({"trigger": "arrived", "conditions": {}, "actions": [{"exclude": True}, COPY]}),
        _body({"trigger": "age", "conditions": {}, "actions": [COPY]}),
        _body({"trigger": "arrived", "conditions": {},
               "actions": [{"copy_to": {"kind": "tier", "dataset_id": "999"}}]}),
    ],
)  # fmt: skip
def test_a_body_that_isnt_well_formed_is_refused(bad: dict) -> None:
    with pytest.raises(R.RulesError):
        _norm(bad)


def test_a_removing_set_is_an_archive_admins_and_any_change_to_its_scope_widens() -> None:
    copy = _norm(R.copy_template(DS))
    archive = _norm(R.archive_after_template(DS, 30))
    assert R.needs_archive_admin(archive, copy) and R.widens(archive, copy)
    # The same removing set again: still an ArchiveAdmin's (A2.2 reads who
    # approved it), but nothing widens, so no fresh dry run.
    assert R.needs_archive_admin(archive, archive)
    assert not R.widens(archive, archive)
    assert not R.needs_archive_admin(copy, archive), "narrowing to Copy"
    sooner = _norm(R.archive_after_template(DS, 20))
    assert R.widens(sooner, archive), "sooner"
    # An exclude put in front of the removal changes who it reaches: a change.
    excl = _norm(R.archive_after_template(DS, 30))
    excl["rules"].insert(2, {"id": "x", "trigger": "age", "conditions": {"extension": ["tmp"]},
                             "actions": [{"exclude": True}]})  # fmt: skip
    assert R.widens(excl, archive)
    # The review's case: the removal is unchanged, but the arrival rules now
    # copy what they excluded, so the age rule reaches more files.
    narrow = _norm(R.archive_after_template(DS, 30))
    narrow["rules"].insert(0, {"id": "scratch", "trigger": "arrived",
                               "conditions": {"path": ["scratch/*"]},
                               "actions": [{"exclude": True}]})  # fmt: skip
    wide = _norm(R.archive_after_template(DS, 30))
    assert R.removal_rules(wide) == R.removal_rules(narrow)
    assert R.widens(wide, narrow), "a wider copy scope widens the age removal"


def test_an_unreachable_rule_is_found_by_subset_and_range() -> None:
    def rules(*conds: dict) -> dict:
        rs = [{"id": f"r{i}", "trigger": "arrived", "conditions": c, "actions": [COPY]}
              for i, c in enumerate(conds)]  # fmt: skip
        rs.append({"id": "rest", "trigger": "arrived", "conditions": {},
                   "actions": [{"exclude": True}]})  # fmt: skip
        return _norm({"rules": rs})

    def unreachable(body: dict) -> list[str]:
        return [c["rule"] for c in R.checks(body, quiet_minutes=10) if c["check"] == "unreachable"]

    assert unreachable(rules({"extension": ["segy", "sgy"]}, {"extension": ["segy"]})) == ["r1"]
    assert unreachable(rules({"extension": ["segy"]}, {"extension": ["segy", "zip"]})) == []
    assert unreachable(rules({"size_gte": 100}, {"size_gte": 1000})) == ["r1"]
    assert unreachable(rules({"size_gte": 1000}, {"size_gte": 100})) == []
    assert unreachable(rules({"size_lt": 1000}, {"size_lt": 10})) == ["r1"]
    assert unreachable(rules({"modified_age_days_gt": 30}, {"modified_age_days_gt": 90})) == ["r1"]
    assert unreachable(rules({"modified_age_days_gt": 90}, {"modified_age_days_gt": 30})) == []
    assert unreachable(rules({"extension": ["segy"]}, {"extension": ["segy"], "size_gte": 5})) == [
        "r1"
    ]


@pytest.mark.parametrize(
    "glob",
    ["**/**/raw/*", "a/**/b/**/c/**/d/**/e/**/f", "/".join(["d"] * 33)],
)
def test_globs_past_the_limits_are_refused(glob: str) -> None:
    body = {"rules": [{"trigger": "arrived", "conditions": {"path": glob},
                       "actions": [{"copy_to": {"kind": "tier"}}]}]}  # fmt: skip
    with pytest.raises(R.RulesError):
        _norm(body)
    ok = {"rules": [{"trigger": "arrived", "conditions": {"path": "a/**/b/**/c/**/d/*"},
                     "actions": [{"copy_to": {"kind": "tier"}}]}]}  # fmt: skip
    _norm(ok)


@pytest.mark.parametrize(
    "actions",
    [
        [{"copy_to": {"kind": "tier"}}, {"policy": {"policy_id": "1"}}],
        [{"copy_to": {"kind": "tier"}}, {"tag": {"key": "k", "value": "v"}}],
    ],
)
def test_policy_and_tag_are_refused_until_something_applies_them(actions: list) -> None:
    with pytest.raises(R.RulesError):
        _norm({"rules": [{"trigger": "arrived", "conditions": {}, "actions": actions}]})


@pytest.mark.parametrize(
    "actions",
    [
        [{"copy_to": {"kind": "tier"}}, {"remove_from_source": True}],
        [{"copy_to": {"kind": "tier"}}, {"remove_after_days": 30}],
    ],
)
def test_a_changed_rule_never_removes(actions: list) -> None:
    with pytest.raises(R.RulesError):
        _norm({"rules": [{"trigger": "changed", "conditions": {}, "actions": actions}]})


# --- the routes -------------------------------------------------------------------------------


def _setup(web, sql) -> tuple[World, dict, str]:
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    return w, a, a["dataset_id"]


def _owner2(w: World) -> dict:
    return w.user("owner2", groups=[w.g_owners], sid_rid=3012)


def _scratch_rules(ds: str) -> dict:
    tier = {"kind": "tier", "dataset_id": ds}
    return _body(
        {"id": "scratch", "trigger": "arrived", "conditions": {"path": "**/scratch/*"},
         "actions": [{"exclude": True}]},
        {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": tier}]},
        {"id": "recopy", "trigger": "changed", "conditions": {}, "actions": [{"copy_to": tier}]},
    )  # fmt: skip


def _archive_rules(ds: str, days: int = 30) -> dict:
    tier = {"kind": "tier", "dataset_id": ds}
    return _body(
        {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": tier}]},
        {"id": "old", "trigger": "age", "conditions": {"age_since_arrival_days_gt": days},
         "actions": [{"remove_from_source": True}]},
        {"id": "stay", "trigger": "age", "conditions": {}, "actions": [{"exclude": True}]},
    )  # fmt: skip


def _approve(b, aid: str, v: int, **kw):
    got = b.get("/v1/annexes/{annex_id}", annex_id=aid)
    return b.call(
        "POST",
        "/v1/annexes/{annex_id}/rulesets/{version}/approve",
        json=kw or {},
        headers={"If-Match": got.headers["ETag"]},
        annex_id=aid,
        version=v,
    )


def test_an_owner_proposes_another_owner_approves_a_copy_only_change(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner = web.browser(w.owner)
    r = owner.post("/v1/annexes/{annex_id}/rulesets",
                   {"body": _scratch_rules(ds), "rationale": "scratch stays"},
                   annex_id=aid)  # fmt: skip
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["version"], p["state"], p["removes"], p["needs_archive_admin"]) == (
        2, "proposed", False, False)  # fmt: skip
    assert p["dry_run_id"] is not None, "every proposal carries its dry run"
    # Nothing runs until it is approved, and its author can't approve it.
    assert owner.get("/v1/annexes/{annex_id}", annex_id=aid).json()["ruleset_version"] == 1
    r = _approve(owner, aid, 2)
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    r = _approve(web.browser(_owner2(w)), aid, 2)
    assert r.status_code == 200, r.text
    assert r.json()["ruleset_version"] == 2 and r.json()["mode"] == "custom"
    assert r.json()["removals"]["dry_run_until"] is None, "copy-only: no shadow period"
    states = dict(
        sql.execute(
            "SELECT version, state FROM catalog.annex_ruleset WHERE annex_id = %s", (int(aid),)
        ).fetchall()
    )
    assert states == {1: "superseded", 2: "active"}
    listed = owner.get("/v1/annexes/{annex_id}/rulesets", annex_id=aid).json()["items"]
    assert [x["version"] for x in listed] == [2, 1]


def test_a_removal_needs_an_archive_admin_and_starts_in_dry_run(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner = web.browser(w.owner)
    r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": _archive_rules(ds)}, annex_id=aid)
    assert r.status_code == 201, r.text
    assert r.json()["needs_archive_admin"] is True
    r = _approve(web.browser(_owner2(w)), aid, 2)
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    admin = web.browser(w.admin)
    r = _approve(admin, aid, 2)
    assert r.status_code == 200, r.text
    rem = r.json()["removals"]
    assert rem["dry_run_until"] is not None and rem["enabled"] is False
    assert r.json()["mode"] == "custom"
    until = sql.execute(
        "SELECT dry_run_until - now() > interval '6 days 23 hours' FROM catalog.annex"
        " WHERE annex_id = %s",
        (int(aid),),
    ).fetchone()[0]
    assert until, "7 days of dry run (decision 5)"
    # An ArchiveAdmin's own removing proposal needs another ArchiveAdmin.
    r = admin.post("/v1/annexes/{annex_id}/rulesets", {"body": _archive_rules(ds, 20)},
                   annex_id=aid)  # fmt: skip
    assert r.status_code == 201
    r = _approve(admin, aid, 3)
    assert r.status_code == 403 and r.json()["code"] == "self_approval"


def test_a_rule_set_that_fails_its_checks_is_not_saved(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    owner = web.browser(w.owner)
    body = _scratch_rules(ds)
    body["rules"] = body["rules"][:1] + body["rules"][2:]  # no default for arrivals
    r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": body}, annex_id=a["annex_id"])
    assert r.status_code == 422 and r.json()["code"] == "rules_check_failed"
    assert {c["check"] for c in r.json()["rule_checks"]} == {"missing_default"}
    n = sql.execute("SELECT count(*) FROM catalog.annex_ruleset WHERE annex_id = %s",
                    (int(a["annex_id"]),)).fetchone()[0]  # fmt: skip
    assert n == 1


def test_a_proposal_can_be_rejected_and_then_never_approved(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner = web.browser(w.owner)
    owner.post("/v1/annexes/{annex_id}/rulesets", {"body": _scratch_rules(ds)}, annex_id=aid)
    o2 = web.browser(_owner2(w))
    got = o2.get("/v1/annexes/{annex_id}", annex_id=aid)
    r = o2.call("POST", "/v1/annexes/{annex_id}/rulesets/{version}/reject",
                json={"note": "not now"}, headers={"If-Match": got.headers["ETag"]},
                annex_id=aid, version=2)  # fmt: skip
    assert r.status_code == 200, r.text
    r = _approve(web.browser(w.admin), aid, 2)
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"


def test_creating_an_archive_annex_copies_now_and_proposes_the_removal(web, sql) -> None:
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds, mode="archive_after", archive_after_days=30)
    assert a["mode"] == "copy" and a["ruleset_version"] == 1, "copying starts at once"
    assert a["pending_proposals"] == 1 and a["removals"]["pointer_files"] is True
    rs = admin.get("/v1/annexes/{annex_id}/rulesets/{version}", annex_id=a["annex_id"],
                   version=2).json()  # fmt: skip
    assert rs["mode"] == "archive_after" and rs["needs_archive_admin"] is True
    assert rs["body"]["archive_after_days"] == 30 and rs["checks"] == []


def test_the_named_person_must_still_have_access_at_approval(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    o2 = _owner2(w)
    web.browser(o2).post("/v1/annexes/{annex_id}/rulesets", {"body": _scratch_rules(ds)},
                         annex_id=aid)  # fmt: skip
    # owner2 leaves the Owners group (and has no role) before it is approved.
    sql.execute("UPDATE catalog.identity SET group_sids = '{}', app_roles = '{}' WHERE sid = %s",
                (o2["sid"],))  # fmt: skip
    r = _approve(web.browser(w.owner), aid, 2)
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"


def test_dry_runs_are_answered_by_the_edge_and_shown_to_admins_only(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    r = admin.post("/v1/annexes/{annex_id}/dry-runs", {"body": _scratch_rules(ds)}, annex_id=aid)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["state"] == "requested" and run["checks"] == []
    report = {
        "files": 9,
        "bytes": 90,
        "rules": [
            {"id": "copy", "trigger": "arrived", "files": 7, "bytes": 30, "samples": ["a/x.segy"]},
            {"id": "scratch", "trigger": "arrived", "files": 2, "bytes": 60,
             "samples": ["a/scratch/t"]},
        ],
        "copied": {"files": 7, "bytes": 30},
        "excluded": {"files": 2, "bytes": 60},
        "removals": {"files": 0, "bytes": 0, "now": {"files": 0, "bytes": 0}, "by_day": []},
    }  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_dry_run SET state = 'done', done_at = now(), report = %s"
        " WHERE run_id = %s",
        (__import__("json").dumps(report), int(run["run_id"])),
    )
    url = "/v1/annexes/{annex_id}/dry-runs/{run_id}"
    seen = admin.get(url, annex_id=aid, run_id=run["run_id"]).json()
    assert seen["state"] == "done" and seen["report"]["rules"][1]["files"] == 2
    assert seen["report"]["rules"][0]["samples"] == ["a/x.segy"]
    # Counts can tell an Owner what is in a NAS folder they can't read.
    seen = owner.get(url, annex_id=aid, run_id=run["run_id"]).json()
    assert seen["state"] == "done" and seen["report"] is None and seen["report_hidden"] is True
    # Someone outside the annex can't ask for one.
    r = web.browser(w.reader).post("/v1/annexes/{annex_id}/dry-runs", {"body": None},
                                   annex_id=aid)  # fmt: skip
    assert r.status_code == 404


def test_removal_settings_pause_by_owners_resume_by_admins(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner = web.browser(w.owner)
    etag = owner.get("/v1/annexes/{annex_id}", annex_id=aid).headers["ETag"]

    def patch(b, body):
        etag = b.get("/v1/annexes/{annex_id}", annex_id=aid).headers["ETag"]
        return b.call("PATCH", "/v1/annexes/{annex_id}", json=body,
                      headers={"If-Match": etag}, annex_id=aid)  # fmt: skip

    assert etag
    r = patch(owner, {"removals_paused": True, "pointer_files": True})
    assert r.status_code == 200, r.text
    assert r.json()["removals"]["paused"] is True
    assert patch(owner, {"removals_paused": False}).status_code == 403
    assert patch(owner, {"removal_rate_per_hour": 50}).status_code == 403
    admin = web.browser(w.admin)
    sql.execute("UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'"
                " WHERE entra_object_id = %s", (w.admin["oid"],))  # fmt: skip
    r = patch(admin, {"removals_paused": False})
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    admin.sign_in(w.admin, reauth=True)
    r = patch(
        admin, {"removals_paused": False, "removal_window": {"start": "22:00", "end": "05:00"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["removals"]["window"] == {"start": "22:00", "end": "05:00"}


def test_the_global_pause_lives_in_the_annex_defaults(web, sql) -> None:
    w = seed(sql)
    admin = web.browser(w.admin)
    got = admin.get("/v1/settings/annex-defaults")
    assert got.json()["removals_paused"] is False and got.json()["dry_run_days"] == 7
    r = admin.call("PUT", "/v1/settings/annex-defaults", json={"removals_paused": True},
                   headers={"If-Match": got.headers["ETag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["removals_paused"] is True
    r = admin.call("PUT", "/v1/settings/annex-defaults", json={"removals_paused": False},
                   headers={"If-Match": r.headers["ETag"]})  # fmt: skip
    assert r.status_code == 200, r.text


# --- A2.2: the end of the dry run (decision 59) -----------------------------------------------


def _removing_annex(web, sql) -> tuple[World, str]:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner = web.browser(w.owner)
    owner.post("/v1/annexes/{annex_id}/rulesets", {"body": _archive_rules(ds)}, annex_id=aid)
    r = _approve(web.browser(w.admin), aid, 2)
    assert r.status_code == 200, r.text
    sql.execute("UPDATE catalog.annex SET state = 'active' WHERE annex_id = %s", (int(aid),))
    return w, aid


def _confirm(b, aid: str):
    got = b.get("/v1/annexes/{annex_id}", annex_id=aid)
    return b.call("POST", "/v1/annexes/{annex_id}/removals/confirm", json={},
                  headers={"If-Match": got.headers["ETag"]}, annex_id=aid)  # fmt: skip


def test_removals_start_only_when_an_archive_admin_confirms_the_dry_run(web, sql) -> None:
    w, aid = _removing_annex(web, sql)
    admin = web.browser(w.admin)
    # Not before the dry run has ended.
    r = _confirm(admin, aid)
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"
    sql.execute("UPDATE catalog.annex SET dry_run_until = now() - interval '1 minute'"
                " WHERE annex_id = %s", (int(aid),))  # fmt: skip
    got = admin.get("/v1/annexes/{annex_id}", annex_id=aid).json()
    assert got["removals"]["enabled"] is False, "never by itself on day 8"
    # Not an Owner, nor the person who proposed the rules.
    r = _confirm(web.browser(w.owner), aid)
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    proposer = w.user("owner", roles=["ArchiveAdmin"], groups=[w.g_owners], sid_rid=3002)
    r = _confirm(web.browser(proposer), aid)
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    # A fresh sign-in.
    sql.execute("UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'"
                " WHERE entra_object_id = %s", (w.admin["oid"],))  # fmt: skip
    r = _confirm(admin, aid)
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    admin.sign_in(w.admin, reauth=True)
    r = _confirm(admin, aid)
    assert r.status_code == 200, r.text
    rem = r.json()["removals"]
    assert rem["enabled"] is True and rem["confirmed_version"] == 2
    assert sql.execute("SELECT count(*) FROM catalog.audit_log WHERE action ="
                       " 'annex.removals.confirm'").fetchone()[0] >= 1  # fmt: skip
    # A new version needs its own confirmation.
    owner = web.browser(w.owner)
    r = owner.post("/v1/annexes/{annex_id}/rulesets",
                   {"body": _archive_rules(r.json()["dataset_id"], 20)},
                   annex_id=aid)  # fmt: skip
    assert r.status_code == 201
    assert _approve(admin, aid, 3).json()["removals"]["enabled"] is False
    listed = admin.get("/v1/annexes/{annex_id}/removals", annex_id=aid).json()
    assert listed["live"] is False and listed["why_not"] in ("dry_run", "not_confirmed")
    assert listed["items"] == []


# --- #57's review: removals' scope, previews' reach, approvals' ETag ------------------------


def _scoped_archive(ds: str) -> dict:
    body = _archive_rules(ds)
    body["rules"].insert(0, {"id": "scratch", "trigger": "arrived",
                             "conditions": {"path": "**/scratch/*"},
                             "actions": [{"exclude": True}]})  # fmt: skip
    return body


def test_widening_what_a_removing_set_copies_needs_an_archive_admin_and_a_new_dry_run(
    web, sql
) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    owner.post("/v1/annexes/{annex_id}/rulesets", {"body": _scoped_archive(ds)}, annex_id=aid)
    assert _approve(admin, aid, 2).status_code == 200
    sql.execute("UPDATE catalog.annex SET dry_run_until = now() - interval '1 day'"
                " WHERE annex_id = %s", (int(aid),))  # fmt: skip
    # v3 keeps the removal as it is, but copies the scratch files too: the
    # age rule now reaches them.
    r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": _archive_rules(ds)}, annex_id=aid)
    assert r.status_code == 201 and r.json()["needs_archive_admin"] is True, r.text
    # Computed again at approval, whatever was stored.
    sql.execute("UPDATE catalog.annex_ruleset SET needs_archive_admin = false"
                " WHERE annex_id = %s AND version = 3", (int(aid),))  # fmt: skip
    r = _approve(web.browser(_owner2(w)), aid, 3)
    assert r.status_code == 403 and r.json()["code"] == "role_required", r.text
    r = _approve(admin, aid, 3)
    assert r.status_code == 200, r.text
    fresh = sql.execute(
        "SELECT dry_run_until > now() + interval '6 days 23 hours' FROM catalog.annex"
        " WHERE annex_id = %s",
        (int(aid),),
    ).fetchone()[0]
    assert fresh, "a fresh 7-day dry run"
    decided_by = sql.execute(
        "SELECT i.sid FROM catalog.annex_ruleset r JOIN catalog.identity i"
        "  ON i.identity_id = r.decided_by_identity_id WHERE r.annex_id = %s AND r.version = 3",
        (int(aid),),
    ).fetchone()[0]
    assert decided_by == w.admin["sid"]


def test_a_removing_set_is_never_approved_by_an_owner_even_unchanged(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    owner.post("/v1/annexes/{annex_id}/rulesets", {"body": _archive_rules(ds)}, annex_id=aid)
    assert _approve(admin, aid, 2).status_code == 200
    # The same rules again (a new label, say): an Owner still can't.
    body = _archive_rules(ds)
    body["rules"][0]["label"] = "everything"
    owner.post("/v1/annexes/{annex_id}/rulesets", {"body": body}, annex_id=aid)
    r = _approve(web.browser(_owner2(w)), aid, 3)
    assert r.status_code == 403 and r.json()["code"] == "role_required"


def test_every_proposal_moves_the_annex_etag(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    o2 = web.browser(_owner2(w))
    stale = o2.get("/v1/annexes/{annex_id}", annex_id=aid).headers["ETag"]
    web.browser(w.owner).post("/v1/annexes/{annex_id}/rulesets", {"body": _scratch_rules(ds)},
                              annex_id=aid)  # fmt: skip
    r = o2.call("POST", "/v1/annexes/{annex_id}/rulesets/{version}/approve", json={},
                headers={"If-Match": stale}, annex_id=aid, version=2)  # fmt: skip
    assert r.status_code == 412, r.text
    # The unversioned route is gone: an approval always names its version.
    r = o2.call("POST", "/v1/annexes/{annex_id}/ruleset/approve", json={},
                headers={"If-Match": stale}, annex_id=aid, check=False)  # fmt: skip
    assert r.status_code in (400, 404, 405), r.text


def test_previews_are_an_archive_admins_until_folder_acls_come_from_the_nas(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    # An existing folder the Owner can read in the catalog: today an annex's
    # folders carry the Owners' ACL, not the NAS's, so it proves nothing.
    (root,) = sql.execute("SELECT file_id FROM catalog.file WHERE dataset_id = %s"
                          " AND parent_id IS NULL", (int(ds),)).fetchone()  # fmt: skip
    owners_acl = _acl(sql, int(ds), [w.g_owners])
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (owners_acl, root))
    sql.execute("INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id)"
                " VALUES (%s, %s, 'hr', 'dir', %s)", (int(ds), root, owners_acl))  # fmt: skip
    tier = {"kind": "tier", "dataset_id": ds}
    url = "/v1/annexes/{annex_id}/dry-runs"
    for glob in ("hr/smith.xlsx", "smith-salary*.xlsx", "**/hr/payroll/smith.xlsx"):
        body = _body(
            {"id": "probe", "trigger": "arrived", "conditions": {"path": glob},
             "actions": [{"exclude": True}]},
            {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": tier}]},
        )  # fmt: skip
        r = owner.post(url, {"body": body}, annex_id=aid)
        assert r.status_code == 403 and r.json()["code"] == "role_required", (glob, r.text)
    r = owner.post(url, {"body": None}, annex_id=aid)
    assert r.status_code == 403, "nor the running set"
    r = admin.post(url, {"body": body}, annex_id=aid)
    assert r.status_code == 202, r.text


def test_a_newer_draft_replaces_the_older_and_runs_nobody_takes_expire(web, sql) -> None:
    w, a, ds = _setup(web, sql)
    aid = a["annex_id"]
    owner = web.browser(w.admin)
    url = "/v1/annexes/{annex_id}/dry-runs"
    first = owner.post(url, {"body": _scratch_rules(ds)}, annex_id=aid).json()
    second = owner.post(url, {"body": _scratch_rules(ds)}, annex_id=aid).json()
    got = owner.get(url + "/{run_id}", annex_id=aid, run_id=first["run_id"]).json()
    assert got["state"] == "failed" and "newer draft" in got["error"]
    sql.execute("UPDATE catalog.annex_dry_run SET requested_at = now() - interval '11 minutes'"
                " WHERE run_id = %s", (int(second["run_id"]),))  # fmt: skip
    got = owner.get(url + "/{run_id}", annex_id=aid, run_id=second["run_id"]).json()
    assert got["state"] == "failed" and "in time" in got["error"]
    # Expired runs don't hold the throttle.
    sql.execute(
        "INSERT INTO catalog.annex_dry_run (annex_id, body, requested_by, requested_at)"
        " SELECT %s, '{}', 'user:x', now() - interval '1 hour' FROM generate_series(1, 20)",
        (int(aid),),
    )
    assert owner.post(url, {"body": _scratch_rules(ds)}, annex_id=aid).status_code == 202


def test_only_archive_admins_see_what_the_dry_run_would_remove(web, sql) -> None:
    w, aid = _removing_annex(web, sql)
    url = "/v1/annexes/{annex_id}/removals"
    assert web.browser(w.owner).get(url, annex_id=aid).json()["would_remove"] is None
    seen = web.browser(w.admin).get(url, annex_id=aid).json()["would_remove"]
    assert seen is not None and set(seen) == {"files", "bytes", "samples"}


# --- return rules (phase A3.1) ------------------------------------------------------------


def test_templates_carry_their_default_return_rules() -> None:
    for name, days, back in (("copy", None, "origin"), ("archive_after", 30, "origin"),
                             ("move_now", None, None)):  # fmt: skip
        body = _norm(R.template(name, DS, days))
        assert body.get("return_template") == back, name
        assert R.mode_of(body) == name, "the outbound template is still recognised"
        assert R.checks(body, quiet_minutes=10) == [], name
    loc = {"kind": "share_folder", "share_id": "3", "path": "recalls"}
    body = R.copy_template(DS)
    body["rules"] += R.return_rules("share", loc)
    assert _norm(body)["return_template"] == "share"
    [rule] = R.returns(_norm(body)["rules"])
    assert R.rule_get(rule, "on_conflict") == "overwrite_keep_version"


def test_return_locations_are_normalised_and_refused_when_odd() -> None:
    assert R.location({"kind": "nas_folder", "origin": True}, "f") == {
        "kind": "nas_folder", "origin": True}  # fmt: skip
    assert R.location({"kind": "nas_folder", "annex_id": 7, "path": "/restore/"}, "f") == {
        "kind": "nas_folder", "path": "restore", "annex_id": "7"}  # fmt: skip
    assert R.location({"kind": "share_folder", "share_id": "3", "keep_relative_path": False},
                      "f") == {"kind": "share_folder", "share_id": "3", "path": "",
                               "keep_relative_path": False}  # fmt: skip
    for bad in (
        {"kind": "nas_folder", "origin": True, "path": "x"},
        {"kind": "nas_folder", "annex_id": 1, "filer_id": 2},
        {"kind": "nas_folder", "path": "a/../b"},
        {"kind": "nas_folder", "path": "a/.omnifile-staging"},
        {"kind": "share_folder"},
        {"kind": "tier"},
        {"kind": "nas_folder", "colour": "red"},
    ):
        with pytest.raises(R.RulesError):
            R.location(bad, "f")


@pytest.mark.parametrize(
    "bad",
    [
        # requester_in is for return rules; ages and types aren't known for them.
        _body({"trigger": "arrived", "conditions": {"requester_in": ["S-1-5-21-1-2-3-4"]},
               "actions": [COPY]}),
        _body({"trigger": "recall_completed", "conditions": {"age_since_arrival_days_gt": 3},
               "actions": [{"exclude": True}]}),
        _body({"trigger": "recall_completed", "conditions": {"content_type": ["segy"]},
               "actions": [{"exclude": True}]}),
        # A return rule places the file or excludes it, with nothing outbound.
        _body({"trigger": "recall_completed", "conditions": {}, "actions": [{"hold_days": 3}]}),
        _body({"trigger": "recall_completed", "conditions": {}, "actions": [COPY]}),
        _body({"trigger": "recall_completed", "conditions": {},
               "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                           {"on_conflict": "clobber"}]}),
        _body({"trigger": "recall_completed", "conditions": {},
               "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                           {"hold_days": 400}]}),
        _body({"trigger": "recall_completed", "conditions": {},
               "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                           {"notify": ["everyone"]}]}),
        _body({"trigger": "arrived", "conditions": {},
               "actions": [{"place_at": {"kind": "nas_folder", "origin": True}}]}),
    ],
)  # fmt: skip
def test_a_return_rule_that_isnt_well_formed_is_refused(bad: dict) -> None:
    with pytest.raises(R.RulesError):
        _norm(bad)


def test_the_service_matches_globs_as_the_edge_does() -> None:
    # The cases of the edge's rules.rs tests.
    assert R.glob_match("*.segy", "surveys/2026/a.segy")
    assert R.glob_match("surveys/**/raw/*", "surveys/raw/x")
    assert R.glob_match("surveys/**/raw/*", "surveys/a/b/raw/x")
    assert not R.glob_match("surveys/*/raw/*", "surveys/a/b/raw/x")
    assert R.glob_match("**/**/**/raw/*", "raw/x"), "runs of ** are one"
    assert R.glob_match("a?c", "abc") and not R.glob_match("a?c", "ac")
    assert R.glob_match("a[1]", "a[1]"), "brackets are plain characters"
    assert R.ext_match(["sgy.gz"], "x/LINE.SGY.GZ")


def test_a_removing_rule_over_a_return_target_is_found_conservatively() -> None:
    may = R.glob_may_match_under
    assert may("*.segy", "restore"), "a name glob matches in any folder"
    assert may("surveys/**/raw/*", "surveys")
    assert may("surveys/*/raw/*", "surveys/2026")
    assert not may("surveys/*/raw/*", "scratch")
    assert not may("surveys/raw/*", "surveys/raw/deep"), "files directly in raw/ only"
    assert may("surveys/**", "surveys/raw/deep")
    assert may("a[1]/*", "a[1]") and not may("a[1]/*", "a1")


def test_a_return_target_outside_the_annex_needs_an_archive_admin() -> None:
    copy = _norm(R.template("copy", DS))
    assert not R.needs_archive_admin(copy, None), "the origin is the annex's own folder"
    own = R.copy_template(DS)
    own["rules"] += R.return_rules("restore_folder", {"kind": "nas_folder", "path": "restored"})
    assert not R.needs_archive_admin(_norm(own), copy)
    out = R.copy_template(DS)
    loc = {"kind": "nas_folder", "filer_id": "4", "path": "restore"}
    out["rules"] += R.return_rules("restore_folder", loc)
    out = _norm(out)
    assert R.needs_archive_admin(out, copy)
    assert not R.needs_archive_admin(out, out), "already approved by one"
