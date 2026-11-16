"""The re-review of #36 (at 272c138): each finding, and a test that fails without its fix."""

from __future__ import annotations

import json
import os

import pytest
from conftest import REAL, REPO, harness

RUN = "rr-dry-0001"
SUB = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000ff"
#: Two real-shaped run ids whose 5-hex prefixes collided (rc3d36) before the fix.
A, B = "rr-202611030004-0043", "rr-202611030009-009e"


def _edit_fake(out, fn):
    f = out / "fake-state.json"
    s = json.loads(f.read_text())
    fn(s)
    f.write_text(json.dumps(s))


def _up(out, **kw):
    assert harness("plan", "azure", out, **kw).code == 0
    p = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto", **kw)
    assert p.code == 0, p.stdout[-1500:]


# ------------------------------------------------- 1. teardown by name, other runs' objects


def test_prefixes_are_long_enough_that_the_colliding_pair_no_longer_collides():
    from harness.core import prefix_for

    assert prefix_for(A) != prefix_for(B)
    assert all(len(prefix_for(r)) == 8 and prefix_for(r).isalnum() for r in (A, B, RUN))


def test_names_the_harness_picks_carry_the_whole_run_id():
    from harness import guidevalues
    from harness.tenant import probe_name

    assert RUN in guidevalues.pg_admin_name(RUN)
    assert RUN in probe_name(RUN)


def test_teardown_leaves_name_matched_objects_that_are_another_runs(out):
    from harness.core import prefix_for

    _up(out)
    p = prefix_for(RUN)
    old = "rr-202611010000-bbbb"

    def seed(s):
        s["apps"] += [
            # Same name as this run's portal app, but another run's tag: never ours.
            {
                "id": "foreign-tagged",
                "appId": "foreign-tagged-app",
                "displayName": f"OmniFile {p} portal",
                "tags": [f"omnifile-run:{B}"],
                "createdDateTime": "2026-11-03T00:00:00Z",
            },
            # Same name, untagged, made before this run began: not ours either.
            {
                "id": "foreign-old",
                "appId": "foreign-old-app",
                "displayName": f"{p}-bot",
                "tags": [],
                "createdDateTime": "2026-10-01T09:00:00.1234567Z",
            },
            # Untagged, named for this run, made inside its window: ours (a failed tagging).
            {
                "id": "ours-untagged",
                "appId": "ours-untagged-app",
                "displayName": f"{p}-relay",
                "tags": [],
                "createdDateTime": "2026-11-03T00:00:00.5000000Z",
            },
            # An expired run's untagged, name-matched app: a sweep goes by tag only.
            {
                "id": "old-untagged",
                "appId": "old-untagged-app",
                "displayName": f"OmniFile {prefix_for(old)} portal",
                "tags": [],
                "createdDateTime": "2026-11-01T00:00:00Z",
            },
            {
                "id": "old-tagged",
                "appId": "old-tagged-app",
                "displayName": "anything",
                "tags": [f"omnifile-run:{old}"],
                "createdDateTime": "2026-11-01T00:00:00Z",
            },
        ]
        s["ad_groups"].append(
            {
                "id": "foreign-group",
                "displayName": f"ofreal-{RUN}-borrowed",
                "description": f"omnifile-run={B}",
                "createdDateTime": "2026-11-03T00:00:00Z",
            }
        )
        s["groups"][f"rg-ofreal-{old}"] = {
            "name": f"rg-ofreal-{old}",
            "location": "x",
            "id": "/old",
            "tags": {
                "omnifile-run": old,
                "omnifile-expires": "2026-11-01T12:00:00Z",
                "omnifile-harness": "tests-real",
            },
        }

    _edit_fake(out, seed)
    r = harness("down", "azure", out, "--run-id", RUN, "--expired")
    assert r.code == 0, r.stdout[-2000:]
    s = r.fake_state()
    ids = {a["id"] for a in s["apps"]}
    assert ids == {"foreign-tagged", "foreign-old", "old-untagged"}, ids
    assert [g["id"] for g in s["ad_groups"]] == ["foreign-group"]
    assert "carries another run's tag" in r.stdout
    assert "outside this run" in r.stdout
    assert "found by tag only" in r.stdout


