"""Tests of the customer guide's tools (docs/guide/tools, docs/guide/aws).

    python3 -m unittest discover -s docs/guide/lint -p 'test_*.py'

Each tool runs as the guide runs it, in a scratch folder standing in for the release folder, with
`az` replaced by fake_az.py where it would call Azure. Nothing reaches Azure or AWS. The AWS settings
the guide composes are loaded with the service's own parser, so a shape the service would refuse fails
here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = HERE.parents[2]
TOOLS = REPO / "docs/guide/tools"
AWS = REPO / "docs/guide/aws"


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNI_", "AZURE_", "AWS_", "GH_"))}
    env.update(extra)
    return env


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, stdin: str | None = None):
    return subprocess.run(cmd, cwd=cwd, env=env or clean_env(), input=stdin, capture_output=True, text=True)


def example_values() -> dict[str, str]:
    import shlex

    out = {}
    for line in (REPO / "docs/guide/omnifile.env.example").read_text().splitlines():
        if line.startswith("export OMNI_"):
            name, _, value = line[len("export "):].partition("=")
            out[name] = shlex.split(value)[0] if value.strip() else ""
    return out


def load_service_module(dotted: str) -> types.ModuleType:
    """Import one stdlib-only module of the service without its package's heavy __init__ files."""
    src = REPO / "service/src"
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [str(src.joinpath(*parts[:i]))]
            sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(dotted, src.joinpath(*parts).with_suffix(".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "az.log"
        self.log.touch()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fake_az(self, answers: dict[str, dict], *, names: tuple[str, ...] = ("az",), **extra: str) -> dict[str, str]:
        for name in names:
            cli = self.bin / name
            cli.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{HERE / 'fake_az.py'}' \"$@\"\n")
            cli.chmod(0o755)
        path = f"{self.bin}:{os.environ['PATH']}"
        if extra.pop("openssl3", None) and openssl3():
            path = f"{os.path.dirname(openssl3())}:{path}"
        return clean_env(PATH=path, FAKE_AZ_LOG=str(self.log), FAKE_AZ_ANSWERS=json.dumps(answers), **extra)

    def az_calls(self) -> list[list[str]]:
        return [json.loads(x) for x in self.log.read_text().splitlines()]


class StateTest(Scratch):
    def test_a_value_reads_back_exactly_and_a_second_set_replaces_it(self) -> None:
        pem = "-----BEGIN CERTIFICATE-----\nAB'C\"$D\n-----END CERTIFICATE-----"
        self.assertEqual(run([str(TOOLS / "state.sh"), "set", "OMNI_X", "first"], self.dir).returncode, 0)
        (self.dir / "x.pem").write_text(pem)
        self.assertEqual(run([str(TOOLS / "state.sh"), "set-file", "OMNI_X", "x.pem"], self.dir).returncode, 0)
        got = run(["bash", "-c", ". ./omnifile.state.env && printf %s \"$OMNI_X\""], self.dir)
        self.assertEqual(got.stdout, pem)
        self.assertEqual((self.dir / "omnifile.state.env").read_text().count("export OMNI_X="), 1)
        self.assertEqual(run([str(TOOLS / "state.sh"), "get", "OMNI_X"], self.dir).stdout.rstrip("\n"), pem)

    def test_a_name_outside_the_guide_is_refused(self) -> None:
        r = run([str(TOOLS / "state.sh"), "set", "PATH", "x"], self.dir)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a name the guide uses", r.stderr)


class CheckEnvTest(Scratch):
    def write(self, **changes: str) -> None:
        values = example_values()
        values.update(changes)
        (self.dir / "omnifile.env").write_text(
            "".join(f"export {k}={json.dumps(v)}\n" for k, v in values.items()))

    def test_the_example_values_pass(self) -> None:
        self.write()
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("every value is set", r.stdout)

    def test_bad_values_are_each_named(self) -> None:
        self.write(OMNI_PREFIX="OMNI", OMNI_USER_CIDRS="10.60.0.0/16", OMNI_EDGE_HOSTNAME="much-too-long-a-name",
                   OMNI_AWS_REGION="eu-west-1", OMNI_TEST_UID="500", OMNI_EDGE_CIDRS="10.20.0.0/16, 10.21.0.0/16")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 1)
        for words in ("OMNI_PREFIX: 2 to 8 lowercase", "OMNI_USER_CIDRS: overlaps the relay subnet",
                      "OMNI_EDGE_HOSTNAME: at most 15", "OMNI_AWS_: set all the S3 values",
                      "OMNI_TEST_UID: must be inside 10000-999999", "' 10.21.0.0/16' is not a CIDR"):
            self.assertIn(words, r.stdout)

    def test_a_group_name_with_a_space_passes_and_one_with_a_quote_does_not(self) -> None:
        self.write(OMNI_PG_ADMIN_GROUP="OmniFile DB Admins")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.write(OMNI_PG_ADMIN_GROUP="O'Brien admins")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_PG_ADMIN_GROUP: a group name without quotes", r.stdout)

    def test_the_hub_override_is_for_tests_and_says_so(self) -> None:
        self.write(OMNI_TEST_HUB_NO_GATEWAY="yes")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_TEST_HUB_NO_GATEWAY: empty, or 1", r.stdout)
        self.write(OMNI_TEST_HUB_NO_GATEWAY="1")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("for test runs only", r.stdout)

    def test_test_run_values_pass_only_in_their_test_shape(self) -> None:
        import guide_lint

        self.write(**guide_lint.TEST_RUN)
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("this is a test run, never a client deployment", r.stdout)
        for bad, words in ((dict(OMNI_TEST_PG_SKU_TIER="GeneralPurpose"), "OMNI_TEST_PG_SKU_TIER: Burstable"),
                           (dict(OMNI_TEST_KV_PURGE_PROTECTION="true"), "OMNI_TEST_KV_PURGE_PROTECTION: false"),
                           (dict(OMNI_TEST_RUN_EXPIRES=""), "OMNI_TEST_RUN_EXPIRES: a test run needs its expiry")):
            self.write(**{**guide_lint.TEST_RUN, **bad})
            r = run([str(TOOLS / "check-env.sh")], self.dir)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn(words, r.stdout)

    def test_an_api_clients_values_go_together_and_have_their_shape(self) -> None:
        good = dict(OMNI_API_CLIENT_APP_ID="55555555-5555-5555-5555-555555555555", OMNI_API_CLIENT_NAME="Migration tool",
                    OMNI_API_CLIENT_SCOPES="Archive.Read,Archive.Adopt", OMNI_API_CLIENT_OWNER="ops@contoso.example")
        self.write(**good)
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        for bad, words in ((dict(OMNI_API_CLIENT_OWNER=""), "OMNI_API_CLIENT_: set all four"),
                           (dict(OMNI_API_CLIENT_SCOPES="Archive.Read, Archive.Adopt"), "OMNI_API_CLIENT_SCOPES: scopes from"),
                           (dict(OMNI_API_CLIENT_SCOPES="Archive.Write"), "OMNI_API_CLIENT_SCOPES: scopes from"),
                           (dict(OMNI_API_CLIENT_APP_ID="my-app"), "OMNI_API_CLIENT_APP_ID: an application (client) id"),
                           (dict(OMNI_API_CLIENT_OWNER="ops"), "OMNI_API_CLIENT_OWNER: a sign-in name")):
            self.write(**{**good, **bad})
            r = run([str(TOOLS / "check-env.sh")], self.dir)
            self.assertEqual(r.returncode, 1, (bad, r.stdout))
            self.assertIn(words, r.stdout)

    def test_the_portal_roles_go_to_groups_or_to_named_people(self) -> None:
        # people: the role groups may be empty, and at least one ArchiveAdmin is named.
        people = dict(OMNI_ROLE_HOLDERS="people", OMNI_GROUP_ARCHIVEADMIN="", OMNI_GROUP_DATASTEWARD="",
                      OMNI_GROUP_USER="", OMNI_PEOPLE_ARCHIVEADMIN="ann@contoso.example bo@contoso.example",
                      OMNI_PEOPLE_DATASTEWARD="")
        self.write(**people)
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        for bad, words in ((dict(OMNI_PEOPLE_ARCHIVEADMIN=" "), "OMNI_PEOPLE_ARCHIVEADMIN: name at least one"),
                           (dict(OMNI_PEOPLE_DATASTEWARD="cy"), "OMNI_PEOPLE_DATASTEWARD: 'cy' is not a sign-in name"),
                           (dict(OMNI_ROLE_HOLDERS="everyone"), "OMNI_ROLE_HOLDERS: groups or people")):
            self.write(**{**people, **bad})
            r = run([str(TOOLS / "check-env.sh")], self.dir)
            self.assertEqual(r.returncode, 1, (bad, r.stdout))
            self.assertIn(words, r.stdout)
        # Empty means groups.
        self.write(OMNI_ROLE_HOLDERS="")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        # groups: the role groups are needed, and named people aren't.
        self.write(OMNI_ROLE_HOLDERS="groups", OMNI_GROUP_USER="")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_GROUP_USER: empty", r.stdout)

    def test_the_aws_name_suffix_is_empty_or_a_run_id(self) -> None:
        self.write(OMNI_AWS_NAME_SUFFIX="run-20261103-a1")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("OMNI_AWS_NAME_SUFFIX", r.stdout)   # named in the test-run note
        self.assertIn("this is a test run, never a client deployment", r.stdout)
        for bad in ("Run_1", "abc", "-run-1", "x" * 41):
            self.write(OMNI_AWS_NAME_SUFFIX=bad)
            r = run([str(TOOLS / "check-env.sh")], self.dir)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("OMNI_AWS_NAME_SUFFIX: empty, or a run id", r.stdout)

    def test_the_admin_network_may_not_cover_the_relay(self) -> None:
        self.write(OMNI_ADMIN_CIDRS="10.60.0.0/24")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_ADMIN_CIDRS: overlaps the relay subnet", r.stdout)

    def test_a_network_overlapping_the_relay_in_any_way_fails(self) -> None:
        # main.bicep fails on any overlap, so a range inside the relay subnet, a single address in it, and the whole
        # internet all fail here too; a neighbouring range does not.
        for name, value in (("OMNI_USER_CIDRS", "10.30.0.0/16,10.60.0.72/29"), ("OMNI_ADMIN_CIDRS", "10.60.0.70/32"),
                            ("OMNI_USER_CIDRS", "0.0.0.0/0"), ("OMNI_EDGE_CIDRS", "0.0.0.0/0")):
            self.write(**{name: value})
            r = run([str(TOOLS / "check-env.sh")], self.dir)
            self.assertEqual(r.returncode, 1, (name, value, r.stdout))
            self.assertIn(f"{name}: overlaps the relay subnet", r.stdout)
        self.write(OMNI_USER_CIDRS="10.60.1.0/24", OMNI_ADMIN_CIDRS="10.60.0.96/32")
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_missing_value_is_named(self) -> None:
        values = example_values()
        del values["OMNI_RG"]
        (self.dir / "omnifile.env").write_text("".join(f"export {k}={json.dumps(v)}\n" for k, v in values.items()))
        r = run([str(TOOLS / "check-env.sh")], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_RG: missing", r.stdout)


AWS_ENV = {
    "OMNI_AWS_ACCOUNT_ID": "123456789012", "OMNI_AWS_REGION": "eu-west-1",
    "OMNI_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "OMNI_AWS_AUDIENCE": "api://22222222-2222-2222-2222-222222222222",
    "OMNI_API_PRINCIPAL_ID": "33333333-3333-3333-3333-333333333333",
    "OMNI_WORKER_PRINCIPAL_ID": "44444444-4444-4444-4444-444444444444",
    "OMNI_AWS_OPS_BUCKET": "contoso-omnifile-ops", "OMNI_AWS_SOURCE_BUCKETS": "seismic-a,seismic-b",
    "OMNI_AWS_VPCE_S3": "vpce-0s3", "OMNI_AWS_VPCE_S3_DNS": "vpce-0s3-abcd.s3.eu-west-1.vpce.amazonaws.com",
    "OMNI_AWS_VPCE_STS_DNS": "vpce-0sts-abcd.sts.eu-west-1.vpce.amazonaws.com",
    "OMNI_AWS_VPCE_SQS_DNS": "vpce-0sqs-abcd.sqs.eu-west-1.vpce.amazonaws.com",
    "OMNI_AWS_VPCE_ROLESANYWHERE_DNS": "vpce-0ra-abcd.rolesanywhere.eu-west-1.vpce.amazonaws.com",
    "OMNI_AWS_EVENT_QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/123456789012/omnifile-events",
    "OMNI_AWS_TRUST_ANCHOR_ARN":
        "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/0b5f6c2e-1111-4222-8333-944445555666",
}


class RenderTest(Scratch):
    def test_service_and_batch_roles_with_one_entry_per_bucket(self) -> None:
        env = {k: v for k, v in AWS_ENV.items() if k != "OMNI_AWS_TRUST_ANCHOR_ARN"}
        r = run([str(AWS / "render.sh"), "--out", "p"], self.dir, clean_env(**env))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(self.dir / "p")), [
            "batch-role-policy.json", "batch-role-trust.json", "service-role-policy.json", "service-role-trust.json"])
        policy = json.loads((self.dir / "p/service-role-policy.json").read_text())
        read = next(s for s in policy["Statement"] if s["Sid"] == "ReadAndRestoreTheSourceObjects")
        self.assertEqual(read["Resource"], ["arn:aws:s3:::seismic-a/*", "arn:aws:s3:::seismic-b/*"])
        deny = next(s for s in policy["Statement"] if s["Sid"] == "S3OnlyThroughTheInterfaceEndpoint")
        self.assertEqual(deny["Condition"]["StringNotEquals"]["aws:SourceVpce"], "vpce-0s3")
        trust = json.loads((self.dir / "p/service-role-trust.json").read_text())
        cond = trust["Statement"][0]["Condition"]["StringEquals"]
        tenant = AWS_ENV["OMNI_TENANT_ID"]
        self.assertEqual(cond[f"sts.windows.net/{tenant}/:aud"], AWS_ENV["OMNI_AWS_AUDIENCE"])
        self.assertEqual(cond[f"sts.windows.net/{tenant}/:sub"],
                         [AWS_ENV["OMNI_API_PRINCIPAL_ID"], AWS_ENV["OMNI_WORKER_PRINCIPAL_ID"]])

    def test_the_service_may_pass_only_the_batch_role_which_only_batch_operations_assumes(self) -> None:
        # Real AWS, 2026-11-14: CreateJob was denied iam:PassRole while the statement carried an
        # iam:PassedToService condition, and worked without it. The role's trust policy is what keeps it
        # to S3 Batch Operations, for this account's jobs.
        env = {k: v for k, v in AWS_ENV.items() if k != "OMNI_AWS_TRUST_ANCHOR_ARN"}
        r = run([str(AWS / "render.sh"), "--out", "p"], self.dir, clean_env(**env))
        self.assertEqual(r.returncode, 0, r.stderr)
        policy = json.loads((self.dir / "p/service-role-policy.json").read_text())
        passes = [s for s in policy["Statement"] if "iam:PassRole" in json.dumps(s["Action"])]
        self.assertEqual(passes, [{"Sid": "PassOnlyTheBatchRole", "Effect": "Allow", "Action": "iam:PassRole",
                                   "Resource": "arn:aws:iam::123456789012:role/omnifile-batch"}])
        trust = json.loads((self.dir / "p/batch-role-trust.json").read_text())
        self.assertEqual(trust["Statement"], [{
            "Sid": "S3BatchOperationsInThisAccount", "Effect": "Allow",
            "Principal": {"Service": "batchoperations.s3.amazonaws.com"}, "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": "123456789012"}}}])

    def test_with_the_trust_anchor_the_edges_are_named(self) -> None:
        r = run([str(AWS / "render.sh"), "--out", "p", "--edge-ids", "7,9"], self.dir, clean_env(**AWS_ENV))
        self.assertEqual(r.returncode, 0, r.stderr)
        trust = json.loads((self.dir / "p/edge-role-trust.json").read_text())
        cond = trust["Statement"][0]["Condition"]
        self.assertEqual(cond["StringEquals"]["aws:PrincipalTag/x509Subject/CN"],
                         ["omnifile-edge-7", "omnifile-edge-9"])
        self.assertTrue(cond["ArnEquals"]["aws:SourceArn"].endswith("trust-anchor/0b5f6c2e-1111-4222-8333-944445555666"))
        crl = (self.dir / "p/service-crl-policy.json").read_text()
        self.assertIn('"0b5f6c2e-1111-4222-8333-944445555666"', crl)
        edge = json.loads((self.dir / "p/edge-role-policy.json").read_text())
        self.assertEqual(edge["Statement"][0]["Resource"], ["arn:aws:s3:::seismic-a/*", "arn:aws:s3:::seismic-b/*"])
        for f in os.listdir(self.dir / "p"):
            for word in ("ACCOUNT_ID", "SOURCE_BUCKET", "EDGE_ID", "REGION", "INTERFACE_ENDPOINT"):
                self.assertNotIn(word, (self.dir / "p" / f).read_text(), f)

    def test_a_missing_value_writes_nothing(self) -> None:
        env = {k: v for k, v in AWS_ENV.items() if k != "OMNI_AWS_OPS_BUCKET"}
        r = run([str(AWS / "render.sh"), "--out", "p"], self.dir, clean_env(**env))
        self.assertEqual(r.returncode, 1)
        self.assertIn("OPS_BUCKET", r.stderr)
        self.assertFalse((self.dir / "p/service-role-policy.json").exists())


