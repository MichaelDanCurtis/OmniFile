"""Check functions whose verdict logic matters on its own: T5's (RA-30) refuses to pass when
api lets a caller in without an edge certificate."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from harness.checks import CheckFailed, Partial
from harness.checks import azure as ca


def env_answering(answers: dict, api: str = "https://rx-api.internal.example:8443"):
    line = "ofreal-t5-probe " + json.dumps(answers)

    def run(argv, **kw):
        assert argv[:3] == ["az", "containerapp", "exec"]
        # api's own replica: web can scale to zero at deploymentSize=small
        assert argv[argv.index("-n") + 1] == "rx-api"
        assert "exec(base64.b64decode(" in argv[-1] and api in argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=f"noise\n{line}\n", stderr="")

    ctx = SimpleNamespace(
        state={"rg": "rg", "prefix": "rx", "outputs": {"apiUrl": api, "namePrefix": "rx"}}, run=run
    )
    return SimpleNamespace(ctx=ctx, rg="rg", prefix="rx")


REFUSED = {"tls": "refused", "error": "SSLError"}


def test_both_refused_passes():
    ev = ca.ra30(env_answering({"no_client_cert": REFUSED, "forged_xfcc": REFUSED}))
    assert ev["answers"]["forged_xfcc"] == REFUSED and ev["peer"] == "rx-api"


def test_a_4xx_after_the_handshake_is_a_refusal_too():
    ca.ra30(
        env_answering(
            {"no_client_cert": REFUSED, "forged_xfcc": {"tls": "accepted", "status": 401}}
        )
    )


@pytest.mark.parametrize("which", ["no_client_cert", "forged_xfcc"])
def test_a_caller_let_in_without_an_edge_certificate_fails(which):
    answers = {"no_client_cert": REFUSED, "forged_xfcc": REFUSED}
    answers[which] = {"tls": "accepted", "status": 200}
    with pytest.raises(CheckFailed, match="without an edge certificate"):
        ca.ra30(env_answering(answers))


def test_an_api_url_off_the_tcp_port_is_only_partial():
    with pytest.raises(Partial, match="8443"):
        ca.ra30(
            env_answering(
                {"no_client_cert": REFUSED, "forged_xfcc": REFUSED},
                api="https://rx-api.internal.example",
            )
        )


def test_the_probe_script_itself_compiles():
    compile(ca.T5_PROBE, "t5", "exec")