def test_aws_adopts_by_the_whole_run_id_not_the_prefix(tmp_path):
    from harness.aws import AWS
    from harness.core import Context, prefix_for

    ctx = Context(
        phase="up",
        cloud="aws",
        dry_run=True,
        out=tmp_path,
        run_id=A,
        now=__import__("datetime").datetime(2026, 11, 3),
    )
    aws = AWS.__new__(AWS)
    aws.ctx = ctx
    assert aws.adoptable(f"ofreal-{A}-guide")
    assert aws.adoptable(f"omnifile-edge-ca-{A}")
    assert not aws.adoptable(f"{prefix_for(A)}-edge-s3-read")
    assert not aws.adoptable(f"ofreal-{A}1-guide")
    assert not aws.adoptable(f"ofreal-{B}-guide")


# ------------------------------------------- 2. the az sign-in copy: after the check, deleted


def _owner(out, default=SUB, extra=("msal_token_cache.json",)):
    from test_review import owner_profile

    f = owner_profile(out, default)
    for name in extra:
        (f.parent / name).write_text('{"fake": "token cache"}')
    return f.parent


def test_no_sign_in_is_copied_before_the_allow_list_check(out, tmp_path):
    _owner(out)
    deny = tmp_path / "deny"
    deny.write_text(
        (REAL / "fakes" / "allow-list.dry")
        .read_text()
        .replace("azure-subscription    " + SUB, "azure-subscription    " + OTHER)
    )
    r = harness("plan", "azure", out, allow=deny)
    assert r.code == 4, r.stdout[-1500:]
    dirs = {d for _, d in r.fake_state()["az_config_dirs"]}
    assert dirs == {str(out / "dry" / "azure")}, dirs
    assert not (out / "az-config").exists()


@pytest.mark.parametrize("fail", [None, "guide.5"])
def test_the_sign_in_copy_is_gone_when_a_phase_ends(out, fail):
    _owner(out)
    assert harness("plan", "azure", out).code == 0
    assert not (out / "az-config").exists()
    args = ["--inject-failure", fail] if fail else []
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto", *args)
    assert r.code == (1 if fail else 0), r.stdout[-1500:]
    # the phase did run pinned, in its own copy ...
    assert str(out / "az-config") in {d for _, d in r.fake_state()["az_config_dirs"]}
    # ... and the copy went with it
    assert not (out / "az-config").exists()
    harness("down", "azure", out, "--run-id", RUN)
    assert not (out / "az-config").exists()


def test_only_the_sign_in_az_needs_is_copied(tmp_path):
    from datetime import datetime

    from harness import azpin
    from harness.core import Context

    owner = tmp_path / "owner"
    owner.mkdir()
    for name in (
        "azureProfile.json",
        "msal_token_cache.json",
        "msal_http_cache.bin",
        "service_principal_entries.json",
        "config",
        "commandIndex.json",
    ):
        (owner / name).write_text("{}")
    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.env = {"AZURE_CONFIG_DIR": str(owner)}
    ctx.run = lambda *a, **k: None  # type: ignore[method-assign]
    ctx.az = lambda *a, **k: {"id": SUB}  # type: ignore[method-assign]
    d = azpin.pin(ctx, SUB, service_principal=False)
    assert sorted(x.name for x in d.iterdir()) == ["azureProfile.json", "msal_token_cache.json"]
    assert ctx.env["AZURE_CONFIG_DIR"] == str(d)
    azpin.unpin(ctx)
    assert not d.exists() and ctx.env["AZURE_CONFIG_DIR"] == str(owner)
    d = azpin.pin(ctx, SUB, service_principal=True)
    assert "service_principal_entries.json" in {x.name for x in d.iterdir()}
    azpin.unpin(ctx)


@pytest.mark.parametrize("where", ["tests/real/results/x", "docs/x", "."])
def test_an_out_inside_the_repository_is_refused(where):
    target = REPO / where / "rr-dry-0001-out-test"
    r = harness("plan", "azure", target)
    assert r.code == 4 and "inside the repository" in r.stdout, r.stdout[-800:]
    assert not target.exists()


def test_an_out_under_tests_real_out_is_allowed():
    from harness.__main__ import check_out

    check_out(REAL / "out" / RUN)  # no exception


# --------------------------------------------- 4. pinned within a phase, not only between


