"""The guide-step format (WP7's): parsing, its errors, placeholders and omnifile.env."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURE_GUIDE, REPO

from harness import guide
from harness.core import UsageError


def write(tmp_path: Path, text: str) -> Path:
    d = tmp_path / "g"
    d.mkdir(exist_ok=True)
    (d / "README.md").write_text(text)
    return d


def test_the_fixture_has_every_kind_of_step():
    steps = guide.parse(FIXTURE_GUIDE)
    assert [s.n for s in steps] == list(range(1, 20))
    assert {s.on for s in steps} == {
        "workstation",
        "kvm",
        "hyperv",
        "portal",
        "edge-console",
        "edge-shell",
        "linux-client",
    }
    by = {s.n: s for s in steps}
    assert by[8].manual and by[17].cloud == "aws" and by[14].write_to == "/etc/omnifile/extra.toml"
    assert ("value", "ENROL_CODE") in by[12].placeholders()
    assert ("secret", "AD_JOIN_PASSWORD") in by[13].placeholders()
    assert [s.n for s in steps if s.gate] == [4, 18]
    assert [s.n for s in steps if s.plan_step] == [1, 3, 4, 18]  # a gate is read-only too


def test_a_block_without_guide_step_is_prose_not_a_step(tmp_path):
    d = write(
        tmp_path, "```bash\necho no\n```\n```bash guide-step=1 on=workstation\necho yes\n```\n"
    )
    [s] = guide.parse(d)
    assert s.body == "echo yes\n"


def test_wp7s_attributes(tmp_path):
    d = write(
        tmp_path,
        "```bash guide-step=59 on=linux-client wait=90\nstat x\n```\n"
        "```bash guide-step=60 on=workstation input=OMNI_ENROL_CODE\nread c\n```\n"
        "```powershell guide-step=61 on=ad-admin\nNew-ADGroup x\n```\n"
        "```text guide-step=62 on=browser manual=yes\nopen admin centre\n```\n",
    )
    a, b, c, e = guide.parse(d)
    assert a.wait_minutes == 90 and b.input_name == "OMNI_ENROL_CODE"
    assert c.on in guide.OWNER_ONLY and e.manual


@pytest.mark.parametrize(
    "text,err",
    [
        (
            "```bash guide-step=2 on=workstation\na\n```\n```bash guide-step=1 on=workstation\nb\n```\n",
            "numbers go up",
        ),
        ("```bash guide-step=x on=workstation\na\n```\n", "integer"),
        ("```bash guide-step=1 on=mainframe\na\n```\n", "on= must be one of"),
        ("```bash guide-step=1 on=portal\nopen /\n```\n", "can't be bash"),
        ("```text guide-step=1 on=portal\nwiggle /files\n```\n", "portal action"),
        ("```bash guide-step=1 on=workstation colour=red\na\n```\n", "unknown keys"),
        ("```bash guide-step=1 on=workstation wait=soon\na\n```\n", "minutes"),
        ("```bash guide-step=1 on=workstation\na\n", "never closed"),
        ("```bash guide-step=1 on=workstation file=/x\na\n```\n", "file= is for"),
        ("```bash guide-step=1 on=workstation cloud=gcp\na\n```\n", "cloud= must be"),
        ("```bash guide-step=1 on=workstation gate=deploy\na\n```\n", "gate= is whatif"),
        ("```bash guide-step=1 on=kvm plan=yes\na\n```\n", "runs on the workstation"),
    ],
)
def test_a_malformed_guide_is_refused(tmp_path, text, err):
    with pytest.raises(UsageError, match=err):
        guide.parse(write(tmp_path, text))


def test_duplicate_numbers_across_files_are_refused(tmp_path):
    d = write(tmp_path, "```bash guide-step=1 on=workstation\na\n```\n")
    (d / "b.md").write_text("```bash guide-step=1 on=workstation\nb\n```\n")
    with pytest.raises(UsageError, match="guide-step=1 at"):
        guide.parse(d)


def test_placeholders_are_filled_and_unknown_ones_refused():
    assert guide.fill("<value:A>\n<secret:B>\n<enter>\n", {"A": "a"}, {"B": "b"}) == "a\nb\n\n"
    with pytest.raises(UsageError, match="doesn't know"):
        guide.fill("<value:NOPE>", {}, {})


def test_a_portal_expect_with_an_unknown_value_is_left_to_capture():
    body = "fill Name = <value:A>\nexpect Enrolment code <value:CODE>\n"
    assert (
        guide.fill_known(body, {"A": "edge"}, {})
        == "fill Name = edge\nexpect Enrolment code <value:CODE>\n"
    )
    with pytest.raises(UsageError):
        guide.fill_known("click <value:CODE>\n", {}, {})


def test_omnifile_env_needs_every_name_and_keeps_export(tmp_path):
    ex = tmp_path / "omnifile.env.example"
    ex.write_text("# x\nexport OMNI_RG=''\nOMNI_LOCATION=\n# input: OMNI_AD_JOIN_PASSWORD\n")
    dest = tmp_path / "omnifile.env"
    with pytest.raises(UsageError, match="OMNI_LOCATION"):
        guide.write_env(ex, dest, {"OMNI_RG": "rg"})
    guide.write_env(ex, dest, {"OMNI_RG": "rg", "OMNI_LOCATION": "it's"})
    text = dest.read_text()
    assert "export OMNI_RG='rg'" in text and "OMNI_AD_JOIN_PASSWORD" not in text
    assert "export OMNI_LOCATION='it'\"'\"'s'" in text
    guide.write_env(ex, dest, {"OMNI_RG": "rg"}, partial=True)
    assert "OMNI_LOCATION" not in dest.read_text()


def test_the_azure_run_has_a_value_for_every_name_wp7s_guide_defines():
    """WP7's names (docs/guide/omnifile.env.example on their branch, sent 2026-11-03); once
    the guide is on main the real example is checked too."""
    from harness import guidevalues

    wp7 = (
        "OMNI_SUBSCRIPTION_ID OMNI_TENANT_ID OMNI_LOCATION OMNI_RG OMNI_PREFIX OMNI_RELEASE "
        "OMNI_SOURCE_REGISTRY OMNI_ACR OMNI_PG_ADMIN_GROUP OMNI_EDGE_CIDRS OMNI_USER_CIDRS "
        "OMNI_ADMIN_CIDRS OMNI_VNET_PREFIX OMNI_SUBNET_CAE OMNI_SUBNET_RELAY OMNI_SUBNET_PG "
        "OMNI_SUBNET_PE OMNI_TEST_HUB_NO_GATEWAY OMNI_HUB_VNET_ID OMNI_DNS_ZONE_RG OMNI_DNS_RESOLVER_IP "
        "OMNI_NOTIFY_SENDER OMNI_ALERT_ACTION_GROUP_ID OMNI_AD_DOMAIN OMNI_AD_WORKGROUP "
        "OMNI_AD_JOIN_USER OMNI_AD_JOIN_OU OMNI_AD_ADMIN_GROUP OMNI_AD_ADMIN_USER "
        "OMNI_GROUP_ARCHIVEADMIN OMNI_GROUP_DATASTEWARD OMNI_GROUP_USER OMNI_FIRST_UID "
        "OMNI_FIRST_GID OMNI_DOMAIN_USERS_GID OMNI_EDGE_NAME OMNI_EDGE_HOSTNAME OMNI_EDGE_ADDRESS OMNI_EDGE_GATEWAY "
        "OMNI_EDGE_DNS OMNI_EDGE_RESTART_WINDOW OMNI_DATA_DISK_GB OMNI_KVM_BRIDGE OMNI_SHARE "
        "OMNI_SHARE_OWNERS OMNI_SHARE_WRITERS OMNI_SHARE_READERS OMNI_NFS_CLIENTS "
        "OMNI_TEST_USER OMNI_TEST_UID OMNI_OTHER_USER OMNI_CLIENT_MOUNT OMNI_AWS_ACCOUNT_ID "
        "OMNI_AWS_REGION OMNI_AWS_VPC_ID OMNI_AWS_SUBNET_IDS OMNI_AWS_SG_ID "
        "OMNI_AWS_SOURCE_BUCKETS OMNI_AWS_OPS_BUCKET OMNI_AWS_NAME_SUFFIX "
        "OMNI_API_CLIENT_APP_ID OMNI_API_CLIENT_NAME OMNI_API_CLIENT_SCOPES OMNI_API_CLIENT_OWNER"
    ).split()
    st = {
        "identity": {"subscription": "s", "tenant": "t"},
        "location": "l",
        "rg": "rg",
        "run_id": "rr-dry-0001",
    }
    got = guidevalues.omni(st, "rabcde", guidevalues.aws_values())
    assert set(wp7) == set(got)
    real = REPO / "docs/guide/omnifile.env.example"
    if real.exists():
        # The real example's names: the run's values, and its test-only ones (OMNI_TEST_*).
        from types import SimpleNamespace

        run = SimpleNamespace(run_id="rr-dry-0001", expires="2026-11-03T12:00:00Z")
        names = set(got) | set(guidevalues.test_values(run))
        missing = set(guide.env_names(real)) - names
        assert not missing, sorted(missing)


def test_wp7s_later_hosts_and_recorded_values(tmp_path):
    d = write(
        tmp_path,
        "```bash guide-step=46 on=edge-network\ncurl <value:OMNI_API_URL>\n```\n"
        "```powershell guide-step=29 on=exchange-admin\nNew-X\n```\n".replace(
            "guide-step=29", "guide-step=47"
        ),
    )
    a, b = guide.parse(d)
    assert a.on == "edge-network" and b.on in guide.OWNER_ONLY
    st = tmp_path / "omnifile.state.env"
    st.write_text("# recorded\nexport OMNI_API_URL='https://x-api:8443'\nOMNI_N=3 # c\n")
    vals = guide.state_values(st)
    assert vals == {"OMNI_API_URL": "https://x-api:8443", "OMNI_N": "3"}
    assert guide.fill(a.body, vals, {}) == "curl https://x-api:8443\n"
    assert guide.state_values(tmp_path / "absent") == {}
