"""The dry run of WP7's whole customer guide (docs/guide), the fake az's models of what it
calls, and the dry run's offline Bicep: each fix with a test that fails without it."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime

import pytest
from conftest import REAL, REPO, clean_env, harness

RUN = "rr-dry-0001"
FAKECLI = REAL / "fakes" / "fakecli.py"


# ------------------------------------------------------------------- offline Bicep


def test_a_dry_runs_bicep_goes_through_a_dead_proxy(tmp_path, monkeypatch):
    from harness.core import DEAD_PROXY, Context

    bindir = tmp_path / "realbin"
    bindir.mkdir()
    fake = bindir / "bicep"
    fake.write_text("#!/bin/sh\nenv | grep -i -E '^(https?|no)_proxy=' | sort\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        monkeypatch.setenv(k, "http://owner-proxy.example:3128" if "PROXY" in k.upper() else "")
    monkeypatch.setenv("NO_PROXY", "*")
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
    got = dict(ln.split("=", 1) for ln in p.stdout.splitlines())
    assert got["HTTPS_PROXY"] == got["HTTP_PROXY"] == DEAD_PROXY == "http://127.0.0.1:9"
    assert got["https_proxy"] == got["http_proxy"] == DEAD_PROXY
    assert got["NO_PROXY"] == got["no_proxy"] == ""


# ------------------------------------------------------------- the whole guide, dry


@pytest.fixture(scope="module")
def guide_run(tmp_path_factory):
    """`run.sh all --dry-run --cloud azure` with no --guide: docs/guide, WP7's, end to end."""
    out = tmp_path_factory.mktemp("guide") / "out"
    r = harness("all", "azure", out, guide=None)
    return r


def test_the_whole_customer_guide_runs_through_in_a_dry_run(guide_run):
    from harness import guide

    r = guide_run
    assert r.code == 0, r.stdout[-2500:]
    steps = [s for s in guide.parse(REPO / "docs" / "guide") if s.cloud == "azure"]
    done = set(r.state["guide_done"])
    assert {s.n for s in steps} <= done, sorted({s.n for s in steps} - done)
    assert not [ln for ln in r.transcript if "no model of" in ln]
    assert r.teardown["runs"][RUN]["residue"] == []


def test_the_guides_network_steps_leave_what_azure_would(guide_run):
    g = guide_run.out / "guide"
    assert "all private" in (g / "step-23.log").read_text()
    assert "pe-str04a37bflive-blob: 10.60.0.132" in (g / "step-19.log").read_text()
    assert '"state": "Connected"' in (g / "step-20.log").read_text()
    assert "now has ArchiveAdmin" in (g / "step-30.log").read_text()
    assert "relay.pem: OK" in (g / "step-36.log").read_text()
    assert "removed from you" in (g / "step-43.log").read_text()
    # the steps that would reach a database or a server reached the fakes
    t = "\n".join(guide_run.transcript)
    assert "psql host=" in t and "uv run --project service" in t
    assert "openssl s_client -connect r04a37bf-enrol.internal.example:443" in t


# ---------------------------------------------- the models refuse what Azure refuses


def fake(tool: str, *args: str, state, env=None):
    e = clean_env(
        {
            "REAL_FAKE_PY": sys.executable,
            "REAL_FAKE_STATE": str(state),
            "REAL_FAKE_TRANSCRIPT": str(state.parent / "t.txt"),
            "REAL_FAKE_REPO": str(REPO),
            "REAL_NOW": "2026-11-03T00:00:00Z",
            **(env or {}),
        }
    )
    return subprocess.run(
        [sys.executable, str(FAKECLI), tool, *args], env=e, capture_output=True, text=True
    )


@pytest.fixture
def deployed(tmp_path):
    """A fake subscription with the run's group, its deployment and the hub VNet."""
    st = tmp_path / "state.json"
    rg = "rg-ofreal-x"

    def az(*a, **k):
        return fake("az", *a, state=st, **k)

    assert az("group", "create", "-n", rg, "-l", "swedencentral").returncode == 0
    env = {
        "OMNI_TEST_RUN_ID": RUN,
        "OMNI_TEST_RUN_EXPIRES": "2026-11-03T12:00Z",
        "OMNIFILE_TEST_PREFIX": "rtest01",
    }
    assert az("deployment", "group", "create", "-g", rg, "-n", "omnifile", env=env).returncode == 0
    assert az("network", "vnet", "create", "-g", rg, "-n", "vnet-ofreal-hub").returncode == 0
    return az, rg, json.loads(st.read_text())


def test_a_link_to_a_zone_that_does_not_exist_is_refused(deployed):
    az, rg, _ = deployed
    hub = f"/subscriptions/00000000-0000-4000-8000-0000000000a1/resourceGroups/{rg}/providers/Microsoft.Network/virtualNetworks/vnet-ofreal-hub"
    p = az(
        "network",
        "private-dns",
        "link",
        "vnet",
        "create",
        "-g",
        rg,
        "--zone-name",
        "privatelink.blob.core.windows.net",
        "-n",
        "l1",
        "--virtual-network",
        hub,
    )
    assert p.returncode == 3 and "ParentResourceNotFound" in p.stderr
    assert (
        az(
            "network",
            "private-dns",
            "zone",
            "create",
            "-g",
            rg,
            "-n",
            "privatelink.blob.core.windows.net",
        ).returncode
        == 0
    )
    p = az(
        "network",
        "private-dns",
        "link",
        "vnet",
        "create",
        "-g",
        rg,
        "--zone-name",
        "privatelink.blob.core.windows.net",
        "-n",
        "l1",
        "--virtual-network",
        hub + "-nope",
    )
    assert p.returncode == 1 and "InvalidVirtualNetwork" in p.stderr