def test_a_subscription_switch_mid_phase_leaves_the_run_where_it_was(out):
    """Another shell's `az account set` lands between two calls of the same phase (after the
    resource group is made, before the registry): every later call still acts in SUB."""
    owner = _owner(out)
    knobs = {"REAL_FAKE_SWITCH_AFTER": r"^group create", "REAL_FAKE_OWNER_AZ": str(owner)}
    r = harness("plan", "azure", out, env=knobs)
    assert r.code == 0, r.stdout[-1500:]
    prof = json.loads((owner / "azureProfile.json").read_text())
    assert [x["id"] for x in prof["subscriptions"] if x["isDefault"]] == [OTHER]  # it switched
    acted = r.fake_state()["acted_in"]
    assert len(acted) > 2 and set(acted) == {SUB}, acted
    _owner(out)  # back to SUB, to switch again in the middle of up
    knobs["REAL_FAKE_SWITCH_AFTER"] = r"^network vnet create"
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto", env=knobs)
    assert r.code == 0, r.stdout[-1500:]
    assert set(r.fake_state()["acted_in"]) == {SUB}
    harness("down", "azure", out, "--run-id", RUN)


# ------------------------------------- 5. a LOCKED policy whose retention ended is not forever


def _worm_up(out):
    assert harness("plan", "azure", out).code == 0
    p = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto")
    assert p.code == 0, p.stdout[-1500:]
    e = harness("exit", "azure", out, "--run-id", RUN)
    assert e.code == 0, e.stdout[-1500:]


def test_locked_versions_past_their_retention_are_deleted_and_the_account_goes(out):
    _worm_up(out)

    def lock(s):
        acct = next(iter(s["worm"]))
        data = s["worm"][acct]["containers"]["data"]
        data["policy"]["state"] = "Locked"  # 1 day from each blob's creation
        for b in data["blobs"]:
            b["created"] = "2026-10-01T00:00:00Z"
        s["worm"][acct]["containers"]["recall"]["blobs"].append(
            {
                "name": "old.segy",
                "versionId": "v9",
                "policy": {"expiryTime": "2026-11-01T00:00:00Z", "policyMode": "Locked"},
                "legal": False,
                "created": "2026-10-01T00:00:00Z",
            }
        )

    _edit_fake(out, lock)
    r = harness("down", "azure", out, "--run-id", RUN)
    assert r.code == 0, r.stdout[-2000:]
    done = " ".join(r.teardown["runs"][RUN]["done"])
    assert "deleted expired version" in done and "recall/old.segy@v9" in done
    assert "LOCKED policy, nothing under retention" in done
    assert r.teardown["runs"][RUN]["residue"] == []
    assert not r.fake_state()["worm"]
    assert f"rg-ofreal-{RUN}" not in r.fake_state()["groups"]


def test_a_locked_version_still_under_retention_is_held_and_says_until_when(out):
    _worm_up(out)

    def lock(s):
        acct = next(iter(s["worm"]))
        s["worm"][acct]["containers"]["recall"]["blobs"].append(
            {
                "name": "new.segy",
                "versionId": "v1",
                "policy": {"expiryTime": "2027-01-01T00:00:00Z", "policyMode": "Locked"},
                "legal": False,
                "created": "2026-11-02T00:00:00Z",
            }
        )

    _edit_fake(out, lock)
    r = harness("down", "azure", out, "--run-id", RUN)
    assert r.code == 1
    residue = " ".join(r.teardown["runs"][RUN]["residue"])
    assert "recall/new.segy@v1: LOCKED until 2027-01-01T00:00:00Z" in residue
    assert "deletable after then" in residue


# --------------------------------------- 7. the Azure run records its AD realm for the tenant


def test_the_azure_run_records_its_realm_and_the_tenant_stub_read_waits_for_it(out, tmp_path):
    from datetime import datetime

    from harness.core import Context
    from harness.tenant import Tenant

    _up(out)
    az = json.loads((out / "state.json").read_text())
    assert az["ad_realm"] == "OFREAL.TEST"
    ctx = Context(
        phase="exit",
        cloud="tenant",
        dry_run=False,
        out=tmp_path / "t",
        run_id="rr-202611030100-abcd",
        now=datetime(2026, 11, 3),
    )

    def no_run(*a, **k):
        raise AssertionError(f"the stub read ran against the run's own DC: {a}")

    ctx.run = no_run  # type: ignore[method-assign]
    t = Tenant.__new__(Tenant)
    t.ctx = ctx
    status, detail = t._exit_step("stub-read", az, "geo-writer@ofreal.test")
    assert status == "pending" and "RT-40" in detail
    harness("down", "azure", out, "--run-id", RUN)