GOLDEN = HERE / "fixtures/aws-render"   # a client's render, as it was before the test-run name suffix


class AwsNameSuffixTest(Scratch):
    """OMNI_AWS_NAME_SUFFIX (test runs only) names section 17's AWS resources after the run; empty, it changes nothing."""

    def render_all(self, out: str, **extra: str) -> dict[str, str]:
        env = clean_env(**AWS_ENV, **extra)
        r = run([str(AWS / "render.sh"), "--out", out, "--edge-ids", "7,9"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = {f: (self.dir / out / f).read_text() for f in os.listdir(self.dir / out)}
        r = run([str(AWS / "accounts-json.sh")], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        files["aws-accounts.json"] = r.stdout
        return files

    def golden(self) -> dict[str, str]:
        return {f.name: f.read_text() for f in GOLDEN.iterdir()}

    def test_a_client_render_is_byte_identical_to_before(self) -> None:
        self.assertEqual(self.render_all("p"), self.golden())
        self.assertEqual(self.render_all("q", OMNI_AWS_NAME_SUFFIX=""), self.golden())

    def test_a_test_run_suffixes_only_the_names(self) -> None:
        got = self.render_all("p", OMNI_AWS_NAME_SUFFIX="run-20261103-a1")
        want = self.golden()
        # The names the service is told, and the ones its policy grants; the queue URL is the one step 74
        # recorded (named there), so it is not rewritten here.
        for f, names in (("aws-accounts.json", ("role/omnifile-service", "role/omnifile-batch")),
                         ("service-role-policy.json", ("role/omnifile-batch", ":omnifile-events"))):
            for name in names:
                self.assertIn(f'{name}"', want[f])
                want[f] = want[f].replace(f'{name}"', f'{name}-run-20261103-a1"')
        self.assertEqual(got, want)
        self.assertIn("role/omnifile-batch-run-20261103-a1", got["service-role-policy.json"])
        self.assertIn(":omnifile-events-run-20261103-a1", got["service-role-policy.json"])
        config = load_service_module("omnifile_service.backends.s3.config")
        dep = config.AwsDeployment.from_mapping(json.loads(got["aws-accounts.json"]),
                                                web_identity_audience=AWS_ENV["OMNI_AWS_AUDIENCE"])
        acct = dep.accounts["123456789012"]
        self.assertEqual(acct.role_arn, "arn:aws:iam::123456789012:role/omnifile-service-run-20261103-a1")
        self.assertEqual(acct.batch.role_arn, "arn:aws:iam::123456789012:role/omnifile-batch-run-20261103-a1")

    def test_render_refuses_a_suffix_of_the_wrong_shape(self) -> None:
        for bad in ("Run_1", "abc", "-run-1", "x" * 41):
            r = run([str(AWS / "render.sh"), "--out", "p"], self.dir, clean_env(**AWS_ENV, OMNI_AWS_NAME_SUFFIX=bad))
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("OMNI_AWS_NAME_SUFFIX must be empty or a run id", r.stderr)
        self.assertFalse((self.dir / "p").exists())


class AwsAccountsTest(Scratch):
    def test_the_service_accepts_the_accounts_json(self) -> None:
        r = run([str(AWS / "accounts-json.sh")], self.dir, clean_env(**AWS_ENV))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        accounts = json.loads(r.stdout)
        config = load_service_module("omnifile_service.backends.s3.config")
        dep = config.AwsDeployment.from_mapping(accounts, web_identity_audience=AWS_ENV["OMNI_AWS_AUDIENCE"])
        acct = dep.accounts["123456789012"]
        self.assertEqual(acct.role_arn, "arn:aws:iam::123456789012:role/omnifile-service")
        self.assertEqual(acct.batch.role_arn, "arn:aws:iam::123456789012:role/omnifile-batch")
        self.assertEqual(acct.endpoints["s3_control"], "https://control." + AWS_ENV["OMNI_AWS_VPCE_S3_DNS"])
        self.assertEqual(acct.roles_anywhere_trust_anchor_arn, AWS_ENV["OMNI_AWS_TRUST_ANCHOR_ARN"])

    def test_a_missing_value_is_named(self) -> None:
        env = {k: v for k, v in AWS_ENV.items() if k != "OMNI_AWS_EVENT_QUEUE_URL"}
        r = run([str(AWS / "accounts-json.sh")], self.dir, clean_env(**env))
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_AWS_EVENT_QUEUE_URL is not set", r.stderr)


class OutputsTest(Scratch):
    def test_every_output_is_recorded(self) -> None:
        outputs = {n: {"value": f"value-of-{n}"} for n in (
            "apiUrl", "enrolUrl", "webUrl", "relayPublicUrl", "internalEnvironmentDefaultDomain",
            "internalEnvironmentStaticIp", "postgresHost", "postgresDatabase", "keyVaultName", "vnetId",
            "privateEndpointSubnetId", "teamsBotAppId")}
        outputs["identityPrincipalIds"] = {"value": {"api": "a", "web": "w", "worker": "k"}}
        outputs["storageAccounts"] = {"value": {"live": "omnilivex", "archive": "omniarchx", "worm": {}}}
        outputs["privateEndpointTargets"] = {"value": [{"resourceId": "/x/omnilivex", "groupId": "blob",
                                                        "privateDnsZone": "privatelink.blob.core.windows.net"}]}
        outputs["requiredPrivateDnsZones"] = {"value": ["privatelink.blob.core.windows.net"]}
        env = self.fake_az({"deployment group show": {"stdout": json.dumps(outputs)}})
        r = run([str(TOOLS / "outputs.sh"), "--resource-group", "rg"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        state = run(["bash", "-c", ". ./omnifile.state.env && echo $OMNI_WEB_URL $OMNI_LIVE_ACCOUNT $OMNI_WORKER_PRINCIPAL_ID"],
                    self.dir)
        self.assertEqual(state.stdout.split(), ["value-of-webUrl", "omnilivex", "k"])
        self.assertEqual(json.loads((self.dir / "pe-targets.json").read_text())[0]["groupId"], "blob")
        self.assertIn(["deployment", "group", "show", "--resource-group", "rg", "--name", "omnifile",
                       "--query", "properties.outputs", "--output", "json"], self.az_calls())

    def test_a_missing_output_fails(self) -> None:
        env = self.fake_az({"deployment group show": {"stdout": "{}"}})
        r = run([str(TOOLS / "outputs.sh"), "--resource-group", "rg"], self.dir, env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no output apiUrl", r.stderr)


#: What the guide accepts (step 6, tools/tls-issuer.sh): OpenSSL 3 or later, not LibreSSL.
OPENSSL_OK = re.compile(r"^OpenSSL ([3-9]|[1-9][0-9]+)\.")


def openssl3() -> str | None:
    """An OpenSSL 3 or later (not LibreSSL, whose s_server can't send a chain), for the test's own
    server."""
    for c in (shutil.which("openssl") or "", "/opt/homebrew/bin/openssl", "/opt/homebrew/opt/openssl@3/bin/openssl",
              "/usr/local/opt/openssl@3/bin/openssl"):
        if c and os.path.exists(c):
            v = subprocess.run([c, "version"], capture_output=True, text=True).stdout
            if OPENSSL_OK.match(v):
                return c
    return None


def make_chain(d: Path) -> None:
    """A CA and a server certificate it issued, for localhost."""
    exe = openssl3() or "openssl"

    def ossl(*a: str) -> None:
        subprocess.run([exe, *a], cwd=d, check=True, capture_output=True)

    ossl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ca.key",
         "-out", "ca.pem", "-subj", "/CN=Test Issuing CA", "-days", "2",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
    ossl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "srv.key",
         "-out", "srv.csr", "-subj", "/CN=localhost")
    (d / "ext").write_text("subjectAltName=DNS:localhost\n")
    ossl("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem",
         "-days", "2", "-extfile", "ext")


class EnrolExecTest(Scratch):
    def test_the_certificate_is_cut_from_terminal_output(self) -> None:
        make_chain(self.dir)
        pem = (self.dir / "ca.pem").read_text().replace("\n", "\r\n")
        env = self.fake_az({
            "containerapp replica list": {"stdout": "omni-enrol--abc-1\n"},
            "containerapp exec": {"stdout": f"\x1b[?2004hINFO: Connecting\r\nwrote /tmp/edge-ca.pem\r\n\x1b[0m{pem}\x1b[?2004l"},
        })
        r = run([str(TOOLS / "enrol-exec.sh"), "--resource-group", "rg", "--app", "omni-enrol",
                 "--command", "python -m omnifile_service.enrol init-kv-ca /tmp/edge-ca.pem",
                 "--then", "cat /tmp/edge-ca.pem", "--pem-out", "edge-ca.pem"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = subprocess.run(["openssl", "x509", "-in", str(self.dir / "edge-ca.pem"), "-noout", "-subject"],
                             capture_output=True, text=True).stdout
        self.assertIn("Test Issuing CA", got)
        pem_lines = (self.dir / "edge-ca.pem").read_text().splitlines()
        self.assertEqual(pem_lines[0], "-----BEGIN CERTIFICATE-----")  # no escape codes, no CRs
        self.assertEqual(pem_lines[-1], "-----END CERTIFICATE-----")
        execs = [c for c in self.az_calls() if c[:2] == ["containerapp", "exec"]]
        self.assertEqual(len(execs), 2)
        for c in execs:  # both commands in the same replica
            self.assertEqual(c[c.index("--replica") + 1], "omni-enrol--abc-1")

    def test_no_certificate_in_the_output_fails_and_writes_nothing(self) -> None:
        env = self.fake_az({"containerapp replica list": {"stdout": "r1\n"},
                            "containerapp exec": {"stdout": "Error: file exists\n"}})
        r = run([str(TOOLS / "enrol-exec.sh"), "--resource-group", "rg", "--app", "a", "--command", "x",
                 "--pem-out", "out.pem"], self.dir, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Copy it by hand", r.stderr)
        self.assertFalse((self.dir / "out.pem").exists())


class OpenSslVersionTest(Scratch):
    """Step 6 and tools/tls-issuer.sh take OpenSSL 3 or later, and refuse LibreSSL and 1.x."""

    VERSIONS = {
        "OpenSSL 3.5.4 30 Sep 2025 (Library: OpenSSL 3.5.4 30 Sep 2025)": True,
        "OpenSSL 4.0.3 29 Sep 2026 (Library: OpenSSL 4.0.3 29 Sep 2026)": True,
        "OpenSSL 10.0.0 1 Jan 2040": True,
        "LibreSSL 3.3.6": False,
        "OpenSSL 1.1.1w  11 Sep 2023": False,
        "OpenSSL 2.0.0": False,
    }

    def fake_openssl(self, version: str) -> dict[str, str]:
        b = self.dir / "bin"
        b.mkdir(exist_ok=True)
        tool = b / "openssl"
        tool.write_text(f"#!/bin/sh\n[ \"$1\" = version ] && {{ echo '{version}'; exit 0; }}\nexit 1\n")
        tool.chmod(0o755)
        return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}"}

    def step6_check(self) -> str:
        body = (REPO / "docs/guide/README.md").read_text().split("guide-step=6 ", 1)[1].split("```", 1)[0]
        line = next(ln for ln in body.splitlines() if ln.startswith("openssl version |"))
        return line

    def test_step_6_takes_openssl_3_or_later_only(self) -> None:
        line = self.step6_check()
        for version, ok in self.VERSIONS.items():
            r = subprocess.run(["bash", "-c", line], env=self.fake_openssl(version), capture_output=True, text=True)
            self.assertEqual(r.returncode == 0, ok, f"{version}: {r.stdout}")
            if not ok:
                self.assertIn("OpenSSL 3 or later is needed", r.stdout)

    def test_tls_issuer_takes_openssl_3_or_later_only(self) -> None:
        for version, ok in self.VERSIONS.items():
            r = run([str(TOOLS / "tls-issuer.sh"), "--host", "enrol.example"], self.dir, self.fake_openssl(version))
            if ok:  # past the version check: the fake s_client then fails to connect
                self.assertEqual(r.returncode, 1, f"{version}: {r.stderr}")
                self.assertNotIn("needs OpenSSL", r.stderr)
            else:
                self.assertEqual(r.returncode, 2, f"{version}: {r.stderr}")
                self.assertIn("needs OpenSSL 3 or later", r.stderr)


@unittest.skipIf(openssl3() is None, "needs OpenSSL 3 for the test's TLS server")
class TlsIssuerTest(Scratch):
    def test_the_pin_is_the_issuing_cas_sha256(self) -> None:
        make_chain(self.dir)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen([openssl3(), "s_server", "-accept", str(port), "-cert", "srv.pem", "-key", "srv.key",
                                   "-cert_chain", "ca.pem", "-quiet"], cwd=self.dir,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.PIPE)
        try:
            for _ in range(50):
                with socket.socket() as c:
                    if c.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.1)
            env = clean_env(PATH=f"{os.path.dirname(openssl3())}:{os.environ['PATH']}")
            tool = str(TOOLS / "tls-issuer.sh")
            r = run([tool, "--host", f"https://localhost:{port}", "--out", "iss.pem", "--ca-file", "ca.pem"],
                    self.dir, env)
            untrusted = run([tool, "--host", "localhost", "--port", str(port), "--out", "bad1.pem"], self.dir, env)
            wrong_name = run([tool, "--host", "127.0.0.1", "--port", str(port), "--out", "bad2.pem",
                              "--ca-file", "ca.pem"], self.dir, env)
        finally:
            server.kill()
            server.wait()
            server.stdin.close()
        self.assertEqual(r.returncode, 0, r.stderr)
        der = subprocess.run(["openssl", "x509", "-in", str(self.dir / "ca.pem"), "-outform", "DER"],
                             capture_output=True, check=True).stdout
        import hashlib

        self.assertEqual(r.stdout.strip().splitlines()[-1], hashlib.sha256(der).hexdigest())
        self.assertIn("Test Issuing CA", r.stdout)
        # A chain this machine doesn't trust, or a certificate for another name: no pin, nothing written.
        for bad, name in ((untrusted, "bad1.pem"), (wrong_name, "bad2.pem")):
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            self.assertIn("did not present a certificate this machine trusts", bad.stderr)
            self.assertEqual(bad.stdout, "")
            self.assertFalse((self.dir / name).exists())


class AppRolesTest(Scratch):
    SKUS = "rest --method GET --uri https://graph.microsoft.com/v1.0/subscribedSkus"
    POST = "rest --method POST"
    EXISTS = {"stdout": 'Bad Request({"error":{"code":"Request_BadRequest","message":'
                        '"Permission being assigned already exists on the object"}})', "rc": 1}

    def posts(self) -> list[dict]:
        return [json.loads(c[c.index("--body") + 1]) for c in self.az_calls() if c[:3] == ["rest", "--method", "POST"]]

    def calls(self) -> list[str]:
        return [" ".join(c) for c in self.az_calls()] if self.log.exists() else []

    def test_roles_are_assigned_once(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles[?value=='ArchiveAdmin']": {"stdout": "role-aa\n"},
            "ad sp show --id app --query appRoles[?value=='User']": {"stdout": "role-u\n"},
            "ad group show --group Admins": {"stdout": "g-admins\n"},
            "ad group show --group Everyone": {"stdout": "g-users\n"},
            self.SKUS: {"stdout": "EXCHANGE_S_STANDARD\nAAD_PREMIUM\n"},
        })
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "ArchiveAdmin=Admins", "User=Everyone"],
                self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.posts()[0], {"principalId": "g-admins", "resourceId": "sp-1", "appRoleId": "role-aa"})
        self.assertEqual(len(self.posts()), 2)
        self.assertIn("Admins now has ArchiveAdmin", r.stdout)
        # The licences are read once, for both groups.
        self.assertEqual(sum(c.startswith(self.SKUS) for c in self.calls()), 1)

    def test_a_group_that_has_its_role_is_left_alone(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles": {"stdout": "role-aa\n"},
            "ad group show": {"stdout": "g\n"},
            self.SKUS: {"stdout": "AAD_PREMIUM_P2\n"},
            self.POST: self.EXISTS,
        })
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "ArchiveAdmin=Admins"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Admins already has ArchiveAdmin", r.stdout)
        self.assertNotIn("now has", r.stdout)

    def test_any_other_refusal_stops_it_with_graphs_words(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles": {"stdout": "role-aa\n"},
            "ad group show": {"stdout": "g\n"},
            self.SKUS: {"stdout": "AAD_PREMIUM\n"},
            self.POST: {"stdout": "Forbidden(Insufficient privileges to complete the operation.)", "rc": 1},
        })
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "ArchiveAdmin=Admins", "User=Everyone"],
                self.dir, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Insufficient privileges", r.stderr)
        self.assertIn("couldn't give Admins the role ArchiveAdmin", r.stderr)
        self.assertEqual(len(self.posts()), 1)   # stopped at the first

    def test_a_group_on_a_tenant_without_p1_stops_before_anything_is_assigned(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles": {"stdout": "role-aa\n"},
            "ad group show": {"stdout": "g\n"},
            self.SKUS: {"stdout": "EXCHANGE_S_STANDARD\n"},
        })
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "ArchiveAdmin=Admins"], self.dir, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs Entra ID P1", r.stderr)
        self.assertIn("OMNI_ROLE_HOLDERS=people", r.stderr)
        self.assertFalse(self.posts())

    def test_unreadable_licences_are_noted_and_the_group_is_tried(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles": {"stdout": "role-aa\n"},
            "ad group show": {"stdout": "g\n"},
            self.SKUS: {"stdout": "", "rc": 1},
        })
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "ArchiveAdmin=Admins"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("couldn't read the tenant's licences", r.stdout)
        self.assertEqual(len(self.posts()), 1)

    def test_people_mode_names_each_person_and_gives_user_to_nobody(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles[?value=='ArchiveAdmin']": {"stdout": "role-aa\n"},
            "ad sp show --id app --query appRoles[?value=='DataSteward']": {"stdout": "role-ds\n"},
            "ad user show --id ann@corp.example": {"stdout": "u-ann\n"},
            "ad user show --id bo@corp.example": {"stdout": "u-bo\n"},
            "ad user show --id cy@corp.example": {"stdout": "u-cy\n"},
            "ad sp show --id app --query appRoleAssignmentRequired": {"stdout": "false\n"},
        }, OMNI_ROLE_HOLDERS="people", OMNI_PEOPLE_ARCHIVEADMIN="ann@corp.example  bo@corp.example",
           OMNI_PEOPLE_DATASTEWARD="cy@corp.example", OMNI_GROUP_USER="")
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "--portal-roles"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.posts(), [
            {"principalId": "u-ann", "resourceId": "sp-1", "appRoleId": "role-aa"},
            {"principalId": "u-bo", "resourceId": "sp-1", "appRoleId": "role-aa"},
            {"principalId": "u-cy", "resourceId": "sp-1", "appRoleId": "role-ds"}])
        self.assertIn("every AD user in the tenant", r.stdout)
        # No group, so no licence check; and nobody is given User.
        self.assertFalse([c for c in self.calls()
                          if c.startswith(self.SKUS) or c.startswith("ad group show") or "'User'" in c])

    def test_people_mode_stops_if_the_app_requires_assignment(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoleAssignmentRequired": {"stdout": "true\n"},
        }, OMNI_ROLE_HOLDERS="people", OMNI_PEOPLE_ARCHIVEADMIN="ann@corp.example")
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "--portal-roles"], self.dir, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn('"Assignment required" on', r.stderr)
        self.assertFalse(self.posts())

    def test_people_mode_without_an_archiveadmin_is_refused(self) -> None:
        env = self.fake_az({}, OMNI_ROLE_HOLDERS="people", OMNI_PEOPLE_ARCHIVEADMIN=" ",
                           OMNI_PEOPLE_DATASTEWARD="cy@corp.example")
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "--portal-roles"], self.dir, env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("at least one OMNI_PEOPLE_ARCHIVEADMIN", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_groups_mode_gives_the_three_groups_their_roles(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles[?value=='ArchiveAdmin']": {"stdout": "role-aa\n"},
            "ad sp show --id app --query appRoles[?value=='DataSteward']": {"stdout": "role-ds\n"},
            "ad sp show --id app --query appRoles[?value=='User']": {"stdout": "role-u\n"},
            "ad group show --group A": {"stdout": "g-a\n"},
            "ad group show --group D": {"stdout": "g-d\n"},
            "ad group show --group U": {"stdout": "g-u\n"},
            self.SKUS: {"stdout": "AAD_PREMIUM\n"},
        }, OMNI_ROLE_HOLDERS="groups", OMNI_GROUP_ARCHIVEADMIN="A", OMNI_GROUP_DATASTEWARD="D",
           OMNI_GROUP_USER="U", OMNI_PEOPLE_ARCHIVEADMIN="")
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "--portal-roles"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(p["principalId"], p["appRoleId"]) for p in self.posts()],
                         [("g-a", "role-aa"), ("g-d", "role-ds"), ("g-u", "role-u")])

    def test_an_empty_mode_means_groups(self) -> None:
        env = self.fake_az({
            "ad sp show --id app --query id": {"stdout": "sp-1\n"},
            "ad sp show --id app --query appRoles": {"stdout": "role\n"},
            "ad group show": {"stdout": "g\n"},
            self.SKUS: {"stdout": "AAD_PREMIUM\n"},
        }, OMNI_ROLE_HOLDERS="", OMNI_GROUP_ARCHIVEADMIN="A", OMNI_GROUP_DATASTEWARD="D", OMNI_GROUP_USER="U")
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "--portal-roles"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.posts()), 3)
        self.assertFalse([c for c in self.calls() if "appRoleAssignmentRequired" in c])

    def test_an_unknown_mode_is_refused(self) -> None:
        env = self.fake_az({}, OMNI_ROLE_HOLDERS="everyone")
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "app", "--portal-roles"], self.dir, env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("must be groups or people", r.stderr)

    def test_an_api_clients_application_gets_its_scopes(self) -> None:
        env = self.fake_az({
            "ad sp show --id web --query id": {"stdout": "sp-web\n"},
            "ad sp show --id web --query appRoles[?value=='Archive.Read']": {"stdout": "role-read\n"},
            "ad sp show --id web --query appRoles[?value=='Archive.Adopt']": {"stdout": "role-adopt\n"},
            "ad sp show --id 55555555-5555-5555-5555-555555555555 --query id": {"stdout": "sp-client\n"},
        })
        client = "app:55555555-5555-5555-5555-555555555555"
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "web", f"Archive.Read={client}",
                 f"Archive.Adopt={client}"], self.dir, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # An application is no group: no group lookup, and no licence check.
        self.assertFalse([c for c in self.calls() if c.startswith("ad group show") or c.startswith(self.SKUS)])
        self.assertEqual(self.posts(), [
            {"principalId": "sp-client", "resourceId": "sp-web", "appRoleId": "role-read"},
            {"principalId": "sp-client", "resourceId": "sp-web", "appRoleId": "role-adopt"}])

    def test_an_unknown_principal_stops_it(self) -> None:
        env = self.fake_az({
            "ad sp show --id web --query id": {"stdout": "sp-web\n"},
            "ad sp show --id web --query appRoles": {"stdout": "role-read\n"},
            "ad sp show --id nobody --query id": {"stdout": ""},
        })
        r = run([str(TOOLS / "assign-app-roles.sh"), "--app-id", "web", "Archive.Read=app:nobody"], self.dir, env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no app:nobody in Entra", r.stderr)
        self.assertFalse(self.posts())


class RunStepTest(Scratch):
    """The first steps of the guide, run as written in a stand-in release folder."""

    def setUp(self) -> None:
        super().setUp()
        (self.dir / "docs").symlink_to(REPO / "docs")

    def test_steps_4_and_5_make_and_check_the_values_file(self) -> None:
        r = run([str(TOOLS / "run-step.sh"), "4"], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.dir / "omnifile.env").exists())
        self.assertTrue((self.dir / "omnifile.state.env").exists())
        r = run([str(TOOLS / "run-step.sh"), "5"], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("every value is set", r.stdout)

    def test_a_failing_command_stops_the_step(self) -> None:
        run([str(TOOLS / "run-step.sh"), "4"], self.dir)
        env = (self.dir / "omnifile.env").read_text().replace("export OMNI_PREFIX='omni'", "export OMNI_PREFIX='NO'")
        (self.dir / "omnifile.env").write_text(env)
        r = run([str(TOOLS / "run-step.sh"), "5"], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMNI_PREFIX", r.stdout)

    def test_a_step_elsewhere_is_shown_not_run(self) -> None:
        import re
        console = re.search(r"guide-step=(\d+) on=edge-console", (REPO / "docs/guide/README.md").read_text()).group(1)
        r = run([str(TOOLS / "run-step.sh"), console], self.dir)
        self.assertEqual(r.returncode, 0)
        self.assertIn("runs on: edge-console", r.stdout)
        self.assertIn("<value:OMNI_EDGE_HOSTNAME>", r.stdout)
        r = run([str(TOOLS / "run-step.sh"), "999"], self.dir)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no step 999", r.stderr)


def libpq_params(conninfo: str) -> dict[str, str]:
    """Parse a libpq keyword/value connection string, as libpq does (values quoted with '...')."""
    out, i, s = {}, 0, conninfo
    while i < len(s):
        while i < len(s) and s[i].isspace():
            i += 1
        if i >= len(s):
            break
        j = s.index("=", i)
        key = s[i:j].strip()
        i = j + 1
        while i < len(s) and s[i].isspace():
            i += 1
        if i < len(s) and s[i] == "'":
            i += 1
            val = ""
            while s[i] != "'":
                if s[i] == "\\":
                    i += 1
                val += s[i]
                i += 1
            i += 1
        else:
            j = i
            while j < len(s) and not s[j].isspace():
                j += 1
            val, i = s[i:j], j
        out[key] = val
    return out


class GuideStepsTest(Scratch):
    """Steps of the guide run exactly as written (run-step.sh), against fakes, in a stand-in release folder."""

    def setUp(self) -> None:
        super().setUp()
        (self.dir / "docs").symlink_to(REPO / "docs")
        shutil.copy(REPO / "docs/guide/omnifile.env.example", self.dir / "omnifile.env")
        (self.dir / "omnifile.state.env").write_text(
            "export OMNI_VNET_ID=/subscriptions/s/resourceGroups/rg-omnifile/providers/Microsoft.Network/virtualNetworks/omni-vnet\n"
            "export OMNI_KV_NAME=omni-kv-x\nexport OMNI_ADMIN_OBJECT_ID=me\n")

    def step(self, n: int, env: dict[str, str]):
        return run([str(TOOLS / "run-step.sh"), str(n)], self.dir, env)

    def number(self, needle: str) -> int:
        """The number of the step whose text holds `needle`."""
        import re
        text = (REPO / "docs/guide/README.md").read_text()
        for m in re.finditer(r"```\w+ guide-step=(\d+)[^\n]*\n(.*?)\n```", text, re.S):
            if needle in m.group(2):
                return int(m.group(1))
        raise AssertionError(f"no step holds {needle!r}")

    def test_the_database_zone_is_linked_to_the_hub_and_forwarded(self) -> None:
        state = self.dir / "omnifile.state.env"
        state.write_text(state.read_text() + "export OMNI_PG_HOST=omni-pg-abc.postgres.database.azure.com\n"
                         "export OMNI_INTERNAL_DOMAIN=nicehill-1234.westeurope.azurecontainerapps.io\n")
        (self.dir / "dns-zones.json").write_text('["privatelink.blob.core.windows.net"]')
        n = self.number("--zone-name \"$pg_zone\"")
        r = self.step(n, self.fake_az({"network private-dns link vnet show": {"rc": 3}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        links = [c for c in self.az_calls() if c[:5] == ["network", "private-dns", "link", "vnet", "create"]]
        pg = [c for c in links if "omni-pg-abc.private.postgres.database.azure.com" in c]
        self.assertEqual(len(pg), 1)
        hub = example_values()["OMNI_HUB_VNET_ID"]
        self.assertEqual(pg[0][pg[0].index("--virtual-network") + 1], hub)
        n = self.number("dns-forwarders.ps1")
        r = self.step(n, self.fake_az({}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Add-DnsServerConditionalForwarderZone -Name 'postgres.database.azure.com'",
                      (self.dir / "dns-forwarders.ps1").read_text())

    def test_the_resource_group_keeps_its_tags_and_a_test_run_tags_a_new_one(self) -> None:
        n = self.number('az group create --name "$OMNI_RG"')
        r = self.step(n, self.fake_az({"group show": {"rc": 0}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("left as it is", r.stdout)
        self.assertFalse([c for c in self.az_calls() if c[:2] == ["group", "create"]])
        self.log.write_text("")
        r = self.step(n, self.fake_az({"group show": {"rc": 3}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        create = [c for c in self.az_calls() if c[:2] == ["group", "create"]]
        self.assertNotIn("--tags", create[0])
        self.log.write_text("")
        env = self.dir / "omnifile.env"
        env.write_text(env.read_text().replace("export OMNI_TEST_RUN_ID=''", "export OMNI_TEST_RUN_ID='run-1'")
                       .replace("export OMNI_TEST_RUN_EXPIRES=''", "export OMNI_TEST_RUN_EXPIRES='2026-11-04T18:00Z'"))
        r = self.step(n, self.fake_az({"group show": {"rc": 3}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        create = [c for c in self.az_calls() if c[:2] == ["group", "create"]]
        tags = create[0][create[0].index("--tags") + 1: create[0].index("--output")]
        self.assertEqual(tags, ["omnifile-run=run-1", "omnifile-expires=2026-11-04T18:00Z", "omnifile-harness=tests-real"])

    def test_peering_stops_when_the_hub_has_no_gateway(self) -> None:
        n = self.number("GatewaySubnet")
        answers = {"network vnet show": {"stdout": "0\n"}, "network vnet peering show": {"rc": 3}}
        r = self.step(n, self.fake_az(answers))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("has no ExpressRoute or VPN gateway", r.stdout)
        self.assertFalse([c for c in self.az_calls() if c[:4] == ["network", "vnet", "peering", "create"]])

    def test_peering_without_a_gateway_only_in_a_test_run(self) -> None:
        n = self.number("GatewaySubnet")
        answers = {"network vnet show": {"stdout": "0\n"}, "network vnet peering show": {"rc": 3}}
        env = self.dir / "omnifile.env"   # the override is a value of omnifile.env, as a harness writes it
        env.write_text(env.read_text().replace("export OMNI_TEST_HUB_NO_GATEWAY=''", "export OMNI_TEST_HUB_NO_GATEWAY='1'"))
        r = self.step(n, self.fake_az(answers))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        creates = [c for c in self.az_calls() if c[:4] == ["network", "vnet", "peering", "create"]]
        self.assertEqual(len(creates), 2)
        for c in creates:
            self.assertNotIn("--use-remote-gateways", c)
            self.assertNotIn("--allow-gateway-transit", c)

    def test_peering_uses_the_hubs_gateway(self) -> None:
        n = self.number("GatewaySubnet")
        answers = {"network vnet show": {"stdout": "1\n"}, "network vnet peering show": {"rc": 3}}
        r = self.step(n, self.fake_az(answers))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        creates = [c for c in self.az_calls() if c[:4] == ["network", "vnet", "peering", "create"]]
        self.assertIn("--allow-gateway-transit", creates[0])
        self.assertIn("--use-remote-gateways", creates[1])

    def test_the_shared_keys_never_go_on_a_command_line(self) -> None:
        n = self.number("put_once api-lease-key")
        for f in ("intent.key", "relay.pem", "relay.key", "edge-ca.pem"):
            (self.dir / f).write_text("fake: test material\n")
        r = self.step(n, self.fake_az({"keyvault secret show": {"rc": 3}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sets = {c[c.index("--name") + 1]: c for c in self.az_calls() if c[:3] == ["keyvault", "secret", "set"]}
        for name in ("api-lease-key", "web-cursor-key"):
            self.assertIn("--file", sets[name])
            self.assertNotIn("--value", sets[name])
            self.assertFalse(os.path.exists(sets[name][sets[name].index("--file") + 1]), "the key file is left behind")

    def test_the_key_file_is_removed_when_the_upload_fails(self) -> None:
        n = self.number("put_once api-lease-key")
        for f in ("intent.key", "relay.pem", "relay.key", "edge-ca.pem"):
            (self.dir / f).write_text("fake: test material\n")
        answers = {"keyvault secret show": {"rc": 3}, "keyvault secret set --vault-name omni-kv-x --name api-lease-key": {"rc": 1}}
        r = self.step(n, self.fake_az(answers))
        self.assertNotEqual(r.returncode, 0)
        sets = [c for c in self.az_calls() if c[:3] == ["keyvault", "secret", "set"]]
        self.assertEqual(sets[0][sets[0].index("--name") + 1], "api-lease-key")
        self.assertFalse(os.path.exists(sets[0][sets[0].index("--file") + 1]), "the key file outlived a failed upload")

    def test_the_vault_right_is_removed_after_phase_2(self) -> None:
        n = self.number('role assignment delete --assignee "$OMNI_ADMIN_OBJECT_ID"')
        answers = {"keyvault show": {"stdout": "/subscriptions/s/kv\n"},
                   "role assignment list": {"stdout": "/subscriptions/s/kv/providers/Microsoft.Authorization/roleAssignments/r1\n"}}
        r = self.step(n, self.fake_az(answers))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dele = [c for c in self.az_calls() if c[:3] == ["role", "assignment", "delete"]]
        self.assertEqual(len(dele), 1)
        self.assertEqual(dele[0][dele[0].index("--role") + 1], "Key Vault Secrets Officer")
        self.log.write_text("")
        r = self.step(n, self.fake_az({"keyvault show": {"stdout": "/subscriptions/s/kv\n"}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse([c for c in self.az_calls() if c[:3] == ["role", "assignment", "delete"]])

    @unittest.skipIf(openssl3() is None, "needs OpenSSL 3 (Ed25519)")
    def test_the_card_signing_key_is_made_once(self) -> None:
        n = self.number("openssl genpkey -algorithm ed25519")
        env = self.fake_az({}, openssl3="1")
        r = self.step(n, env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = (self.dir / "omnifile.state.env").read_text()
        self.assertIn("OMNI_INTENT_PUBLIC_KEYS", first)
        (self.dir / "intent.key").unlink()   # as the next steps do, once it is in the vault
        r = self.step(n, env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("made already", r.stdout)
        self.assertEqual((self.dir / "omnifile.state.env").read_text(), first)
        self.assertFalse((self.dir / "intent.key").exists())

    def test_a_buckets_other_notifications_are_kept(self) -> None:
        n = self.number("get-bucket-notification-configuration")
        env_file = (self.dir / "omnifile.env").read_text()
        for k, v in {"OMNI_AWS_ACCOUNT_ID": "123456789012", "OMNI_AWS_REGION": "eu-west-1",
                     "OMNI_AWS_SOURCE_BUCKETS": "seismic-a"}.items():
            env_file = env_file.replace(f"export {k}=''", f"export {k}='{v}'")
        (self.dir / "omnifile.env").write_text(env_file)
        existing = {"TopicConfigurations": [{"TopicArn": "arn:aws:sns:eu-west-1:123456789012:other", "Events": ["s3:ObjectCreated:*"]}]}
        answers = {"s3api get-bucket-notification-configuration": {"stdout": json.dumps(existing)},
                   "sqs create-queue": {"stdout": "https://sqs.eu-west-1.amazonaws.com/123456789012/omnifile-events\n"},
                   "sqs get-queue-attributes": {"stdout": "arn:aws:sqs:eu-west-1:123456789012:omnifile-events\n"},
                   "events put-rule": {"stdout": "arn:aws:events:eu-west-1:123456789012:rule/omnifile-s3\n"}}
        r = self.step(n, self.fake_az(answers, names=("aws",)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        written = json.loads((self.dir / "notify-seismic-a.json").read_text())
        self.assertEqual(written["TopicConfigurations"], existing["TopicConfigurations"])
        self.assertEqual(written["EventBridgeConfiguration"], {})
        put = [c for c in self.az_calls() if c[:2] == ["s3api", "put-bucket-notification-configuration"]]
        self.assertEqual(put[0][-1], "file://notify-seismic-a.json")

    def test_the_guide_says_events_start_five_minutes_after_the_step(self) -> None:
        # Real AWS, 2026-11-14: objects written seconds after put-bucket-notification-configuration sent
        # no event at all; ones written five minutes later did.
        text = (REPO / "docs/guide/README.md").read_text()
        after = text.split("put-bucket-notification-configuration", 1)[1].split("### 17.4", 1)[0]
        self.assertIn("about five minutes to start sending a bucket's events", " ".join(after.split()))

    def test_the_event_queue_and_rule_carry_the_name_suffix_only_in_a_test_run(self) -> None:
        n = self.number("get-bucket-notification-configuration")
        base = (self.dir / "omnifile.env").read_text()
        for k, v in {"OMNI_AWS_ACCOUNT_ID": "123456789012", "OMNI_AWS_REGION": "eu-west-1",
                     "OMNI_AWS_SOURCE_BUCKETS": "seismic-a"}.items():
            base = base.replace(f"export {k}=''", f"export {k}='{v}'")
        answers = {"s3api get-bucket-notification-configuration": {"stdout": "{}"},
                   "sqs create-queue": {"stdout": "https://sqs.eu-west-1.amazonaws.com/123456789012/q\n"},
                   "sqs get-queue-attributes": {"stdout": "arn:aws:sqs:eu-west-1:123456789012:q\n"},
                   "events put-rule": {"stdout": "arn:aws:events:eu-west-1:123456789012:rule/r\n"}}
        for suffix, queue, rule in (("", "omnifile-events", "omnifile-s3"),
                                    ("run-1x", "omnifile-events-run-1x", "omnifile-s3-run-1x")):
            (self.dir / "omnifile.env").write_text(
                base.replace("export OMNI_AWS_NAME_SUFFIX=''", f"export OMNI_AWS_NAME_SUFFIX='{suffix}'"))
            self.log.write_text("")
            r = self.step(n, self.fake_az(answers, names=("aws",)))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            calls = self.az_calls()
            create = next(c for c in calls if c[:2] == ["sqs", "create-queue"])
            self.assertEqual(create[create.index("--queue-name") + 1], queue)
            put = next(c for c in calls if c[:2] == ["events", "put-rule"])
            self.assertEqual(put[put.index("--name") + 1], rule)
            targets = next(c for c in calls if c[:2] == ["events", "put-targets"])
            self.assertEqual(targets[targets.index("--rule") + 1], rule)

    def test_the_api_client_step_grants_each_scope_and_skips_without_a_client(self) -> None:
        n = self.number("section 18 skipped")
        r = self.step(n, self.fake_az({}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("section 18 skipped", r.stdout)
        self.assertEqual(self.az_calls(), [])
        env_file = (self.dir / "omnifile.env").read_text()
        for k, v in {"OMNI_API_CLIENT_APP_ID": "55555555-5555-5555-5555-555555555555",
                     "OMNI_API_CLIENT_NAME": "Migration tool", "OMNI_API_CLIENT_SCOPES": "Archive.Read,Recall.Request",
                     "OMNI_API_CLIENT_OWNER": "ops@contoso.example"}.items():
            env_file = env_file.replace(f"export {k}=''", f"export {k}='{v}'")
        (self.dir / "omnifile.env").write_text(env_file)
        state = self.dir / "omnifile.state.env"   # step 28 recorded the API's application id
        state.write_text((state.read_text() if state.exists() else "") + "export OMNI_WEB_CLIENT_ID=web\n")
        answers = {"ad sp show --id 55555555-5555-5555-5555-555555555555 --query id": {"stdout": "sp-client\n"},
                   "ad sp show": {"stdout": "x\n"}, "rest --method GET": {"stdout": "0\n"}}
        r = self.step(n, self.fake_az(answers))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        roles = [c[c.index("--query") + 1] for c in self.az_calls() if c[:2] == ["ad", "sp"] and "appRoles" in " ".join(c)]
        self.assertEqual(roles, ["appRoles[?value=='Archive.Read'].id | [0]", "appRoles[?value=='Recall.Request'].id | [0]"])
        posts = [json.loads(c[c.index("--body") + 1]) for c in self.az_calls() if c[:3] == ["rest", "--method", "POST"]]
        self.assertEqual([p["principalId"] for p in posts], ["sp-client", "sp-client"])

    def test_the_aws_answers_are_replaced_not_appended(self) -> None:
        n = self.number("setup.toml.before-aws")
        shown = run([str(TOOLS / "run-step.sh"), "--show", str(n)], self.dir).stdout.splitlines()[1:]
        etc = self.dir / "etc"
        etc.mkdir()
        (etc / "setup.toml").write_text('version = 1\nhostname = "edge1"\n\n[updates]\nreboot_window = "Sun *-*-* 03:00"\n')
        body = "\n".join(ln for ln in shown if "omnifile-setup" not in ln and "systemctl" not in ln)
        body = body.replace("sudo ", "").replace("/etc/omnifile/", f"{etc}/")
        (self.dir / "edge-aws.toml").write_text('\n[aws]\ntrust_anchor_arn = "a"\n')
        script = self.dir / "step.sh"
        script.write_text(body + "\n")
        for _ in range(3):
            r = run(["bash", "-euo", "pipefail", str(script)], self.dir)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = (etc / "setup.toml").read_text()
            if _ == 0:
                first = after
        self.assertEqual(after, first, "a rerun changed the file")
        self.assertEqual(after.count("[aws]"), 1)
        self.assertIn('hostname = "edge1"', after)
        self.assertIn("[updates]", after)

    def test_a_failed_edit_leaves_the_aws_answers_as_they_were(self) -> None:
        n = self.number("setup.toml.before-aws")
        shown = run([str(TOOLS / "run-step.sh"), "--show", str(n)], self.dir).stdout.splitlines()[1:]
        etc = self.dir / "etc"
        etc.mkdir()
        before = 'version = 1\nhostname = "edge1"\n'
        (etc / "setup.toml").write_text(before)
        body = "\n".join(ln for ln in shown if "omnifile-setup" not in ln and "systemctl" not in ln)
        body = body.replace("sudo ", "").replace("/etc/omnifile/", f"{etc}/")
        (self.dir / "edge-aws.toml").write_text('\n[aws]\ntrust_anchor_arn = "a"\n')
        (self.dir / "step.sh").write_text(body + "\n")
        # An awk that fails (a full disk, a killed process) part way.
        (self.bin / "awk").write_text("#!/bin/sh\nexit 2\n")
        (self.bin / "awk").chmod(0o755)
        r = run(["bash", str(self.dir / "step.sh")], self.dir, clean_env(PATH=f"{self.bin}:{os.environ['PATH']}"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((etc / "setup.toml").read_text(), before)
        self.assertEqual(sorted(x.name for x in etc.iterdir()), ["setup.toml", "setup.toml.before-aws"])

    def test_every_database_connection_takes_a_group_name_with_a_space(self) -> None:
        import re
        text = (REPO / "docs/guide/README.md").read_text()
        strings = re.findall(r'"(host=\$OMNI_PG_HOST [^"]*)"', text)
        self.assertGreaterEqual(len(strings), 5)
        for s in strings:
            got = run(["bash", "-c", f'printf %s "{s}"'], self.dir,
                      clean_env(OMNI_PG_HOST="omni-pg.postgres.database.azure.com", OMNI_PG_DATABASE="omnifile",
                                OMNI_PG_ADMIN_GROUP="OmniFile DB Admins"))
            params = libpq_params(got.stdout)
            self.assertEqual(params["user"], "OmniFile DB Admins", s)
            self.assertEqual(params["sslmode"], "verify-full", s)


class PrivateDnsTest(Scratch):
    def test_a_name_that_does_not_resolve_fails(self) -> None:
        r = run([str(TOOLS / "check-private-dns.sh"), "https://no-such-host.invalid:8443/x"], self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no-such-host.invalid: does not resolve", r.stdout)


if __name__ == "__main__":
    unittest.main()
