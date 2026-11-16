"""Teardown by tag, by recorded id and by the run's names, WORM first, each part isolated, and
its proof; and that it still runs, clean, after a failure at any point of a run."""

from __future__ import annotations

import json

import pytest
from conftest import harness

RUN = "rr-dry-0001"
PREFIX = "r04a37bf"  # prefix_for(RUN)


def left_behind(r) -> list[str]:
    """What the fake Azure still holds of the run, independent of the harness's own proof:
    tagged or untagged, by the run's names too (the review found apps the old check missed)."""
    s = r.fake_state()
    left = [
        g
        for g, v in s["groups"].items()
        if v["tags"].get("omnifile-run") == RUN or g == f"rg-ofreal-{RUN}"
    ]
    left += [x["id"] for x in s["resources"] if x["tags"].get("omnifile-run") == RUN]
    left += [x["id"] for x in s["resources"] if x["resourceGroup"] == f"rg-ofreal-{RUN}"]
    left += [
        v["name"]
        for v in s["deleted_vaults"]
        if v["properties"]["tags"].get("omnifile-run") == RUN or PREFIX in v["name"]
    ]
    left += [
        a["displayName"]
        for a in s["apps"]
        if f"omnifile-run:{RUN}" in a.get("tags", []) or PREFIX in a["displayName"]
    ]
    left += [f"teams app {t['id']}" for t in s["teams_apps"]]
    left += [d["roleName"] for d in s["role_defs"]]
    left += [g["displayName"] for g in s["ad_groups"]]
    return sorted(set(left))


@pytest.mark.parametrize(
    "step",
    [
        "plan.whatif",
        "plan.registry",
        "guide.2",
        "guide.5",
        "guide.9",
        "vm.provision",
        "vm.appliance",
        "guide.13",
        "exit.read-back",
        "exit.worm-immutable",
        "check.RA-10",
        "up.adopt",
    ],
)
def test_azure_teardown_after_a_failure_injected_mid_run_is_clean(step, out):
    r = harness("all", "azure", out, "--inject-failure", step)
    assert r.code != 0, f"{step}: the injected failure was lost"
    assert f"failure injected at azure.{step}" in r.stdout
    assert r.state["down"] is True
    assert r.teardown["runs"][RUN]["residue"] == []
    assert left_behind(r) == []


def test_apps_made_by_a_step_that_fails_after_them_are_still_removed(out):
    """The review's case: the portal app and the Teams catalog app exist, untagged, when a
    later step fails (here, adoption's tagging itself)."""
    r = harness("all", "azure", out, env={"REAL_FAKE_FAIL": r"^az rest --method patch"})
    assert r.code != 0
    assert left_behind(r) == []
    assert not r.fake_state()["teams_apps"]


@pytest.mark.parametrize(
    "step",
    [
        "up.buckets",
        "up.network",
        "up.roles-anywhere",
        "up.probe",
        "guide.17",
        "check.RW-01",
        "check.RW-19",
    ],
)
def test_aws_teardown_after_a_failure_injected_mid_run_is_clean(step, out):
    r = harness("all", "aws", out, "--inject-failure", step)
    assert r.code != 0
    assert f"failure injected at aws.{step}" in r.stdout
    assert r.state["down"] is True
    assert r.teardown["runs"][RUN]["residue"] == []
    f = out / "fake-state.json-aws.json"  # absent if nothing reached Roles Anywhere
    extra = json.loads(f.read_text()) if f.exists() else {"anchors": {}, "profiles": {}}
    assert not [a for a in extra["anchors"].values() if RUN in a["name"]]
    assert not extra["profiles"]


@pytest.mark.parametrize("step", ["up.partner-probe", "exit.card-sent", "check.RT-13"])
def test_tenant_teardown_after_a_failure_injected_mid_run_is_clean(step, out):
    r = harness("all", "tenant", out, "--inject-failure", step)
    assert r.code != 0
    assert r.teardown["runs"][RUN]["residue"] == []
    apps = r.fake_state()["apps"] if (out / "fake-state.json").exists() else []
    assert not [a for a in apps if "partner-probe" in a["displayName"]]


def test_the_partner_probe_app_goes_even_when_tagging_it_failed(out):
    r = harness("all", "tenant", out, env={"REAL_FAKE_FAIL": r"^az rest --method patch"})
    assert r.code != 0
    assert not [a for a in r.fake_state()["apps"] if "partner-probe" in a["displayName"]]
    assert r.teardown["runs"][RUN]["residue"] == []


def test_a_cloud_call_that_fails_inside_a_step_still_gets_a_clean_teardown(out):
    r = harness("all", "azure", out, env={"REAL_FAKE_FAIL": r"^az vm create"})
    assert r.code == 1
    assert r.teardown["runs"][RUN]["residue"] == []
    assert left_behind(r) == []