def test_a_private_endpoint_needs_its_target_and_the_right_group(deployed):
    az, rg, s = deployed
    acct = next(r["id"] for r in s["resources"] if r["name"] == "strtest01live")
    subnet = next(r["id"] for r in s["resources"] if r["name"] == "rtest01-vnet") + "/subnets/pe"
    base = ["network", "private-endpoint", "create", "-g", rg, "-n", "pe1", "--subnet", subnet]
    p = az(*base, "--private-connection-resource-id", acct, "--group-id", "vault")
    assert p.returncode == 1 and "InvalidGroupId" in p.stderr
    p = az(*base, "--private-connection-resource-id", acct + "x", "--group-id", "blob")
    assert p.returncode == 1 and "PrivateLinkServiceNotFound" in p.stderr
    assert az(*base, "--private-connection-resource-id", acct, "--group-id", "blob").returncode == 0
    p = az("network", "private-endpoint", "show", "-g", rg, "-n", "pe2")
    assert p.returncode == 3


def test_a_name_resolves_privately_only_once_endpoint_zone_group_and_link_exist(deployed, tmp_path):
    az, rg, s = deployed
    st = tmp_path / "state.json"

    def dns(*names):
        return fake("dns", *names, state=st)

    name = "strtest01live.blob.core.windows.net"
    assert dns(name).returncode == 1  # nothing made yet: it doesn't resolve
    acct = next(r["id"] for r in s["resources"] if r["name"] == "strtest01live")
    subnet = next(r["id"] for r in s["resources"] if r["name"] == "rtest01-vnet") + "/subnets/pe"
    hub = next(r["id"] for r in s["resources"] if r["name"] == "vnet-ofreal-hub")
    zone = "privatelink.blob.core.windows.net"
    az(
        "network",
        "private-endpoint",
        "create",
        "-g",
        rg,
        "-n",
        "pe1",
        "--subnet",
        subnet,
        "--private-connection-resource-id",
        acct,
        "--group-id",
        "blob",
    )
    zid = json.loads(az("network", "private-dns", "zone", "create", "-g", rg, "-n", zone).stdout)[
        "id"
    ]
    assert dns(name).returncode == 1  # no zone group yet
    az(
        "network",
        "private-endpoint",
        "dns-zone-group",
        "create",
        "-g",
        rg,
        "--endpoint-name",
        "pe1",
        "-n",
        "default",
        "--private-dns-zone",
        zid,
        "--zone-name",
        "blob",
    )
    assert dns(name).returncode == 1  # the zone isn't linked to the hub
    az(
        "network",
        "private-dns",
        "link",
        "vnet",
        "create",
        "-g",
        rg,
        "--zone-name",
        zone,
        "-n",
        "l1",
        "--virtual-network",
        hub,
    )
    p = dns(name)
    assert p.returncode == 0 and "10.60.0.132" in p.stdout and "all private" in p.stdout


def test_a_peering_is_initiated_until_the_other_side_exists(deployed):
    az, rg, s = deployed
    spoke = next(r["id"] for r in s["resources"] if r["name"] == "rtest01-vnet")
    hub = next(r["id"] for r in s["resources"] if r["name"] == "vnet-ofreal-hub")
    assert (
        az(
            "network",
            "vnet",
            "peering",
            "create",
            "-g",
            rg,
            "--vnet-name",
            "rtest01-vnet",
            "-n",
            "to-hub",
            "--remote-vnet",
            hub,
            "--allow-vnet-access",
        ).returncode
        == 0
    )
    one = az("network", "vnet", "peering", "list", "-g", rg, "--vnet-name", "rtest01-vnet")
    assert json.loads(one.stdout)[0]["peeringState"] == "Initiated"
    az(
        "network",
        "vnet",
        "peering",
        "create",
        "-g",
        rg,
        "--vnet-name",
        "vnet-ofreal-hub",
        "-n",
        "to-spoke",
        "--remote-vnet",
        spoke,
    )
    two = az("network", "vnet", "peering", "list", "-g", rg, "--vnet-name", "rtest01-vnet")
    assert json.loads(two.stdout)[0]["peeringState"] == "Connected"
    p = az(
        "network",
        "vnet",
        "peering",
        "create",
        "-g",
        rg,
        "--vnet-name",
        "rtest01-vnet",
        "-n",
        "x",
        "--remote-vnet",
        hub,
        "--use-remote-gateways",
        "true",
    )
    assert p.returncode == 1 and "GatewayTransit" in p.stderr