# ------------------------------------------ 3. every deploy approved by its own what-if digest


def test_a_later_deploy_stops_until_its_own_what_if_is_approved_then_resumes(out):
    assert harness("plan", "azure", out).code == 0
    d1 = json.loads((out / "state.json").read_text())["digest"]
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", d1)
    assert r.code == 3, r.stdout[-1500:]
    st = r.state
    assert st["paused_at"] == 18 and 19 not in st["guide_done"] and 16 in st["guide_done"]
    d2 = st["pending_gate"]["digest"]
    assert d2 != d1
    assert f"--approve {d1} --approve {d2}" in r.stdout
    assert (out / "evidence" / "whatif-step-18.txt").exists()
    deploys = [c for c in r.transcript if c.startswith("az deployment group create")]
    assert len(deploys) == 1  # phase 1 only: phase 2 waits for its own approval
    n = len(r.transcript)
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", d1, "--approve", d2)
    assert r.code == 0, r.stdout[-1500:]
    later = r.transcript[n:]
    assert len([c for c in later if c.startswith("az deployment group create")]) == 1
    assert "resuming at guide step 18" in r.stdout
    assert "paused_at" not in r.state and r.state["approved_gates"]["18"] == d2
    harness("down", "azure", out, "--run-id", RUN)


def test_a_wrong_second_digest_does_not_deploy(out):
    assert harness("plan", "azure", out).code == 0
    d1 = json.loads((out / "state.json").read_text())["digest"]
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", d1, "--approve", d1)
    assert r.code == 3
    assert len([c for c in r.transcript if c.startswith("az deployment group create")]) == 1
    harness("down", "azure", out, "--run-id", RUN)


def test_a_guide_deploy_without_its_own_gate_is_refused(out, tmp_path):
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / "guide"
    shutil.copytree(FIXTURE_GUIDE, g)
    f = g / "README.md"
    f.write_text(
        f.read_text().replace(
            "guide-step=18 on=workstation gate=whatif", "guide-step=18 on=workstation"
        )
    )
    r = harness("plan", "azure", out, guide=g)
    assert r.code == 4, r.stdout[-1500:]
    assert "19 deploy" in r.stdout and "its own what-if" in r.stdout


# ------------------------------------------- 6. the goldens hold omnifile.env's values


def test_the_transcript_and_golden_hold_omnifile_env_values_not_sizes(out):
    from conftest import GOLDEN, normalize

    from harness.vm import EDGE_SUBNET_PREFIX

    r = harness("all", "azure", out)
    assert r.code == 0, r.stdout[-1500:]
    t = r.transcript
    want_env = f"OMNI_USER_CIDRS={EDGE_SUBNET_PREFIX}"
    want_file = f"env-file OMNI_USER_CIDRS='{EDGE_SUBNET_PREFIX}'"
    # what the deploy's parameters read, and what the edge's omnifile.env holds
    assert any(ln.startswith("env ") and want_env in ln.split() for ln in t)
    assert any(want_file in ln for ln in t)
    golden = (GOLDEN / "azure.transcript").read_text()
    assert want_file in golden and want_env in golden
    # a widened range is a different transcript: the golden comparison fails on it
    widened = [ln.replace(EDGE_SUBNET_PREFIX, "0.0.0.0/1,::") for ln in t]
    assert normalize(widened) != golden
    # and no secret's value is in either: names that look secret are redacted
    assert not [ln for ln in t if "PASSWORD=" in ln and "<secret>" not in ln]


# ------------------------- third pass: a deploy written any other way needs its own gate too

DEPLOY_19 = 'deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" --deploy --approve "$OMNI_WHATIF_DIGEST"'