def _up(out, **kw):
    assert harness("plan", "azure", out, **kw).code == 0
    p = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto", **kw)
    assert p.code == 0, p.stdout[-1500:]
    e = harness("exit", "azure", out, "--run-id", RUN, **kw)
    assert e.code == 0, e.stdout[-1500:]


def _edit_fake(out, fn):
    f = out / "fake-state.json"
    s = json.loads(f.read_text())
    fn(s)
    f.write_text(json.dumps(s))


def test_the_group_goes_even_when_a_guide_step_stripped_its_tags(out):
    """The fixture's step 2 re-creates the group with no tags, as the guide's step 10 could:
    teardown finds it by its name."""
    _up(out)
    assert not json.loads((out / "fake-state.json").read_text())["groups"][f"rg-ofreal-{RUN}"][
        "tags"
    ]
    r = harness("down", "azure", out, "--run-id", RUN)
    assert r.code == 0, r.stdout[-1500:]
    assert left_behind(r) == []


def test_worm_comes_first_locks_and_holds_then_policies_then_the_group(out):
    _up(out)

    def add(s):
        s["locks"].append(
            {"id": "/locks/worm", "name": "worm-cannot-delete", "rg": f"rg-ofreal-{RUN}"}
        )
        acct = next(iter(s["worm"]))
        s["worm"][acct]["containers"]["data"]["legal_hold"] = ["case42"]

    _edit_fake(out, add)
    n = len((out / "transcript.txt").read_text().splitlines())
    r = harness("down", "azure", out, "--run-id", RUN)
    assert r.code == 0, r.stdout[-2000:]
    calls = r.transcript[n:]

    def at(prefix):
        return next(i for i, c in enumerate(calls) if c.startswith(prefix))

    group = at("az group delete")
    assert at("az lock delete") < group
    assert at("az storage container legal-hold clear") < group
    assert at("az storage container immutability-policy delete") < group
    # the version policies and holds are read one version at a time: az's list shows neither
    assert at("az storage blob show") < at("az storage blob immutability-policy delete") < group
    assert at("az storage blob set-legal-hold") < group
    assert left_behind(r) == []


def test_403s_while_the_data_role_propagates_are_retried(out):
    _up(out)
    r = harness("down", "azure", out, "--run-id", RUN, env={"REAL_FAKE_403": "3"})
    assert r.code == 0, r.stdout[-2000:]
    assert r.stdout.count("data plane not yet allowed") == 3
    assert left_behind(r) == []