def test_unknown_groups_images_identities_and_secrets_are_not_found(deployed):
    az, rg, _ = deployed
    assert az("ad", "group", "show", "--group", "Nobody-Has-This").returncode == 3
    assert az("ad", "group", "show", "--group", "OmniFile-ArchiveAdmins").returncode == 0  # synced
    assert az("identity", "show", "-g", rg, "-n", "id-rtest01-nope").returncode == 3
    assert az("identity", "show", "-g", rg, "-n", "id-rtest01-worker").returncode == 0
    p = az("acr", "repository", "show", "--name", "ofrealx", "--image", "omnifile-web@sha256:00")
    assert p.returncode == 1 and "MANIFEST_UNKNOWN" in p.stderr
    p = az("keyvault", "secret", "show", "--vault-name", "kv-rtest01", "--name", "api-lease-key")
    assert p.returncode == 3 and "SecretNotFound" in p.stderr


def test_psql_curl_and_openssl_reach_only_the_fakes_deployment(deployed, tmp_path):
    st = tmp_path / "state.json"
    good = "host=pg-rtest01.postgres.database.azure.com dbname=postgres user=x"
    p = fake("psql", good, "-c", "select 1", state=st)
    assert p.returncode == 2 and "password authentication failed" in p.stderr  # no token
    p = fake("psql", good, "-c", "select 1", state=st, env={"PGPASSWORD": "fake-token"})
    assert p.returncode == 0
    p = fake("psql", "host=db.example.org", state=st, env={"PGPASSWORD": "fake-token"})
    assert p.returncode == 2 and "could not translate host name" in p.stderr
    p = fake("curl", "--fail", "https://example.org/healthz", state=st)
    assert p.returncode == 6
    p = fake("openssl", "s_client", "-connect", "example.org:443", state=st)
    assert p.returncode == 1 and "getaddrinfo" in p.stdout