@pytest.mark.parametrize(
    "deploy",
    [
        'deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" \\\n  --deploy --approve "$OMNI_WHATIF_DIGEST"',
        'az deployment group create -g "$RG" -n omnifile --template-file t.json -o none',
        "az deployment sub create -l swedencentral --template-file t.json -o none",
    ],
    ids=["continued-line", "group-create", "sub-create"],
)
def test_any_deploy_without_a_gate_of_its_own_is_refused(out, tmp_path, deploy):
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / "guide"
    shutil.copytree(FIXTURE_GUIDE, g)
    f = g / "README.md"
    text = f.read_text()
    assert text.count(DEPLOY_19) == 2  # steps 5 and 19
    head, tail = text.rsplit(DEPLOY_19, 1)
    text = head + deploy + tail
    text = text.replace("guide-step=18 on=workstation gate=whatif", "guide-step=18 on=workstation")
    f.write_text(text)
    r = harness("plan", "azure", out, guide=g)
    assert r.code == 4, r.stdout[-1500:]
    assert "19 deploy" in r.stdout


def test_the_customer_guides_deploys_all_have_their_own_gate():
    """WP7's guide on #33, when it is on this branch: every deploy has its own what-if."""
    from harness import guide
    from harness.azure import Azure

    g = REPO / "docs" / "guide"
    if not (g / "README.md").exists():
        pytest.skip("docs/guide is not on this branch yet")
    steps = [s for s in guide.parse(g) if s.cloud == "azure"]
    assert Azure._ungated_deploys(steps, [s for s in steps if s.gate]) == []


# ------------------------------------------ third pass: a dry run reads nothing of the owner's


def test_a_dry_runs_home_and_caches_are_its_own_with_the_schema_seeded(tmp_path, monkeypatch):
    from datetime import datetime

    from harness.core import TEAMS_SCHEMA, Context

    owner = tmp_path / "owner-home"
    owner.mkdir()
    monkeypatch.setenv("HOME", str(owner))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.build_env()
    dry = tmp_path / "o" / "dry"
    assert ctx.env["HOME"] == str(dry / "home")
    assert ctx.env["XDG_CACHE_HOME"] == str(dry / "cache")
    seeded = dry / "cache" / "omnifile" / "teams-schema" / "1.1.3" / "MicrosoftTeams.schema.json"
    assert seeded.read_bytes() == TEAMS_SCHEMA.read_bytes()
    assert not any(owner.iterdir())


def test_the_pinned_teams_schema_fixture_is_the_one_build_sh_pins():
    import hashlib

    from harness.core import TEAMS_SCHEMA, TEAMS_SCHEMA_VERSION

    build = (REPO / "deploy" / "teams" / "build.sh").read_text()
    assert f'PKG_VERSION="{TEAMS_SCHEMA_VERSION}"' in build
    assert "teams/v1.17/MicrosoftTeams.schema.json" in build
    # what build.sh extracted from the sha512-checked package (2026-11-03)
    assert hashlib.sha256(TEAMS_SCHEMA.read_bytes()).hexdigest() == (
        "a94d5b9f3125a7804fd96b9efecc5b923ecfc266dd5a7be1d92fe2544ae01426"
    )


def test_the_azure_dry_run_passes_on_an_empty_home_and_writes_nothing_there(out, tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    r = harness("all", "azure", out, env={"HOME": str(home), "XDG_CACHE_HOME": ""})
    assert r.code == 0, r.stdout[-2000:]
    assert "--schema" in (out / "guide" / "step-7.sh").read_text()
    assert not any(home.iterdir()), sorted(str(p) for p in home.rglob("*"))[:10]


# ------------------- the Bicep CLI's own crashes: retried once in a dry run, errors are not


@pytest.mark.parametrize("first, want", [(134, 0), (1, 1)], ids=["crash", "error"])
def test_a_crashing_bicep_is_run_once_more_and_an_error_is_not(tmp_path, monkeypatch, first, want):
    import subprocess
    from datetime import datetime

    from harness.core import Context

    bindir = tmp_path / "realbin"
    bindir.mkdir()
    count = tmp_path / "calls"
    fake = bindir / "bicep"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo x >>"{count}"\n'
        f'[ "$(wc -l <"{count}")" -eq 1 ] && exit {first}\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.build_env()
    p = subprocess.run(["bicep", "build-params", "x"], env=ctx.env, capture_output=True, text=True)
    assert p.returncode == want, p.stderr
    assert len(count.read_text().splitlines()) == (2 if first >= 128 else 1)