def test_without_the_worm_step_azure_would_refuse_the_delete(out):
    """The fake refuses a group delete while WORM data is held, as Azure does: so the order
    above is what makes teardown work, not luck."""
    import subprocess
    import sys

    from conftest import REAL, clean_env

    _up(out)
    env = clean_env(
        {
            "REAL_FAKE_PY": sys.executable,
            "REAL_FAKE_STATE": str(out / "fake-state.json"),
            "REAL_FAKE_TRANSCRIPT": str(out / "t2.txt"),
        }
    )
    p = subprocess.run(
        [str(REAL / "fakes/bin/az"), "group", "delete", "-n", f"rg-ofreal-{RUN}", "--yes"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1
    assert "immutable" in p.stderr
    harness("down", "azure", out, "--run-id", RUN)


def test_a_locked_policy_holds_its_account_and_everything_else_still_goes(out):
    _up(out)

    def lock(s):
        acct = next(iter(s["worm"]))
        s["worm"][acct]["containers"]["data"]["policy"]["state"] = "Locked"

    _edit_fake(out, lock)
    n = len((out / "transcript.txt").read_text().splitlines())
    r = harness("down", "azure", out, "--run-id", RUN)
    assert r.code == 1
    run = r.teardown["runs"][RUN]
    assert any("WORM account" in x and "LOCKED" in x for x in run["residue"])
    calls = r.transcript[n:]
    assert not [c for c in calls if c.startswith("az group delete")]
    assert not [
        c
        for c in calls
        if c.startswith("az storage container immutability-policy delete") and " -c data " in c
    ]
    # The rest of the group went one by one: the VM and PostgreSQL first of all.
    left = {
        x["name"] for x in r.fake_state()["resources"] if x["resourceGroup"] == f"rg-ofreal-{RUN}"
    }
    worm = next(iter(r.fake_state()["worm"]))
    assert left == {worm}, left
    # And the Entra objects, outside the group, too.
    assert not [a for a in r.fake_state()["apps"] if PREFIX in a["displayName"]]


def test_teardown_touches_only_this_run_and_sweeps_expired_runs_on_request(out):
    _up(out)
    live, old = "rr-202611040000-aaaa", "rr-202611010000-bbbb"

    def others(s):
        s["groups"]["rg-owner-prod"] = {
            "name": "rg-owner-prod",
            "location": "x",
            "tags": {},
            "id": "/x",
        }
        for rid, exp in ((live, "2026-11-04T00:00:00Z"), (old, "2026-11-01T00:00:00Z")):
            s["groups"][f"rg-ofreal-{rid}"] = {
                "name": f"rg-ofreal-{rid}",
                "location": "x",
                "id": "/y",
                "tags": {
                    "omnifile-run": rid,
                    "omnifile-expires": exp,
                    "omnifile-harness": "tests-real",
                },
            }
        # A tag anyone could write, aimed at the OData filters: never used.
        s["groups"]["rg-evil"] = {
            "name": "rg-evil",
            "location": "x",
            "id": "/z",
            "tags": {
                "omnifile-run": "x') or startswith(displayName,'",
                "omnifile-harness": "tests-real",
                "omnifile-expires": "2026-11-01T00:00:00Z",
            },
        }

    _edit_fake(out, others)
    r = harness("down", "azure", out, "--run-id", RUN)
    assert r.code == 0
    assert set(r.fake_state()["groups"]) == {
        "rg-owner-prod",
        f"rg-ofreal-{live}",
        f"rg-ofreal-{old}",
        "rg-evil",
    }
    n = len(r.transcript)
    r = harness("down", "azure", out, "--run-id", RUN, "--expired")
    assert r.code == 0, r.stdout[-1500:]
    assert set(r.fake_state()["groups"]) == {"rg-owner-prod", f"rg-ofreal-{live}", "rg-evil"}
    assert not [c for c in r.transcript[n:] if "or startswith" in c]
    assert "is not a run id" in r.stdout


def test_a_purge_protected_vault_is_reported_and_named(out):
    p = harness("plan", "azure", out)
    assert p.code == 0
    r = harness(
        "up",
        "azure",
        out,
        "--run-id",
        RUN,
        "--approve",
        "auto",
        env={"REAL_FAKE_PURGE_PROTECTION": "1"},
    )
    assert r.code == 0
    r = harness("down", "azure", out, "--run-id", RUN, env={"REAL_FAKE_PURGE_PROTECTION": "1"})
    assert r.code == 1  # the soft-deleted vault is still the run's: the proof says so
    run = r.teardown["runs"][RUN]
    assert any("could not be purged" in w for w in run["warnings"])
    assert any("soft-deleted vault" in x for x in run["residue"])


def test_a_down_with_no_state_is_clean(tmp_path):
    r = harness("down", "azure", tmp_path / "empty", "--run-id", "rr-dry-0002")
    assert r.code == 0, r.stdout[-1500:]
    assert r.teardown["runs"]["rr-dry-0002"]["residue"] == []


def test_the_aws_run_adopts_only_what_is_its_own(out):
    r = harness("all", "aws", out)
    assert r.code == 0, r.stdout[-1500:]
    kinds = sorted(a["kind"] for a in r.state["adopted"])
    assert kinds == ["bucket", "rolesanywhere"]
    extra = json.loads((out / "fake-state.json-aws.json").read_text())
    # someone else's anchor, made while the steps ran, is untouched
    assert [a["name"] for a in extra["anchors"].values()] == ["someone-elses-anchor"]
    assert "not adopted" in r.stdout and "someone-elses-anchor" in r.stdout


def test_one_failing_aws_teardown_part_doesnt_stop_the_rest(out):
    r = harness("all", "aws", out, env={"REAL_FAKE_FAIL_BOTO": r"^events\.DeleteRule "})
    assert r.code == 1
    run = r.teardown["runs"][RUN]
    assert any("down.events" in e for e in run["errors"])
    done = " ".join(run["done"])
    for later in (
        "bucket ofreal-rr-dry-0001-src",
        "queue ofreal-rr-dry-0001-events",
        "trust anchor ofreal-rr-dry-0001-edge-ca",
        "role ofreal-rr-dry-0001-edge-s3-read",  # the run's only role (the rest are the owner's)
    ):
        assert later in done, later


def test_an_untagged_bucket_of_the_run_is_still_removed(out):
    """NoSuchTagSet: tagging the guide's bucket failed, so it has no tags at all; its name
    (ofreal-<run id>-...) is the run's, so it goes."""
    r = harness("all", "aws", out, env={"REAL_FAKE_FAIL_BOTO": r"^s3\.PutBucketTagging .*-guide"})
    assert r.code != 0
    assert "bucket ofreal-rr-dry-0001-guide" in " ".join(r.teardown["runs"][RUN]["done"])
    assert r.teardown["runs"][RUN]["residue"] == []
