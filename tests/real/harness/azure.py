"""The Azure run: the customer guide deploys the service (its own what-if is what the owner
approves), the fresh appliance boots on a nested-virt VM in the VNet, then the M6 exit and the
real-Azure checks run, and teardown removes everything (harness/azure_down.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

from . import azpin, guide, guidevalues, release
from .allowlist import AllowList
from .core import (
    REPO,
    TAG_EXPIRES,
    TAG_RUN,
    TEAMS_SCHEMA,
    AwaitingApproval,
    Context,
    Refused,
    StepFailed,
    UsageError,
    iso,
    norm_digest,
    prefix_for,
)
from .guiderun import run_steps
from .portal import Portal
from .results import run_checks
from .vm import EDGE_SUBNET_PREFIX, REMOTE, TestVM

DIGEST_LINE = re.compile(r"^digest:\s*(sha256:[0-9a-f]{64})\s*$")
#: whatif.sh's lines for what its digest covers, by file: a deploy's grant binds them.
INPUT_LINE = re.compile(r"^(template|parameters):\s*(sha256:[0-9a-f]{64})\s*$")
AD_REALM = guidevalues.AD_REALM


#: How the guide says a gate previewed nothing because its section doesn't apply to this
#: client ("no S3 values in omnifile.env: section 17 skipped").
SKIP_MARKER = re.compile(r": section [0-9.]+ skipped$")

#: A step that deploys: WP1's whatif.sh with --deploy (anywhere on its line, continuation
#: lines joined first), or a direct deployment create at any scope.
DEPLOYS = re.compile(r"whatif\.sh\b[^\n]*--deploy\b")
#: A deploy that binds no digest: only whatif.sh --deploy may deploy (it refuses unless the
#: fresh what-if is the approved one), so a step that deploys any other way is refused.
DIRECT_DEPLOYS = re.compile(
    r"\baz\s+deployment\s+(?:(?:group|sub|mg|tenant)\s+)?create\b"  # and the legacy `az deployment create`
    r"|\baz\s+group\s+deployment\s+create\b"  # older still
    r"|\baz\s+stack\s+(?:group|sub|mg)\s+(?:create|set)\b"
    r"|\bNew-Az(?:ResourceGroup|Subscription|ManagementGroup|Tenant)?Deployment(?:Stack)?\b"
    r"|\bSet-Az(?:ResourceGroup|Subscription|ManagementGroup)DeploymentStack\b"
)


def joined(body: str) -> str:
    """A step's body with its continuation lines joined, as the shell reads it."""
    return re.sub(r"\\\n", " ", body)