def test_openssl_s_client_and_the_services_db_commands_go_to_the_fakes(tmp_path):
    """The real openssl and uv run, except s_client and the guide's catalog commands."""
    from harness.core import Context

    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.build_env()
    env = {**ctx.env, "REAL_FAKE_STATE": str(tmp_path / "fs.json")}
    p = subprocess.run(
        ["openssl", "s_client", "-connect", "nothing.invalid:443"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert p.returncode == 1 and "connect: getaddrinfo: nothing.invalid" in p.stdout
    v = subprocess.run(["openssl", "version"], env=env, capture_output=True, text=True).stdout
    assert re.match(r"^OpenSSL ([3-9]|[1-9][0-9]+)\.", v), v  # 3 or later, not LibreSSL
    p = subprocess.run(
        ["uv", "run", "--project", "service", "python", "-m", "omnifile_service.catalog.migrate"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1 and "could not translate host name" in p.stderr
    t = (tmp_path / "o" / "transcript.txt").read_text()
    assert "openssl s_client -connect nothing.invalid:443" in t
    assert "uv run --project service python -m omnifile_service.catalog.migrate" in t
    for tool in ("psql", "curl"):
        assert (
            subprocess.run(
                ["sh", "-c", f"command -v {tool}"], env=env, capture_output=True, text=True
            )
            .stdout.strip()
            .startswith(str(REAL / "fakes" / "bin"))
        )


@pytest.mark.parametrize(
    "crashes, want, runs", [(4, 0, 5), (9, 134, 5)], ids=["four times", "always"]
)
def test_bicep_crashing_in_a_row_is_run_up_to_five_times(
    tmp_path, monkeypatch, crashes, want, runs
):
    from harness.core import Context

    bindir = tmp_path / "realbin"
    bindir.mkdir()
    count = tmp_path / "calls"
    fake = bindir / "bicep"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo x >>"{count}"\n'
        f'[ "$(wc -l <"{count}")" -le {crashes} ] && exit 134\n'
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
    assert len(count.read_text().splitlines()) == runs


# ------------------------------- a gate that hides its digest can't let a deploy through


def _mutated_guide(tmp_path, *, marker: bool):
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / "guide"
    shutil.copytree(FIXTURE_GUIDE, g)
    f = g / "README.md"
    text = f.read_text()
    old = 'deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" | tee whatif-phase2.txt'
    assert text.count(old) == 1
    # The review's mutation: the what-if goes to a file, so the gate prints nothing, but it
    # still records the fresh digest in omnifile.state.env for the deploy after it.
    new = 'deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" > whatif-phase2.txt'
    if marker:
        new += '\necho "nothing to preview: section 18 skipped"'
    f.write_text(text.replace(old, new))
    return g


@pytest.mark.parametrize("marker", [False, True], ids=["silent", "fake-skip-marker"])
def test_a_gate_that_hides_its_digest_deploys_nothing(tmp_path, marker):
    g = _mutated_guide(tmp_path, marker=marker)
    out = tmp_path / "out"
    p = harness("plan", "azure", out, guide=g)
    assert p.code == 0, p.stdout[-1500:]
    d1 = p.state["digest"]
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", d1, guide=g)
    assert r.code != 0, r.stdout[-1500:]
    assert "printed no `digest:` line" in r.stdout
    deploys = [ln for ln in r.transcript if ln.startswith("az deployment group create")]
    assert len(deploys) == 1, deploys  # phase 1 only: phase 2 never deployed
    assert 19 not in r.state["guide_done"]
    harness("down", "azure", out, "--run-id", RUN, guide=g)


def test_the_deploy_check_refuses_a_digest_nobody_approved(tmp_path):
    from harness.azure import Azure
    from harness.core import AwaitingApproval, Context

    ctx = Context(
        phase="up",
        cloud="azure",
        dry_run=False,
        out=tmp_path,
        run_id=RUN,
        now=datetime(2026, 11, 3),
        approvals=["sha256:" + "11" * 32],
    )
    ctx.state = {"digest": "sha256:" + "11" * 32, "approved_gates": {"18": "sha256:" + "22" * 32}}
    az = Azure(ctx, None)  # type: ignore[arg-type]

    class S:
        n = 19

    az._require_approved(S, "sha256:" + "11" * 32)
    az._require_approved(S, "sha256:" + "22" * 32)
    with pytest.raises(AwaitingApproval, match="no one approved"):
        az._require_approved(S, "sha256:" + "33" * 32)
    with pytest.raises(AwaitingApproval):
        az._require_approved(S, None)


# ----------------------------------------------- default deny, and goldens free of the path


@pytest.mark.parametrize(
    "cmd",
    [
        ["nslookup", "example.org"],
        ["dig", "example.org"],
        ["host", "example.org"],
        ["wget", "https://example.org/"],
        ["nc", "-z", "example.org", "443"],
        ["ssh-keyscan", "example.org"],
        ["git", "ls-remote", "https://example.org/x.git"],
        ["git", "clone", "https://example.org/x.git"],
        ["openssl", "s_time", "-connect", "example.org:443"],
        ["openssl", "ocsp", "-url", "http://example.org/"],
    ],
    ids=lambda c: " ".join(c[:2]),
)
def test_a_dry_runs_network_tools_refuse(tmp_path, cmd):
    from harness.core import Context

    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.build_env()
    env = {**ctx.env, "REAL_FAKE_STATE": str(tmp_path / "fs.json")}
    p = subprocess.run(
        cmd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
    )
    assert p.returncode == 2 and "refused" in p.stderr, (p.returncode, p.stdout, p.stderr)
    assert "refused" in (tmp_path / "o" / "transcript.txt").read_text()
    # and every proxy-honouring client is pointed at the dead proxy, local servers excepted
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://127.0.0.1:9"
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"


def test_git_still_works_locally_in_a_dry_run(tmp_path):
    from harness.core import Context

    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.build_env()
    p = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], env=ctx.env, capture_output=True, text=True
    )
    assert p.returncode == 0 and len(p.stdout.strip()) == 40


def test_psql_and_curl_refuse_what_they_do_not_model(deployed, tmp_path):
    st = tmp_path / "state.json"
    good = "host=pg-rtest01.postgres.database.azure.com dbname=postgres user=x"
    tok = {"PGPASSWORD": "fake-token"}
    sql = tmp_path / "drop-everything.sql"
    sql.write_text("DROP DATABASE omnifile;\n")
    p = fake("psql", good, "-f", str(sql), state=st, env=tok)
    assert p.returncode == 3 and "models no SQL file" in p.stderr
    az, rg, _ = deployed
    hub = f"/subscriptions/00000000-0000-4000-8000-0000000000a1/resourceGroups/{rg}/providers/Microsoft.Network/virtualNetworks/vnet-ofreal-hub"
    az("network", "private-dns", "zone", "create", "-g", rg, "-n", "internal.example")
    az(
        "network",
        "private-dns",
        "link",
        "vnet",
        "create",
        "-g",
        rg,
        "--zone-name",
        "internal.example",
        "-n",
        "l",
        "--virtual-network",
        hub,
    )
    url = "https://rtest01-web.internal.example"
    assert fake("curl", "--fail", url + "/healthz", state=st).returncode == 0
    p = fake("curl", "--fail", url + "/v1/admin/secrets", state=st)
    assert p.returncode == 22 and "404" in p.stderr
    p = fake("curl", "--write-out", "%{http_code}", url + "/nope", state=st)
    assert p.stdout == "404"


def test_the_golden_records_env_files_by_value_not_size():
    from conftest import GOLDEN

    for line in (GOLDEN / "azure.transcript").read_text().splitlines():
        if line.startswith("scp ") and ".env" in line.split()[1]:
            assert "bytes" not in line, line


# ------------------ the re-review's variants (g57-1 to g57-3): only a fresh, approved digest


PHASE2_GATE = (
    'deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" | tee whatif-phase2.txt\n'
    "echo \"export OMNI_WHATIF_DIGEST='$(sed -n 's/^digest: //p' whatif-phase2.txt | tail -1)'\""
    " >> omnifile.state.env\n"
)
PHASE2_DEPLOY = (
    'deploy/azure/whatif.sh -g "$RG" -p "$PARAMS" --deploy --approve "$OMNI_WHATIF_DIGEST"\n```\n'
)
VARIANTS = {
    # g57-1: the what-if goes to a file; the gate prints nothing, still records the digest
    "g57-1": (PHASE2_GATE, PHASE2_GATE.replace("| tee whatif-phase2.txt", "> whatif-phase2.txt")),
    # g57-2: the same, with a skip marker
    "g57-2": (
        PHASE2_GATE,
        PHASE2_GATE.replace("| tee whatif-phase2.txt", "> whatif-phase2.txt")
        + 'echo "no values: section 9 skipped"\n',
    ),
}


def variant_guide(tmp_path, name: str):
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / name
    shutil.copytree(FIXTURE_GUIDE, g)
    f = g / "README.md"
    text = f.read_text()
    if name == "g57-3":
        # a skip marker and nothing recorded, then a direct deploy that binds no digest
        assert text.count(PHASE2_GATE) == 1 and text.endswith(PHASE2_DEPLOY)
        text = text.replace(PHASE2_GATE, 'echo "no values: section 9 skipped"\n')
        text = text[: -len(PHASE2_DEPLOY)] + (
            'az deployment group create -g "$RG" -n omnifile -f deploy/azure/main.bicep '
            '-p "$PARAMS" -o none\n```\n'
        )
    elif name == "g57-4":
        # a skip marker and nothing recorded, then the usual whatif.sh deploy: it may only use
        # its own gate's digest, and that gate previewed nothing, so it doesn't run
        text = text.replace(PHASE2_GATE, 'echo "no values: section 9 skipped"\n')
    else:
        old, new = VARIANTS[name]
        assert text.count(old) == 1
        text = text.replace(old, new)
    f.write_text(text)
    return g


@pytest.mark.parametrize("name", ["g57-1", "g57-2"])
def test_a_silent_gate_never_lets_its_deploy_through(tmp_path, name):
    g = variant_guide(tmp_path, name)
    out = tmp_path / "out"
    p = harness("plan", "azure", out, guide=g)
    assert p.code == 0, p.stdout[-1500:]
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", p.state["digest"], guide=g)
    assert r.code != 0, r.stdout[-1500:]
    assert len([ln for ln in r.transcript if ln.startswith("az deployment group create")]) == 1
    assert 19 not in r.state["guide_done"]
    harness("down", "azure", out, "--run-id", RUN, guide=g)


def test_a_direct_deploy_is_refused_at_plan_and_before_up_runs_it(tmp_path):
    g = variant_guide(tmp_path, "g57-3")
    out = tmp_path / "out"
    p = harness("plan", "azure", out, guide=g)
    assert p.code == 4 and "deploy directly" in p.stdout, p.stdout[-1500:]
    assert not [ln for ln in p.transcript if "deployment group create" in ln]
    # planned with the honest fixture, then the guide changes before up: up refuses too
    from conftest import FIXTURE_GUIDE

    out2 = tmp_path / "out2"
    p = harness("plan", "azure", out2, guide=FIXTURE_GUIDE)
    assert p.code == 0
    r = harness("up", "azure", out2, "--run-id", RUN, "--approve", p.state["digest"], guide=g)
    assert r.code == 4 and "deploy directly" in r.stdout, r.stdout[-1500:]
    assert not [ln for ln in r.transcript if ln.startswith("az deployment group create")]
    harness("down", "azure", out2, "--run-id", RUN, guide=g)


def test_the_run_time_check_refuses_a_direct_deploy_step(tmp_path):
    """The step check in up, on its own (as a guide edited after up started would meet it)."""
    from harness.azure import DIRECT_DEPLOYS, joined

    for body in (
        "az deployment group create -g rg -f main.bicep",
        "az deployment sub \\\n  create -l x -f main.bicep",
        "az deployment create -l x -f main.bicep",
        "az group deployment create -g rg --template-file t.json",
        "az stack group create -g rg -n s -f main.bicep",
        "az stack sub set -n s -l x -f main.bicep",
        "New-AzResourceGroupDeployment -ResourceGroupName rg -TemplateFile t.json",
    ):
        assert DIRECT_DEPLOYS.search(joined(body)), body
    for body in (
        "deploy/azure/whatif.sh -g rg -p p --deploy --approve sha256:x",
        "az deployment group show -g rg -n omnifile",
        "az deployment group what-if -g rg -f main.bicep",
    ):
        assert not DIRECT_DEPLOYS.search(joined(body)), body


def test_a_deploy_whose_own_gate_previewed_nothing_does_not_run(tmp_path):
    g = variant_guide(tmp_path, "g57-4")
    out = tmp_path / "out"
    p = harness("plan", "azure", out, guide=g)
    assert p.code == 0, p.stdout[-1500:]
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", p.state["digest"], guide=g)
    assert r.code == 0, r.stdout[-1500:]
    assert "guide step 19 not run: its gate, step 18, previewed no deploy" in r.stdout
    assert len([ln for ln in r.transcript if ln.startswith("az deployment group create")]) == 1
    harness("down", "azure", out, "--run-id", RUN, guide=g)


def test_a_carried_over_digest_is_not_its_gates_own(tmp_path):
    from harness.azure import Azure
    from harness.core import AwaitingApproval, Context

    d1, d2 = "sha256:" + "11" * 32, "sha256:" + "22" * 32
    ctx = Context(
        phase="up",
        cloud="azure",
        dry_run=False,
        out=tmp_path,
        run_id=RUN,
        now=datetime(2026, 11, 3),
        approvals=[d1, d2],
    )
    ctx.state = {"digest": d1, "approved_gates": {"18": d2}}
    az = Azure(ctx, None)  # type: ignore[arg-type]

    class S:
        n = 19

    az._require_approved(S, d2, fresh=d2, gate_n=18)
    with pytest.raises(AwaitingApproval, match="its own gate"):
        az._require_approved(S, d1, fresh=d2, gate_n=18)  # phase 1's, approved but not fresh
    with pytest.raises(AwaitingApproval, match="its own gate"):
        az._require_approved(S, d1, fresh=None, gate_n=18)


# --------------------------------------------- the az guard: a deploy only through whatif.sh


EVASIONS = {
    "quoted": 'az deployment group "create" -g rg -n x -f t.json',
    "variable": "d=deployment; az $d group create -g rg -n x -f t.json",
    "eval": 'eval "az deployment sub create -l x -f t.json"',
    "env": "env az deployment group create -g rg -n x -f t.json",
    "bash -c": "bash -c 'az stack group create -g rg -n s -f t.json'",
    "sh -c, mg": "sh -c 'az deployment mg create -m m -l x -f t.json'",
    "tenant": "az deployment tenant create -l x -f t.json",
    "legacy": "az deployment create -l x -f t.json",
    "older": "az group deployment create -g rg --template-file t.json",
    "stack set": "az stack sub set -n s -l x -f t.json",
    "rest put": 'az rest --method PUT --url "https://management.azure.com/subscriptions/s/resourceGroups/'
    'rg/providers/Microsoft.Resources/deployments/x?api-version=2024-03-01" --body @b.json',
    "rest patch stack": "az rest -m patch --uri https://management.azure.com/subscriptions/s/"
    "providers/Microsoft.Resources/deploymentStacks/s",
    "rest post, = form": "az rest --method=post --url=https://management.azure.com/x/deployments/y/validate",
    "rest, %-escaped": "az rest --method put --url https://management.azure.com/s/providers/"
    "Microsoft.Resources/%64eployments/x --body @b.json",
    "rest, escaped slash": "az rest --method put --url https://management.azure.com/s/providers/"
    "Microsoft.Resources%2Fdeployments%2Fx --body @b.json",
    "rest, escaped twice": "az rest -m PUT -u https://management.azure.com/s/providers/"
    "Microsoft.Resources/%2564eployments/x",
    "rest, body and no method": "az rest --url https://management.azure.com/s/providers/"
    "Microsoft.Resources/deployments/x --body @b.json",
    "resource create": "az resource create --id /subscriptions/s/resourceGroups/rg/providers/"
    "Microsoft.Resources/deployments/x --properties {}",
    "resource create by type": "az resource create -g rg -n x --namespace Microsoft.Resources "
    "--resource-type deployments --properties @p.json",
    "resource create, full type": "az resource create -g rg -n x "
    "--resource-type Microsoft.Resources/deployments --properties @p.json",
    "resource create, stack type": "az resource create -g rg -n x --namespace "
    "Microsoft.Resources --resource-type=deploymentStacks --properties @p.json",
    "resource, %-escaped id": "az resource create --id /subscriptions/s/resourceGroups/rg/providers/"
    "Microsoft.Resources/%64eployments/x --properties {}",
    "self-declared digest": "OMNIFILE_DEPLOY_APPROVED_DIGEST=sha256:00 az deployment group create -g rg",
    "option first": "az --only-show-errors deployment group create -g rg -f t.json",
}
PASSES = {
    "group show": "az group show -n rg",
    "what-if": "az deployment group what-if -g rg -f t.json",
    "deployment show": "az deployment group show -g rg -n omnifile",
    "rest get": "az rest --method get --url https://management.azure.com/x/deployments/y",
    "graph post": "az rest --method POST --uri https://graph.microsoft.com/v1.0/servicePrincipals/s/appRoleAssignedTo",
    "resource create, other type": "az resource create -g rg -n x --namespace Microsoft.Web "
    "--resource-type sites --properties {}",
}


def guarded(tmp_path, body: str, extra=None):
    """BODY run with the run's az guard first on PATH, as harness/guiderun.py sets it up:
    the wrapper carries the real az, the grants file and the log; the environment only the
    step's grant, if any."""
    from harness.azguard import write_wrapper

    real = tmp_path / "real-az"
    calls = tmp_path / "calls"
    real.write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\n')
    real.chmod(0o755)
    write_wrapper(
        tmp_path / "guard",
        python=sys.executable,
        real=str(real),
        grants=tmp_path / "grants.json",
        log=tmp_path / "guard.log",
    )
    env = {"PATH": f"{tmp_path / 'guard'}:/usr/bin:/bin", **(extra or {})}
    p = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", body],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    got = calls.read_text().splitlines() if calls.exists() else []
    return p, got


@pytest.mark.parametrize("name", list(EVASIONS))
def test_the_guard_refuses_every_way_round_it(tmp_path, name):
    p, calls = guarded(tmp_path, EVASIONS[name])
    assert p.returncode != 0, p.stdout
    assert "az guard: refused" in p.stderr
    assert calls == []  # the real az never saw it
    assert "refused" in (tmp_path / "guard.log").read_text()


@pytest.mark.parametrize("name", list(PASSES))
def test_the_guard_passes_everything_else_to_the_real_az(tmp_path, name):
    p, calls = guarded(tmp_path, PASSES[name])
    assert p.returncode == 0, p.stderr
    assert len(calls) == 1


def _deploy_files(tmp_path):
    """A compiled template and parameters, and their hashes as whatif.sh prints them."""
    from harness.azguard import file_sha

    t, pm = tmp_path / "t.json", tmp_path / "p.json"
    t.write_text(json.dumps({"resources": [], "$schema": "x"}))
    pm.write_text(json.dumps({"parameters": {"runId": {"value": "rr-1"}}}))
    return t, pm, {"template": file_sha(str(t)), "parameters": file_sha(str(pm))}


D = "sha256:" + "ab" * 32
CREATE = "az deployment group create --resource-group rg -n omnifile -f t.json --parameters @p.json"


def _grants(tmp_path, files, **kw):
    g = {"step": 15, "digest": D, "rg": "rg", "used": False, **files, **kw}
    (tmp_path / "grants.json").write_text(json.dumps({"tok": g}))


GRANT = {"OMNIFILE_DEPLOY_GRANT": "tok", "OMNIFILE_DEPLOY_APPROVED_DIGEST": D}


def test_a_grant_lets_whatif_sh_deploy_once_for_its_digest_and_group(tmp_path):
    _, _, files = _deploy_files(tmp_path)
    _grants(tmp_path, files)
    for wrong in (
        {**GRANT, "OMNIFILE_DEPLOY_APPROVED_DIGEST": "sha256:" + "cd" * 32},  # another digest
        {**GRANT, "OMNIFILE_DEPLOY_GRANT": "other"},  # another grant
        {"OMNIFILE_DEPLOY_GRANT": "tok"},  # whatif.sh didn't check anything
    ):
        p, calls = guarded(tmp_path, CREATE, wrong)
        assert p.returncode != 0 and calls == [], wrong
    p, calls = guarded(tmp_path, CREATE.replace("rg -n", "other-rg -n"), GRANT)
    assert p.returncode != 0 and calls == []  # another group
    p, calls = guarded(tmp_path, CREATE, GRANT)
    assert p.returncode == 0 and len(calls) == 1, p.stderr
    p, calls = guarded(tmp_path, CREATE, GRANT)
    assert p.returncode != 0 and "spent" in p.stderr  # one shot
    # a grant never covers anything but whatif.sh's group create
    p, _ = guarded(tmp_path, "az deployment sub create -l x -f t.json", GRANT)
    assert p.returncode != 0


def test_a_grant_covers_only_the_template_and_parameters_its_gate_previewed(tmp_path):
    t, pm, files = _deploy_files(tmp_path)
    _grants(tmp_path, files)
    other = tmp_path / "evil.json"
    other.write_text(json.dumps({"resources": [{"type": "Microsoft.Compute/virtualMachines"}]}))
    for body in (
        CREATE.replace("-f t.json", "-f evil.json"),  # another template
        CREATE.replace("@p.json", "@evil.json"),  # other parameters
        CREATE + " --parameters adminUser=evil",  # a parameter added on top
        CREATE.replace("@p.json", "@p.json namePrefix=evil"),  # inline, after the file
        CREATE.replace("@p.json", "@p.json @evil.json"),  # a second file, after the first
        CREATE.replace("--parameters @p.json", "-p @p.json namePrefix=evil"),
        # az's abbreviations, and any option whatif.sh doesn't pass
        CREATE + " --param namePrefix=evil",
        CREATE.replace("--parameters @p.json", "-p @p.json --parameter namePrefix=evil"),
        CREATE.replace("--parameters @p.json", "--param @p.json"),
        CREATE.replace("--resource-group rg", "--resource-gr rg"),
        CREATE + " --mode Complete",
        CREATE + " --rollback-on-error",
        CREATE + " --no-wait",
        CREATE + " -n other",  # a second name
        CREATE + " --parameters=@p.json",
        "az --debug " + CREATE[3:],  # a global option before the command
        CREATE.replace("-f t.json", "--template-uri https://example.test/t.json"),
        CREATE + " --template-spec /subscriptions/s/templateSpecs/x",
    ):
        p, calls = guarded(tmp_path, body, GRANT)
        assert p.returncode != 0 and calls == [], body
    # a grant written without the files (an older harness) binds nothing, so it lets nothing
    _grants(tmp_path, {})
    p, calls = guarded(tmp_path, CREATE, GRANT)
    assert p.returncode != 0 and calls == []
    _grants(tmp_path, files)
    p, calls = guarded(tmp_path, CREATE, GRANT)
    assert p.returncode == 0 and len(calls) == 1, p.stderr


def test_whatif_sh_s_own_create_is_what_a_grant_accepts(tmp_path):
    """The create line exactly as deploy/azure/whatif.sh writes it, short or long options and
    --output, is the one shape a grant lets through."""
    import re

    line = (REPO / "deploy" / "azure" / "whatif.sh").read_text()
    m = re.search(r"az deployment group create (.*?)\\\n\s*(.*?)\\\n\s*(.*?)>", line)
    assert m, "whatif.sh's create line moved"
    used = re.findall(r"(--?[a-z-]+)", " ".join(m.groups()))
    from harness.azguard import GRANTED_OPTIONS

    assert set(used) <= set(GRANTED_OPTIONS), used
    _, _, files = _deploy_files(tmp_path)
    _grants(tmp_path, files)
    long = "az deployment group create --resource-group rg --name omnifile --template-file t.json "
    long += "--parameters @p.json --output json"
    p, calls = guarded(tmp_path, long, GRANT)
    assert p.returncode == 0 and len(calls) == 1, p.stderr


def test_the_guards_paths_come_from_its_wrapper_never_the_environment(tmp_path):
    """The review's forgery: a grants file of the step's own, named in the environment, with
    a token it chose. The guard reads only the run's grants file, which has no such token."""
    _, _, files = _deploy_files(tmp_path)
    _grants(tmp_path, files, used=True)  # the run's own grant is spent
    forged = tmp_path / "mine.json"
    forged.write_text(
        json.dumps({"tok": {"step": 1, "digest": D, "rg": "rg", "used": False, **files}})
    )
    decoy = tmp_path / "decoy-az"
    decoy.write_text(f'#!/bin/sh\necho decoy >>"{tmp_path / "decoy-calls"}"\n')
    decoy.chmod(0o755)
    env = {
        **GRANT,
        "OMNIFILE_DEPLOY_GRANTS": str(forged),
        "OMNIFILE_REAL_AZ": str(decoy),
        "OMNIFILE_AZ_GUARD_LOG": str(tmp_path / "elsewhere.log"),
    }
    p, calls = guarded(tmp_path, CREATE, env)
    assert p.returncode != 0 and calls == [] and "spent" in p.stderr
    p, calls = guarded(tmp_path, "az group show -n rg", env)
    assert p.returncode == 0 and len(calls) == 1  # the real az, not the decoy
    assert not (tmp_path / "decoy-calls").exists() and not (tmp_path / "elsewhere.log").exists()
    # and the guard can't be run without the wrapper's arguments
    q = subprocess.run(
        [sys.executable, str(REAL / "harness" / "azguard.py"), "group", "show"],
        env={**env, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert q.returncode == 2 and "wrapper" in q.stderr


def test_no_step_is_handed_the_real_az(tmp_path):
    from datetime import datetime

    from harness.core import Context
    from harness.guiderun import guard_env

    ctx = Context(
        phase="up",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=datetime(2026, 11, 3),
    )
    ctx.out.mkdir(parents=True)
    real = tmp_path / "bin" / "az"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    ctx.state["real_az"] = str(real)
    env = guard_env(ctx, {"OMNIFILE_DEPLOY_GRANT": "t"})
    assert not [k for k, v in env.items() if str(real) in v], env
    assert set(env) == {"PATH", "OMNIFILE_DEPLOY_GRANT", "OMNIFILE_DEPLOY_APPROVED_DIGEST"}
    assert env["PATH"].split(os.pathsep)[0] == str(ctx.out / "guard")
    assert str(real) in (ctx.out / "guard" / "az").read_text()  # the wrapper's own


def test_a_grant_is_spent_under_an_exclusive_lock(tmp_path):
    """While another guard holds the grants file's lock, a second waits; and it then reads
    the grant as the first left it (spent), not as it was before."""
    import fcntl
    import time

    _, _, files = _deploy_files(tmp_path)
    _grants(tmp_path, files)
    lock = os.open(tmp_path / "grants.json.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    from harness.azguard import write_wrapper

    real = tmp_path / "real-az"
    calls = tmp_path / "calls"
    real.write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\n')
    real.chmod(0o755)
    w = write_wrapper(
        tmp_path / "guard",
        python=sys.executable,
        real=str(real),
        grants=tmp_path / "grants.json",
        log=tmp_path / "guard.log",
    )
    proc = subprocess.Popen(
        [str(w), *CREATE.split()[1:]],
        env={"PATH": "/usr/bin:/bin", **GRANT},
        cwd=tmp_path,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.0)
    assert proc.poll() is None and not calls.exists()  # waiting for the lock
    g = json.loads((tmp_path / "grants.json").read_text())
    g["tok"]["used"] = True  # the holder spends it
    (tmp_path / "grants.json").write_text(json.dumps(g))
    fcntl.flock(lock, fcntl.LOCK_UN)
    os.close(lock)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode != 0 and "spent" in err and not calls.exists()


def test_a_guide_step_that_deploys_round_the_text_check_is_stopped_by_the_guard(tmp_path):
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / "guide"
    shutil.copytree(FIXTURE_GUIDE, g)
    f = g / "README.md"
    text = f.read_text()
    anchor = (
        "deploy/images/build.sh"
        if "deploy/images/build.sh" in text
        else "lock=deploy/images/images.lock.json"
    )
    assert anchor in text
    # the review's evasion, in an ordinary step: the plan-time text check doesn't see it
    f.write_text(
        text.replace(
            anchor, 'd=deployment; az $d group create -g "$RG" -n evil -f x.json\n' + anchor, 1
        )
    )
    out = tmp_path / "out"
    p = harness("plan", "azure", out, guide=g)
    assert p.code == 0, p.stdout[-1500:]
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", p.state["digest"], guide=g)
    assert r.code != 0
    assert "az guard: refused" in (out / "guide" / "step-6.log").read_text()
    assert not [ln for ln in r.transcript if " -n evil " in ln]
    harness("down", "azure", out, "--run-id", RUN, guide=g)