class Azure:
    def __init__(self, ctx: Context, al: AllowList) -> None:
        self.ctx, self.al = ctx, al

    # ------------------------------------------------------------ identity

    def identity(self) -> dict[str, str]:
        """The run's subscription and tenant, checked against the allow-list: the first call
        of every phase, before anything that changes a thing. The owner's sign-in is read where
        it is; only once the check passes is it copied into the run's own az directory and
        pinned there (azpin): to the owner's default at a run's first phase, and to the
        subscription the run was planned in at every phase after."""
        ctx = self.ctx
        acct = azpin.owner_account(ctx)
        if not acct:
            raise Refused("az is not signed in (az login); the harness never signs in")
        planned = ctx.state.get("identity")
        sub = planned["subscription"] if planned else acct["id"]
        self.al.require("azure-subscription", sub, "subscription")
        if not self.al.values("azure-tenant"):
            raise Refused("the allow-list names no azure-tenant")
        if not planned:
            self.al.require("azure-tenant", acct["tenantId"], "tenant")
        azpin.pin(ctx, sub, service_principal=azpin.is_service_principal(acct))
        got = ctx.az("account", "show")
        tenant = got["tenantId"]
        self.al.require("azure-tenant", tenant, "tenant")
        me = {
            "subscription": got["id"],
            "tenant": tenant,
            "user": acct.get("user", {}).get("name", ""),
        }
        st = ctx.state.setdefault("identity", me)
        if st["subscription"] != got["id"] or st["tenant"] != tenant:
            raise Refused(
                f"run {ctx.run_id} was planned in subscription {st['subscription']} "
                f"(tenant {st['tenant']}); the pinned sign-in is {got['id']} ({tenant})"
            )
        ctx.save_state()
        return me

    # ------------------------------------------------------------- names

    def _init_state(self) -> None:
        st, ctx = self.ctx.state, self.ctx
        st.setdefault("run_id", ctx.run_id)
        st.setdefault("cloud", "azure")
        st.setdefault("dry_run", ctx.dry_run)
        st.setdefault("prefix", prefix_for(ctx.run_id))
        # The start of the run's window: an untagged Entra object found by name is the run's
        # only if it was made after this (ownership.py).
        st.setdefault("started", iso(ctx.now))
        st.setdefault("rg", f"rg-ofreal-{ctx.run_id}")
        st.setdefault("location", self.al.one("azure-location"))
        ctx.fix_expiry(self.al.max_ttl)

    @property
    def rg(self) -> str:
        return self.ctx.state["rg"]

    def deploy_env(self) -> dict[str, str]:
        """What deploy/azure/test.bicepparam reads (WP1's interface), for steps that use it."""
        st = self.ctx.state
        return {
            "OMNIFILE_RUN_ID": self.ctx.run_id,
            "OMNIFILE_RUN_EXPIRES": self.ctx.expires,
            "OMNIFILE_TEST_PG_ADMIN_OID": st.get(
                "pg_admin_oid", "00000000-0000-0000-0000-000000000000"
            ),
            "OMNIFILE_TEST_PG_ADMIN_NAME": guidevalues.pg_admin_name(self.ctx.run_id),
            "OMNIFILE_TEST_REGISTRY": f"ofreal{st['prefix']}.azurecr.io",
            "OMNIFILE_TEST_REGISTRY_ID": self._acr_id(),
            "OMNIFILE_TEST_ENROL_CIDRS": EDGE_SUBNET_PREFIX,
            "OMNIFILE_TEST_WEB_CIDRS": EDGE_SUBNET_PREFIX,
            "OMNIFILE_TEST_PG_ADMIN_CIDRS": EDGE_SUBNET_PREFIX,
            "OMNIFILE_TEST_PREFIX": st["prefix"],
            "OMNIFILE_TEST_UPLOADER_OID": st.get("owner_oid", ""),
        }

    def _acr_id(self) -> str:
        st = self.ctx.state
        return (
            f"/subscriptions/{st['identity']['subscription']}/resourceGroups/{self.rg}"
            f"/providers/Microsoft.ContainerRegistry/registries/ofreal{st['prefix']}"
        )

    # -------------------------------------------------------------- plan

    def plan(self) -> None:
        """What the owner approves: the guide's own what-if (its gate=whatif step), run with
        this run's values. Before it: the empty, tagged resource group what-if needs, the
        PostgreSQL admin group and the registry the guide's values name, and the guide's
        plan=yes steps (read-only). Nothing else is made before an approval."""
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        self._init_state()
        ctx.save_state()
        with ctx.step("plan.group"):
            ctx.az(
                "group",
                "create",
                "-n",
                self.rg,
                "-l",
                ctx.state["location"],
                "--tags",
                *ctx.az_tags(),
            )
            ctx.state["created"] = True
            # A plan starts the guide afresh: nothing of an earlier, torn-down up resumes.
            for k in ("paused_at", "pending_gate", "guide_done", "approved_gates"):
                ctx.state.pop(k, None)
            ctx.save_state()
        # The real az, by absolute path, for the guard every guide step runs behind.
        from .guiderun import real_az

        ctx.state.setdefault("real_az", real_az(ctx))
        ctx.save_state()
        with ctx.step("plan.pg-admins"):
            self._pg_admin_group()
        with ctx.step("plan.registry"):
            ctx.az(
                "acr",
                "create",
                "-g",
                self.rg,
                "-n",
                f"ofreal{ctx.state['prefix']}",
                "--sku",
                "Basic",
                "-l",
                ctx.state["location"],
                "--tags",
                *ctx.az_tags(),
            )
            ctx.state["acr"] = f"ofreal{ctx.state['prefix']}"
            ctx.save_state()
        rel, steps, gates = self._guide()
        gate = gates[0]
        before = [s for s in steps if s.plan_step and s.n <= gate.n]
        ctx.state["plan_done"] = []
        digest: list[str] = []

        def after(s: guide.Step, out: str | None) -> None:
            if out is not None and s is gate:
                digest.append(self._gate_digest(out))

        with ctx.step("plan.whatif"):
            run_steps(ctx, before, rel, {}, self.values, done_key="plan_done", after=after)
        if not digest:
            raise StepFailed("the guide's gate step ran but printed no `digest: sha256:` line")
        ctx.state["digest"] = digest[-1]
        ctx.save_state()
        ctx.log(f"what-if digest {digest[-1]} (the guide's step {gate.n}: evidence/whatif.txt)")
        ctx.log(
            f"to deploy: tests/real/run.sh all --cloud azure --run-id {ctx.run_id} "
            f"--approve {digest[-1]}"
        )

    def _guide(self) -> tuple[Path, list[guide.Step], list[guide.Step]]:
        """The release folder, the guide's Azure steps, and its gate=whatif steps: one before
        each deploy (phase 1, phase 2, the AWS settings), each approved by its own digest."""
        rel = release.make(self.ctx.out, dry_run=self.ctx.dry_run, guide=self._guide_dir())
        gdir = self._guide_dir()
        steps = [s for s in guide.parse(gdir) if s.cloud == "azure"]
        gates = [s for s in steps if s.gate]
        if not gates:
            raise Refused(
                "the guide has no gate=whatif step: without one the harness can't know what "
                "the guide deploys, so it won't approve anything"
            )
        direct = [s.n for s in steps if DIRECT_DEPLOYS.search(joined(s.body))]
        if direct:
            raise Refused(
                "guide step(s) " + ", ".join(str(n) for n in direct) + " deploy directly "
                "(az deployment ... create or az stack ... create/set): only "
                "deploy/azure/whatif.sh --deploy may deploy, since it binds the approved digest"
            )
        undeclared = self._ungated_deploys(steps, gates)
        if undeclared:
            raise Refused(
                "guide step(s) "
                + ", ".join(str(n) for n in undeclared)
                + " deploy (whatif.sh --deploy) with no gate=whatif step before them since the "
                "last deploy: the owner would approve a deploy without its own what-if"
            )
        self._appliance_files(rel)
        guide.write_env(gdir / "omnifile.env.example", rel / "omnifile.env", self.guide_values())
        (rel / "omnifile.state.env").touch()
        return rel, steps, gates

    @staticmethod
    def _ungated_deploys(steps: list[guide.Step], gates: list[guide.Step]) -> list[int]:
        """Deploy steps with no gate of their own: every `whatif.sh ... --deploy` needs a
        gate=whatif step after the previous deploy and before it."""
        gate_ns = {g.n for g in gates}
        bad, gated = [], False
        for s in steps:
            if s.n in gate_ns:
                gated = True
            elif DEPLOYS.search(joined(s.body)):
                if not gated:
                    bad.append(s.n)
                gated = False
        return bad

    def _gate_digest(self, out: str, name: str = "whatif.txt") -> str:
        found = [m.group(1) for ln in out.splitlines() if (m := DIGEST_LINE.match(ln.strip()))]
        if not found:
            raise StepFailed("the guide's gate step printed no `digest: sha256:<hex>` line")
        (self.ctx.evidence / name).write_text(self.ctx.mask(out))
        return norm_digest(found[-1])

    def _approvals(self) -> list[str]:
        return [norm_digest(d) for d in self.ctx.approvals if d != "auto"]

    def _approve_later_gate(self, s: guide.Step, got: str) -> None:
        """A later deploy's own what-if (phase 2, the AWS settings). Its digest is known only
        now, after the earlier deploys: approved when the owner passed it (--approve, once per
        gate), or typed it in at the prompt of an interactive run; otherwise the run stops
        here, nothing of this deploy done, and `up` resumes at this step once approved."""
        ctx, ev = self.ctx, f"whatif-step-{s.n}.txt"
        if "auto" in ctx.approvals and ctx.dry_run:
            ctx.log(f"dry run: approving the what-if of guide step {s.n}, {got}")
            return
        if got in self._approvals():
            return
        if not ctx.dry_run and sys.stdin.isatty():
            print(f"\n--- guide step {s.n}'s what-if is evidence/{ev}; its digest is {got}")
            ans = input("Read it, then paste that digest to approve it (anything else stops): ")
            if ans.strip().lower() == got:
                ctx.approvals.append(got)
                return
        ctx.state["paused_at"] = s.n
        ctx.state["pending_gate"] = {"step": s.n, "digest": got}
        ctx.save_state()
        flags = " ".join(f"--approve {d}" for d in [ctx.state["digest"], *self._later(), got])
        raise AwaitingApproval(
            f"guide step {s.n} previews another deploy: read evidence/{ev}, then resume with "
            f"tests/real/run.sh {getattr(ctx, 'driver', 'up')} --cloud azure "
            f"--run-id {ctx.run_id} {flags}"
        )

    def _require_approved(
        self,
        s: guide.Step,
        recorded: str | None,
        *,
        fresh: str | None = None,
        gate_n: int | None = None,
    ) -> None:
        """A deploy step runs only with the digest its own gate previewed in this run (FRESH,
        none if that gate didn't run), which the owner approved: the plan's, a later gate's,
        or one an interactive run took at its prompt. A digest carried over from an earlier
        gate is not enough."""
        ctx = self.ctx
        if gate_n is not None and (not recorded or norm_digest(recorded) != fresh):
            raise AwaitingApproval(
                f"guide step {s.n} would deploy with OMNI_WHATIF_DIGEST {recorded or '(none)'}, "
                f"not the digest its own gate (step {gate_n}) previewed in this run "
                f"({fresh or 'none'}): nothing was deployed"
            )
        approved = {ctx.state.get("digest"), *ctx.state.get("approved_gates", {}).values()}
        if "auto" in ctx.approvals and ctx.dry_run:
            approved |= {ctx.state.get("approved")}
        approved |= set(self._approvals())
        approved.discard(None)
        if recorded and norm_digest(recorded) in approved:
            return
        raise AwaitingApproval(
            f"guide step {s.n} deploys with OMNI_WHATIF_DIGEST {recorded or '(none)'}, which no "
            "one approved in this run: nothing was deployed. Run its what-if gate and approve "
            "that digest"
        )

    @staticmethod
    def _gate_inputs(out: str) -> dict[str, str]:
        """The compiled template and parameters a gate's digest covers (whatif.sh prints
        them just before the digest)."""
        got: dict[str, str] = {}
        for ln in out.splitlines():
            if m := INPUT_LINE.match(ln.strip()):
                got[m.group(1)] = m.group(2)
        return got

    def _grant(self, s: guide.Step, digest: str, files: dict[str, str]) -> dict[str, str]:
        """A one-shot grant for this deploy step: the az guard lets whatif.sh's create through
        once, for this digest, the run's group, and the compiled template and parameters the
        gate previewed (tests/real/harness/azguard.py)."""
        import secrets

        ctx = self.ctx
        if set(files) != {"template", "parameters"}:
            raise StepFailed(
                f"guide step {s.n}'s gate printed no `template:` and `parameters:` lines "
                "(deploy/azure/whatif.sh prints them): the az guard can't bind its deploy"
            )
        path = ctx.out / "deploy-grants.json"
        try:
            grants = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            grants = {}
        token = secrets.token_hex(16)
        grants[token] = {"step": s.n, "digest": digest, "rg": self.rg, "used": False, **files}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(grants, f, indent=2, sort_keys=True)
        ctx.secrets[f"deploy-grant:{s.n}"] = token  # masked in every log
        return {"OMNIFILE_DEPLOY_GRANT": token}

    def _later(self) -> list[str]:
        """Later gates' digests already approved in this run (kept for the resume command)."""
        first = self.ctx.state.get("digest")
        return [d for d in self.ctx.state.get("approved_gates", {}).values() if d != first]

    def _pg_admin_group(self) -> None:
        """The Entra group that administers PostgreSQL in this run, with the signed-in owner in
        it. Made once per run (a plan rerun reuses it, so the digest stays the same). Groups
        take no tags, so its name carries the whole run id and its description names the
        run, which is how teardown knows it is this run's."""
        ctx, st = self.ctx, self.ctx.state
        if st.get("pg_admin_oid"):
            return
        name = guidevalues.pg_admin_name(ctx.run_id)
        st["pg_admin_name"] = name
        g = ctx.az(
            "ad",
            "group",
            "create",
            "--display-name",
            name,
            "--mail-nickname",
            name,
            "--description",
            f"{TAG_RUN}={ctx.run_id} {TAG_EXPIRES}={ctx.expires}",
        )
        st["pg_admin_oid"] = g["id"]
        ctx.save_state()
        me = ctx.az("ad", "signed-in-user", "show")
        st["owner_oid"] = me["id"]
        ctx.az("ad", "group", "member", "add", "--group", g["id"], "--member-id", me["id"])
        ctx.save_state()

    # ----------------------------------------------------------------- up

    def guide_values(self) -> dict[str, str]:
        st = self.ctx.state
        aws = guidevalues.aws_values()
        v = {
            **guidevalues.omni(st, st["prefix"], aws),
            **guidevalues.test_values(self.ctx),
            # The fixture's names (tests/real/fakes/guide).
            "AWS_REGION": aws["OMNI_AWS_REGION"],
            "EDGE_CA_FILE": "edge-ca.pem",
            "SUBSCRIPTION_ID": st["identity"]["subscription"],
            "TENANT_ID": st["identity"]["tenant"],
            "LOCATION": st["location"],
            "RG": self.rg,
            "PREFIX": st["prefix"],
            "PARAMS": "docs/guide/customer.bicepparam",
            "REGISTRY": f"ofreal{st['prefix']}.azurecr.io",
            "REGISTRY_NAME": f"ofreal{st['prefix']}",
            "AD_DOMAIN": AD_REALM.lower(),
            "AD_WORKGROUP": "OFREAL",
            "AD_JOIN_USER": "ofreal-join",
            "AD_ADMIN_GROUP": "filer admins",
            "EDGE_NAME": f"edge-{st['prefix']}",
            "TEAMS_SCHEMA": str(TEAMS_SCHEMA),
            "APPLIANCE_IMAGE": st.get("appliance_name", "omnifile-edge.qcow2"),
            "M365_TENANT_ID": (self.al.values("m365-tenant") or [st["identity"]["tenant"]])[0],
            **self.deploy_env(),
        }
        v.update({k: str(x) for k, x in st.get("outputs", {}).items()})
        # What the guide's steps recorded themselves, where the harness has no value of its own.
        recorded = guide.state_values(self.ctx.out / "release" / "omnifile.state.env")
        v.update({k: x for k, x in recorded.items() if k not in v})
        v.update(captured(self.ctx))
        return v

    def values(self) -> dict[str, str]:
        self._refresh_outputs()
        return self.guide_values()

    def up(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        if not ctx.state.get("created") or not ctx.state.get("digest"):
            raise UsageError(f"run {ctx.run_id} has no plan; run `plan` first")
        if not ctx.approvals:
            raise AwaitingApproval(
                f"awaiting approval: rerun with --approve {ctx.state['digest']} once you have "
                "read evidence/whatif.txt"
            )
        if "auto" in ctx.approvals:
            if not ctx.dry_run:
                raise Refused("--approve auto is for dry runs only")
            want = ctx.state["digest"]
            ctx.log(f"dry run: approving the digest this run's plan printed, {want}")
        else:
            want = ctx.state["digest"]
            if want not in self._approvals():
                raise AwaitingApproval(
                    f"the approved digest(s) {', '.join(self._approvals())} don't include "
                    f"this run's plan ({want})"
                )
        ctx.secret("AD_ADMIN_PASSWORD")
        ctx.secrets.update(guidevalues.secrets(ctx))
        with ctx.step("up.hub"):
            # The guide peers the deployment's VNet to the client's hub; a test run has none,
            # so it gets an empty one of its own (tagged, in the run's group).
            ctx.az(
                "network",
                "vnet",
                "create",
                "-g",
                self.rg,
                "-n",
                guidevalues.HUB_VNET,
                "-l",
                ctx.state["location"],
                "--address-prefixes",
                guidevalues.HUB_PREFIX,
                "--tags",
                *ctx.az_tags(),
            )
        rel, steps, gates = self._guide()
        gate = gates[0]
        example = self._guide_dir() / "omnifile.env.example"
        vm = TestVM(ctx, self.rg, ctx.state["location"], self.al.one("ssh-from"))

        def on_vm(s: guide.Step) -> str:
            vm.ensure()
            self._appliance_on_vm(vm)
            guide.write_env(example, ctx.out / "omnifile.env.vm", self.guide_values())
            vm.put(ctx.out / "omnifile.env.vm", f"{REMOTE}/release/omnifile.env")
            state_env = ctx.out / "release" / "omnifile.state.env"
            if state_env.exists():
                vm.put(state_env, f"{REMOTE}/release/omnifile.state.env")
            body = guide.fill(s.body, self.guide_values(), ctx.secrets)
            if s.write_to:
                vm.sh(
                    f"mkdir -p {REMOTE}/release && cat > {REMOTE}/release/{s.write_to}",
                    input=body,
                    tag=f"guide-{s.n}",
                )
                return f"wrote {s.write_to} on the VM\n"
            return vm.script(body, tag=f"guide-{s.n}", as_root=s.on == "kvm")

        def on_edge_shell(s: guide.Step) -> str:
            vm.ensure()
            body = guide.fill(s.body, self.guide_values(), ctx.secrets)
            stdin = base64.b64encode(body.encode()).decode() + "\n"
            if s.input_name:
                stdin += self._input_value(s.input_name) + "\n"
            return vm.sh(f"{REMOTE}/vm/edge-shell.sh", input=stdin, tag=f"guide-{s.n}")

        def on_console(s: guide.Step) -> str:
            vm.ensure()
            answers = guide.fill(s.body, self.guide_values(), ctx.secrets)
            return vm.sh(
                f"sudo {REMOTE}/vm/console-answers.py --domain edge-{ctx.state['prefix']}",
                input=answers,
                tag=f"guide-{s.n}",
            )

        def on_portal(s: guide.Step) -> str:
            vm.ensure()
            body = guide.fill_known(s.body, self.guide_values(), ctx.secrets)
            got = self._portal(vm).steps(body, tag=f"guide-{s.n}")
            caught = got.get("captured", {})
            for k, v in caught.items():
                # Enrolment codes are credentials: kept with the secrets (0600), not in state.
                ctx.add_secret(f"captured:{k}", v)
            return ctx.mask(json.dumps(got)) + "\n"

        handlers = {
            "kvm": on_vm,
            "linux-client": on_vm,
            "edge-network": on_vm,
            "edge-shell": on_edge_shell,
            "edge-console": on_console,
            "portal": on_portal,
        }

        state_env = rel / "omnifile.state.env"
        recorded_before: dict[int, str | None] = {}

        def recorded_digest() -> str | None:
            return guide.state_values(state_env).get("OMNI_WHATIF_DIGEST")

        outcomes = ctx.state.setdefault("gate_outcomes", {})
        inputs = ctx.state.setdefault("gate_inputs", {})  # what each gate's digest covers
        gate_ns = sorted(g.n for g in gates)

        def before(s: guide.Step) -> str | None:
            body = joined(s.body)
            if DIRECT_DEPLOYS.search(body):
                raise Refused(
                    f"guide step {s.n} deploys directly: only deploy/azure/whatif.sh --deploy "
                    "may deploy, since it binds the approved digest. Nothing was deployed"
                )
            if s.gate:
                recorded_before[s.n] = recorded_digest()
                return None
            if not DEPLOYS.search(body):
                return None
            # Its own gate: the nearest one before it. It must have previewed a deploy in this
            # run, approved, and recorded exactly that digest, which this step deploys.
            own = max((n for n in gate_ns if n < s.n), default=None)
            got = outcomes.get(str(own)) if own is not None else None
            if got == "skipped":
                ctx.log(f"guide step {s.n} not run: its gate, step {own}, previewed no deploy")
                return "skip"
            self._require_approved(s, recorded_digest(), fresh=got, gate_n=own)
            return self._grant(s, got, inputs.get(str(own)) or {})

        def after(s: guide.Step, out: str | None) -> None:
            self._record_made()
            if out is None or not s.gate:
                return
            if s is gate:
                got = self._gate_digest(out)
                if got != want:
                    raise AwaitingApproval(
                        f"the guide's what-if (step {s.n}) now gives {got}, not the approved "
                        f"{want}: nothing after it ran; read evidence/whatif.txt and approve "
                        "the new digest"
                    )
                ctx.state["approved"] = got
                outcomes[str(s.n)] = got
                inputs[str(s.n)] = self._gate_inputs(out)
            elif not any(DIGEST_LINE.match(ln.strip()) for ln in out.splitlines()):
                # Empty only when the guide says so (its skip marker) and recorded nothing new:
                # a gate that hid its digest (written to a file, not printed) stops the run.
                (ctx.evidence / f"whatif-step-{s.n}.txt").write_text(ctx.mask(out))
                skipped = any(SKIP_MARKER.search(ln.strip()) for ln in out.splitlines())
                if not skipped or recorded_digest() != recorded_before.get(s.n):
                    raise StepFailed(
                        f"guide step {s.n} is a what-if gate but printed no `digest:` line"
                        + ("" if skipped else " and no skip marker")
                        + (
                            ", and it recorded a new OMNI_WHATIF_DIGEST"
                            if recorded_digest() != recorded_before.get(s.n)
                            else ""
                        )
                        + ": nothing after it runs"
                    )
                ctx.log(f"guide step {s.n} previewed no deploy (it skipped): nothing to approve")
                outcomes[str(s.n)] = "skipped"
            else:
                got = self._gate_digest(out, f"whatif-step-{s.n}.txt")
                self._approve_later_gate(s, got)
                ctx.state.setdefault("approved_gates", {})[str(s.n)] = got
                outcomes[str(s.n)] = got
                inputs[str(s.n)] = self._gate_inputs(out)
            ctx.state.pop("paused_at", None)
            ctx.state.pop("pending_gate", None)
            ctx.save_state()

        if ctx.state.get("paused_at"):
            # Resuming at a later gate: what ran before it stays done (it deployed already).
            ctx.log(f"resuming at guide step {ctx.state['paused_at']} (its what-if, again)")
        else:
            ctx.state["guide_done"] = []
            outcomes.clear()  # a fresh up: no gate has previewed anything yet
            inputs.clear()
        try:
            run_steps(ctx, steps, rel, handlers, self.values, after=after, before=before)
        finally:
            self._close_portal()
            with ctx.step("up.adopt"):
                from .azure_down import AzureDown

                AzureDown(self).adopt_entra()
        ctx.state["up"] = True
        ctx.save_state()

    def _record_made(self) -> None:
        """Ids of what the guide's steps made outside the resource group, the moment they
        exist (register.sh's entra.json: the portal app and the Teams catalog app), so a
        failure in a later step still tears them down by id."""
        st = self.ctx.state
        entra = self.ctx.out / "release" / "deploy" / "entra" / "out" / "entra.json"
        if entra.exists():
            try:
                e = json.loads(entra.read_text())
            except json.JSONDecodeError:
                return
            if e.get("webClientId"):
                st["entra_app_ids"] = sorted({*st.get("entra_app_ids", []), e["webClientId"]})
            if e.get("teamsAppCatalogId"):
                st["teams_catalog_ids"] = sorted(
                    {*st.get("teams_catalog_ids", []), e["teamsAppCatalogId"]}
                )
            self.ctx.save_state()

    def _input_value(self, name: str) -> str:
        """A value a step reads on stdin (input=NAME): a test secret, a value, or what the
        portal last put on the clipboard (the enrolment code the portal's Copy code gives)."""
        src = {**self.guide_values(), **self.ctx.secrets}
        if name in src:
            return src[name]
        clip = captured(self.ctx).get("__clipboard__")
        if clip:
            return clip
        raise StepFailed(f"a guide step reads {name}, which the harness has no value for")

    def _guide_dir(self) -> Path:
        if self.ctx.guide:
            return self.ctx.guide
        real = REPO / "docs" / "guide"
        if real.is_dir():
            return real
        if self.ctx.dry_run:
            return REPO / "tests" / "real" / "fakes" / "guide"
        raise Refused("docs/guide is not on this branch (WP7 writes it)")

    def _refresh_outputs(self) -> None:
        """The deployment's outputs, as guide values (`<value:relayPublicUrl>` and the like)."""
        dep = self.ctx.az("deployment", "group", "list", "-g", self.rg, check=False) or []
        if not dep:
            return
        name = sorted(dep, key=lambda d: d.get("properties", {}).get("timestamp", ""))[-1]["name"]
        props = (
            self.ctx.az(
                "deployment",
                "group",
                "show",
                "-g",
                self.rg,
                "-n",
                name,
                "--query",
                "properties",
                check=False,
            )
            or {}
        )
        outs = {k: v.get("value") for k, v in (props.get("outputs") or {}).items()}
        prefix = ((props.get("parameters") or {}).get("namePrefix") or {}).get("value")
        self.ctx.state["outputs"] = flatten_outputs(outs, prefix)
        self.ctx.save_state()

    def _appliance_files(self, rel: Path) -> Path:
        """The release folder's appliance-files/ (as the guide expects it): the fresh
        appliance qcow2, named omnifile-edge_<release>.qcow2, and its SHA256SUMS. A dry run
        uses a placeholder file, never an image."""
        src = self.ctx.appliance
        if src is None:
            if self.ctx.dry_run:
                src = self.ctx.out / "omnifile-edge_0.0.0.qcow2"
                src.write_bytes(b"QFI\xfb dry-run placeholder, not an image\n")
            else:
                found = sorted((REPO / "appliance/build/out").glob("omnifile-edge_*.qcow2"))
                if not found:
                    raise UsageError(
                        "no appliance qcow2: build one (appliance/build.sh, "
                        "convert.sh) or pass --appliance FILE"
                    )
                src = found[-1]
        m = re.fullmatch(r"omnifile-edge_(.+)\.qcow2", src.name)
        if not m:
            raise UsageError(f"{src.name}: want omnifile-edge_<release>.qcow2")
        self.ctx.state["release"] = m.group(1)
        self.ctx.state["appliance_name"] = src.name
        files = rel / "appliance-files"
        files.mkdir(exist_ok=True)
        link = files / src.name
        if not link.exists():
            link.symlink_to(src.resolve())
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        (files / "SHA256SUMS").write_text(f"{h.hexdigest()}  {src.name}\n")
        self.ctx.save_state()
        return link

    def _appliance_on_vm(self, vm: TestVM) -> None:
        st = self.ctx.state.setdefault("vm", {})
        if st.get("appliance"):
            return
        rel = release.make(self.ctx.out, dry_run=self.ctx.dry_run, guide=self._guide_dir())
        src = self._appliance_files(rel)
        with self.ctx.step("vm.appliance"):
            vm.put(src.resolve(), f"{REMOTE}/release/appliance-files/{src.name}")
            vm.put(
                rel / "appliance-files" / "SHA256SUMS",
                f"{REMOTE}/release/appliance-files/SHA256SUMS",
            )
            # The release folder on the VM: the same files the workstation's has.
            vm.put_dir(self.ctx.out / "release" / "deploy", f"{REMOTE}/release/deploy")
            vm.sh(
                f"sudo {REMOTE}/vm/samba-dc.sh {AD_REALM}",
                tag="samba-dc",
                input="\n".join(
                    [
                        self.ctx.secret("AD_ADMIN_PASSWORD"),
                        self.ctx.secret("AD_JOIN_PASSWORD"),
                        self.ctx.secret("AD_USER_PASSWORD"),
                        self.ctx.secret("AD_OTHER_PASSWORD"),
                    ]
                )
                + "\n",
            )
            # The realm the edge joins: a tenant run reads it to know the edge's users are
            # the run's own DC's, which Entra doesn't sync (tenant.py's stub-read).
            self.ctx.state["ad_realm"] = AD_REALM
        st["appliance"] = src.name
        self.ctx.save_state()

    # ------------------------------------------------------------- portal

    _socks = None
    _portal_obj: Portal | None = None

    def _portal(self, vm: TestVM) -> Portal:
        if self._portal_obj is None:
            port = _free_port()
            self._socks = vm.socks(port)
            url = (
                self.ctx.state.get("outputs", {}).get("webUrl")
                or f"https://{self.ctx.state['prefix']}-web.internal.invalid"
            )
            self._portal_obj = Portal(self.ctx, url, port)
            self._portal_obj.act("sign-in")
        return self._portal_obj

    def _close_portal(self) -> None:
        if self._socks is not None:
            self._socks.terminate()
            self._socks = None
        self._portal_obj = None

    # --------------------------------------------------------------- exit

    def exit(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        if not ctx.state.get("up"):
            raise UsageError(f"run {ctx.run_id} is not up")
        vm = TestVM(ctx, self.rg, ctx.state["location"], self.al.one("ssh-from"))
        vm.ensure()
        r3 = getattr(ctx, "r3_dir", None)
        if r3 and not ctx.state.get("r3_dir_on_vm"):
            with ctx.step("exit.r3-upload"):
                # The release build the R3 gate signs: build output, bins, the source offer
                # and an optional SIGNER file (appliance/release.sh's inputs).
                vm.put_dir(Path(r3), f"{REMOTE}/r3")
                ctx.state["r3_dir_on_vm"] = True
                ctx.save_state()
        from .m6exit import M6Exit

        try:
            M6Exit(ctx, vm, self._portal(vm)).run()
        finally:
            self._close_portal()

    # ------------------------------------------------------------- checks

    def checks(self) -> dict[str, int]:
        with self.ctx.step("identity"):
            self.identity()
        from .checks.azure import AzureEnv

        vm = TestVM(self.ctx, self.rg, self.ctx.state["location"], self.al.one("ssh-from"))
        return run_checks(self.ctx, AzureEnv(self.ctx, self, vm))

    # --------------------------------------------------------------- down

    def down(self) -> None:
        from .azure_down import AzureDown

        with self.ctx.step("identity"):
            self.identity()
        st = self.ctx.state
        st.setdefault("prefix", prefix_for(self.ctx.run_id))
        st.setdefault("rg", f"rg-ofreal-{self.ctx.run_id}")
        AzureDown(self).down()


def captured(ctx: Context) -> dict[str, str]:
    """What portal steps captured (an enrolment code), from the run's secrets."""
    return {k.split(":", 1)[1]: v for k, v in ctx.secrets.items() if k.startswith("captured:")}


def flatten_outputs(outs: dict[str, Any], name_prefix: str | None) -> dict[str, Any]:
    """main.bicep's outputs as the harness and the checks use them: the scalar ones as they
    are, the objects (storageAccounts, identityPrincipalIds) as one key each, and web's URL
    when the template has no webUrl output: https://<namePrefix>-web.<the environment's domain>."""
    flat = {k: v for k, v in outs.items() if isinstance(v, str | int | bool)}
    acc = outs.get("storageAccounts") or {}
    ids = outs.get("identityPrincipalIds") or {}
    worm = list((acc.get("worm") or {}).values())
    flat.update(
        {
            "liveAccount": acc.get("live", ""),
            "archiveAccount": acc.get("archive", ""),
            "wormAccount": worm[0] if worm else "",
            "webPrincipalId": ids.get("web", ""),
            "workerPrincipalId": ids.get("worker", ""),
            "postgresFqdn": outs.get("postgresHost", ""),
        }
    )
    if name_prefix:
        flat["namePrefix"] = name_prefix
        if outs.get("internalEnvironmentDefaultDomain") and "webUrl" not in flat:
            flat["webUrl"] = f"https://{name_prefix}-web.{outs['internalEnvironmentDefaultDomain']}"
    return flat


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
