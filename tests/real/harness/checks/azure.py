"""docs/milestones/real-azure-checks.md, line by line (RA-nn).

Automated checks that need the VNet (the storage accounts and the internal environment are
private) run on the test VM: tests/real/vm/azure_checks.py, with the VM's managed identity.
The rest use az from the workstation, through Azure's management plane.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from . import CheckFailed, Partial, Pending, auto, by_exit, manual


class AzureEnv:
    def __init__(self, ctx, azure, vm) -> None:
        self.ctx, self.azure, self.vm = ctx, azure, vm

    @property
    def rg(self) -> str:
        return self.ctx.state["rg"]

    @property
    def prefix(self) -> str:
        return self.ctx.state["prefix"]

    def az(self, *args: str, check: bool = True) -> Any:
        return self.ctx.az(*args, check=check)

    def on_vm(self, cid: str, **args: Any) -> dict[str, Any]:
        """tests/real/vm/azure_checks.py CID on the test VM; one JSON line back."""
        if not self.ctx.state.get("vm", {}).get("ready"):
            raise Pending("the test VM is not up (run up first)")
        cfg = json.dumps(
            {
                "rg": self.rg,
                "prefix": self.prefix,
                "args": args,
                "outputs": self.ctx.state.get("outputs", {}),
                "facts": self.ctx.state.get("exit_facts", {}),
            }
        )
        out = self.vm.sh(
            f"sudo -u omnitest /srv/ofreal/venv/bin/python /srv/ofreal/vm/azure_checks.py "
            f"{cid}",
            input=cfg,
            tag=f"check-{cid}",
        )
        rows = self.vm.json_lines(out)
        if not rows:
            raise CheckFailed(f"{cid}: no result from the VM")
        r = rows[-1]
        if r.get("status") == "partial":
            raise Partial(r.get("left", "see evidence"), r.get("evidence"))
        if not r.get("ok"):
            raise CheckFailed(r.get("error", "failed on the VM"))
        return r.get("evidence", {})


# ----------------------------------------------------------- storage behaviour


@auto("RA-01", "a bad x-ms-content-crc64 on Put Block is refused, and its error code recorded")
def ra01(env: AzureEnv):
    return env.on_vm("RA-01")


@auto("RA-02", "change-feed records map to the service's ChangeKind")
def ra02(env: AzureEnv):
    return env.on_vm("RA-02")


@auto("RA-03", "a rehydrate copy from the LRS archive account into the ZRS live account")
def ra03(env: AzureEnv):
    return env.on_vm("RA-03")


manual(
    "RA-04",
    "Smart tier with versioning and immutability",
    "pilot 4 storage accounts",
    [
        "Create a ZRS account with versioning and version-level immutability, default tier Smart "
        "(API 2025-08-01), in the run's resource group (tag it with the run id).",
        "Upload a blob without a tier; after a day read its tier and `x-ms-access-tier-inferred`.",
        "Record whether Smart applies; if it does, Smart may be offered for WORM shares (decision 31).",
    ],
)
manual(
    "RA-05",
    "Set Blob Tier on locked version-level WORM blobs, and early deletion",
    "a locked policy, which no run can remove before it expires",
    [
        "In a separate account kept outside the harness, lock a 1-day version-level policy on "
        "one blob version.",
        "Set Blob Tier to Archive, then back to Hot (rehydrate); record both answers.",
        "Read the next invoice's early-deletion line for that blob.",
    ],
)
manual(
    "RA-06",
    "real recall times for 8 GB, 184 GB and 1.2 TB at Standard and High",
    "pilot 4 (hours to a day per file, and the data)",
    [
        "Archive three files of 8 GB, 184 GB and 1.2 TB through a share.",
        "Recall each at Standard and at High; record the times from request to available.",
        "Put the times in the portal's ETA table (never promise above 10 GB, AGENTS.md).",
    ],
)


@auto("RA-07", "change-feed lag under a burst of writes")
def ra07(env: AzureEnv):
    return env.on_vm("RA-07", blobs=500)


@auto("RA-08", "a listing with include=versions returns each version's id, tier and length")
def ra08(env: AzureEnv):
    return env.on_vm("RA-08")


@auto("RA-09", "deleting the current version by its version id")
def ra09(env: AzureEnv):
    return env.on_vm("RA-09")


@auto(
    "RA-10",
    "archive copy: Copy Blob live->archive lands Hot, CRC64 read-back, Set Blob Tier Archive",
)
def ra10(env: AzureEnv):
    return env.on_vm("RA-10", size_mib=1024)


@auto("RA-11", "Get Block List with versionid returns that version's committed blocks")
def ra11(env: AzureEnv):
    return env.on_vm("RA-11")


@auto("RA-12", "a forged X-Forwarded-Client-Cert from the VNet authenticates nobody at api")
def ra12(env: AzureEnv):
    return env.on_vm("RA-12")


@auto("RA-13", "real Azure checks what Floci doesn't: a tampered SAS and a bad CRC64 are refused")
def ra13(env: AzureEnv):
    return env.on_vm("RA-13")


# ------------------------------------------------------------------ Storage Mover

for _cid, _title in (
    ("RA-14", "Storage Mover Additive mode with a changed source"),
    ("RA-15", "unrestored Glacier objects in a Storage Mover prefix"),
    ("RA-16", "Storage Mover writes a private-only target account"),
    ("RA-17", "Storage Mover run status through azure-mgmt-storagemover"),
):
    manual(
        _cid,
        _title,
        "an Azure run and an AWS run both up, and an Arc multicloud connector "
        "(its CloudFormation stack runs in the AWS account)",
        [
            "Bring up the Azure run and the AWS run (run.sh up for each).",
            "Create a Storage Mover, a project and the multicloud connector to the AWS run's "
            "account, a source endpoint on the AWS run's source bucket and a target endpoint "
            "on the live account's container; re-deploy with storageMoverConfig "
            "(deploy/azure/README.md step 10).",
            "Start a migration of `seed/` in the portal and follow it on the Migrations page.",
            f"For {_cid}: record what the line asks, with the job run's JSON from "
            "`az storage-mover job-run show`.",
        ],
    )
manual(
    "RA-18",
    "the verifier's read rate per replica",
    "a migration of at least 100 GB",
    [
        "Run a verification of a 100 GB prefix (Migrations, Verify).",
        "Read `omnifile_verify_bytes` per replica in Application Insights; record GB/h per replica.",
    ],
)

# ---------------------------------------------------------------- hosting, identity


by_exit(
    "RA-19",
    "X-Forwarded-Client-Cert from Container Apps ingress: the edge's certificate "
    "is read, a client's copy is stripped",
    "edge-enrolled",
)


manual(
    "RA-20",
    "the Entra audience for api -> enrol sign, and the app-to-app source address",
    "an edge certificate renewal, which comes at two thirds of its 30 days",
    [
        "On the appliance, shorten the renewal threshold (edge.toml `renew_before`) and "
        "restart the daemon, or wait.",
        "In Log Analytics: enrol's access log for POST /internal/v0/sign; record the caller's "
        "source address and that the token's aud was enrol's client id.",
    ],
)
manual(
    "RA-21",
    "the Bot Framework token's tenant and endorsement on a real click",
    "a tenant run (RT-09 proves the same click)",
    [
        "Run the tenant run's exit (its click-recall step).",
        "Record pass here with the tenant run's evidence for RT-09: the relay's log of the "
        "token's tid, serviceUrl and msteams endorsement.",
    ],
)
manual(
    "RA-22",
    "forwarded-allow-ips for the relay",
    "a Teams click (tenant run)",
    [
        "During the tenant run's click, read the relay's access log: the client address it logs must "
        "be Microsoft's, not the ingress proxy's; record the proxy's source range.",
    ],
)
manual(
    "RA-23",
    "the relay reaches api only with the chain in relay-ca-bundle",
    "a tenant run (a real Bot Framework token is the only way in)",
    [
        "With the tenant run's card, click Recall: the forward succeeds.",
        "Replace relay-ca-bundle with an unrelated CA, restart the relay revision, click again: "
        "the relay's log shows the TLS failure and the card says it could not reach the service.",
        "Put the right bundle back.",
    ],
)


@auto("RA-24", "what-if against a real subscription resolves the template and parameters")
def ra24(env: AzureEnv):
    ev = env.ctx.evidence / "whatif.txt"
    if not ev.exists():
        raise Pending("no what-if in this run")
    text = ev.read_text()
    if "digest: sha256:" not in text:
        raise CheckFailed("the what-if has no digest")
    return {"whatif": "evidence/whatif.txt", "digest": env.ctx.state.get("digest")}


manual(
    "RA-25",
    "the catalog restore drill on Azure",
    "about two hours of a running deployment",
    [
        "Follow deploy/azure/README.md, Catalog backup and restore, steps 1 to 6, on the run's server.",
        "Record each step's command and output, and that an edge re-snapshots.",
    ],
)
by_exit(
    "RA-26", "the Key Vault CA signer: init-kv-ca, enrol starts, an edge enrols", "edge-enrolled"
)
manual(
    "RA-27",
    "the custom storage roles are enough and no more",
    "the deployment",
    [
        "Mint a SAS as api would (`az containerapp exec -n <prefix>-api`, the service's mint "
        "command) and try a delete through it: refused.",
        "As the worker's identity, get a user-delegation key and open a portal download link.",
    ],
)


@auto("RA-28", "per-app database privileges on Azure Database for PostgreSQL")
def ra28(env: AzureEnv):
    return env.on_vm("RA-28", token=_pg_token(env))


@auto("RA-29", "each app signs in as its own role, and a fresh connection after an hour works")
def ra29(env: AzureEnv):
    ev = env.on_vm("RA-29", token=_pg_token(env))
    if not ev.get("after_an_hour"):
        raise Partial(
            "a connection opened more than an hour after start: rerun checks after " "an hour", ev
        )
    return ev


def _pg_token(env: AzureEnv) -> str:
    """The owner's own Entra token for the database (the owner is in the run's PG admin group).
    It goes to the VM on stdin, never on a command line, and lasts an hour."""
    t = env.az("account", "get-access-token", "--resource-type", "oss-rdbms")
    return t["accessToken"]


# Runs inside api's own replica (az containerapp exec): the T5 probe. It connects to api's TCP
# ingress (8443, apiUrl) twice, without a client certificate: once plainly, once with a forged
# X-Forwarded-Client-Cert. Direct TLS must refuse both, at the handshake or with a 4xx. Any
# container in the internal subnet will do as the peer; api's is used because it always has a
# replica, while web can scale to zero at deploymentSize=small and exec would find none.
T5_PROBE = r"""
import json, socket, ssl, sys, urllib.parse
u = urllib.parse.urlsplit(sys.argv[1])
host, port = u.hostname, u.port or 443
def attempt(headers):
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=15) as raw:
            with c.wrap_socket(raw, server_hostname=host) as t:
                req = "GET /v0/edges/self HTTP/1.1\r\nHost: %s\r\n%sConnection: close\r\n\r\n" % (
                    host, "".join("%s: %s\r\n" % kv for kv in headers.items()))
                t.sendall(req.encode())
                first = t.recv(4096).decode(errors="replace").split("\r\n", 1)[0]
                return {"tls": "accepted", "status": int(first.split()[1]) if first else 0}
    except ssl.SSLError as e:
        return {"tls": "refused", "error": type(e).__name__}
    except (ConnectionError, OSError) as e:
        return {"tls": "refused", "error": type(e).__name__}
print("ofreal-t5-probe " + json.dumps({
    "no_client_cert": attempt({}),
    "forged_xfcc": attempt({"X-Forwarded-Client-Cert": "Hash=00;Cert=forged;Subject=CN=omnifile-edge-victim"}),
}))
"""


def _refused(r: dict) -> bool:
    return r.get("tls") == "refused" or 400 <= int(r.get("status", 0)) < 500


@auto(
    "RA-30",
    "from inside the internal subnet (api's own replica), api's TCP ingress refuses a peer "
    "without an edge certificate and a forged XFCC",
)
def ra30(env: AzureEnv):
    outs = env.ctx.state.get("outputs", {})
    api = outs.get("apiUrl") or f"https://{env.prefix}-api:8443"
    # api, not web: web may have no replica to exec into (scale to zero at small)
    peer = f"{outs.get('namePrefix', env.prefix)}-api"
    code = base64.b64encode(T5_PROBE.encode()).decode()
    cmd = (
        f"python -c \"import base64,sys;sys.argv=['t5','{api}'];"
        f"exec(base64.b64decode('{code}'))\""
    )
    out = env.ctx.run(
        ["az", "containerapp", "exec", "-n", peer, "-g", env.rg, "--command", cmd]
    ).stdout
    line = next((ln for ln in out.splitlines()[::-1] if ln.startswith("ofreal-t5-probe ")), None)
    if line is None:
        raise CheckFailed(f"no answer from the probe in {peer}: {out[-300:]}")
    got = json.loads(line.split(" ", 1)[1])
    bad = {k: v for k, v in got.items() if not _refused(v)}
    if bad:
        raise CheckFailed(f"api let in a caller without an edge certificate: {bad}")
    if not api.rstrip("/").endswith(":8443") and outs.get("apiIngress", "tcp") == "tcp":
        raise Partial(
            "refused, but apiUrl doesn't name the TCP port 8443: check apiIngress",
            {"answers": got, "api": api},
        )
    return {"api": api, "peer": peer, "answers": got}


# ------------------------------------------------------------- not real-Azure

manual(
    "RA-31",
    "the *_dlq queues are in a catalog migration",
    "nothing: done in M3",
    [
        "Done locally in M3 (migration 0008); record pass with the migration's path.",
    ],
)


@auto("RA-32", "an archive copy's CRC64 is confirmed from the copy's properties")
def ra32(env: AzureEnv):
    return env.on_vm("RA-32")


@auto("RA-33", "the WORM share's container policy was set through ARM by the service")
def ra33(env: AzureEnv):
    accts = env.az("storage", "account", "list", "-g", env.rg) or []
    worm = [
        a["name"] for a in accts if (a.get("immutableStorageWithVersioning") or {}).get("enabled")
    ]
    if not worm:
        raise CheckFailed("no WORM account in the run's resource group")
    pol = env.az(
        "storage",
        "container",
        "immutability-policy",
        "show",
        "-g",
        env.rg,
        "--account-name",
        worm[0],
        "-c",
        "data",
        check=False,
    )
    if not pol or not pol.get("state"):
        raise CheckFailed(f"{worm[0]}/data has no container policy")
    return {"account": worm[0], "policy": pol}


manual(
    "RA-34",
    "the Blob Inventory report reader",
    "a day: inventory runs daily",
    [
        "Keep a run up for 24 h (--ttl-hours 30), or read a report in pilot 4.",
        "Run reconcile on the live account; record that it read the inventory report, not a listing.",
    ],
)

# ---------------------------------------------------------------------- monitoring


@auto("RA-35", "the service's metrics reach AppMetrics in Application Insights")
def ra35(env: AzureEnv):
    app = env.ctx.state.get("outputs", {}).get("applicationInsightsId")
    if not app:
        raise Pending("no Application Insights output in the deployment")
    rows = env.az(
        "monitor",
        "app-insights",
        "query",
        "--apps",
        app,
        "--analytics-query",
        "AppMetrics | where TimeGenerated > ago(2h) | summarize n=count() by Name",
    )
    names = {r[0] for t in rows.get("tables", []) for r in t.get("rows", [])}
    want = {"omnifile_api_requests", "omnifile_queue_depth"}
    if not want & names:
        raise CheckFailed(f"no OmniFile metric in AppMetrics (saw {sorted(names)[:10]})")
    return {"metrics": sorted(names)}


manual(
    "RA-36",
    "the alert queries match the exporter and each rule fires once",
    "forcing eight conditions on a live deployment",
    [
        "For each rule in `az monitor scheduled-query list -g <rg>`: run its query in Log "
        "Analytics against the last hour; it must parse and return the exporter's fields.",
        "Force each condition (docs/design/observability.md section 4 names them) and record the "
        "alert firing once.",
    ],
)
manual(
    "RA-37",
    "private ingestion through an Azure Monitor Private Link Scope",
    "a deployment with monitorPrivateLink = true",
    [
        "Deploy a run with monitorPrivateLink = true (an override of test.bicepparam).",
        "Check AppMetrics still fills, and that the ingestion endpoint resolves to a private "
        "address from the VNet.",
    ],
)
manual(
    "RA-38",
    "the Azure Monitor Agent on an Arc-connected appliance",
    "an appliance outside Azure (Arc won't onboard an Azure VM or its nested guests)",
    [
        "Connect the laptop appliance to Arc (docs/design/appliance-image.md), add its machine "
        "id to edgeArcMachineIds, and re-deploy.",
        "Check the daemon's journal lines arrive in the workspace under the data collection rule.",
    ],
)

# ------------------------------------------------------------------ release signing

manual(
    "RA-39",
    "the update key in Key Vault Premium signs a feed an appliance accepts",
    "the owner's release Key Vault and pipeline identity",
    [
        "As the release identity: `az keyvault key show` (EC-HSM, P-256), then "
        "`appliance/release.sh --signer kv:<key id>` on a test feed.",
        "appliance/tests/signed-update.sh with that feed; the vault log shows each sign.",
    ],
)
manual(
    "RA-40",
    "the Secure Boot key through e_akv",
    "the owner's release vault and host",
    [
        "`sbsign --engine e_akv --key vault:<vault>:<key>` on a UKI; `sbverify` and OVMF accept it.",
    ],
)
manual(
    "RA-41",
    "GitHub Releases from a box through a client proxy",
    "the public feed repository and a first release",
    [
        "On an appliance behind a proxy, `systemd-sysupdate check-new` against "
        "releases/latest/download/SHA256SUMS; record the redirects it followed.",
    ],
)
manual(
    "RA-42",
    "a Secure Boot key rotation on a box without the new certificate",
    "WP2's local OVMF test (no real account)",
    [
        "Run WP2's key-rotation test (appliance/tests/, OVMF) and record its result here.",
    ],
)


# ------------------------------------------------------------------ Connect Azure (WP19)

manual(
    "RA-43",
    'the delegated ARM consent and Connect Azure"s sign-in',
    "a real tenant and an ArchiveAdmin",
    [
        "Run register.sh, then Settings, Azure, Sign in to Azure as an ArchiveAdmin; record the consent prompt and that the listing came back (last_op done).",
    ],
)

manual(
    "RA-44",
    "the permissions answer on a resource group",
    "a subscription with Owner, UAA and Contributor users",
    [
        "GET <rg>/providers/Microsoft.Authorization/permissions as each; record actions and notActions.",
    ],
)

manual(
    "RA-45",
    "a new custom role and its assignment at once",
    "a subscription the run owns",
    [
        'Connect Azure"s grant on a fresh resource group; record any RoleDefinitionDoesNotExist and the retries.',
    ],
)

manual(
    "RA-46",
    "discovery with the Discovery Reader only",
    "a deployment with a granted resource group",
    [
        "Press Discover; compare the page with `az storage account list` and `az network private-endpoint list`.",
    ],
)

manual(
    "RA-47",
    "the worker deploys Storage Mover with the Mover Deployer role",
    "a granted Storage Mover group",
    [
        'Press Deploy Storage Mover; record the task"s outcome and `az storage-mover show`.',
    ],
)

manual(
    "RA-48",
    'the Storage Mover agent"s download and registration',
    "an on-premises Hyper-V or VMware host",
    [
        'Follow the page"s agent steps; record the console"s Register prompts and the agent going Online.',
    ],
)

manual(
    "RA-49",
    "web reads a deployed mover with its Discovery Reader",
    "the mover of RA-47",
    [
        "Open Settings, Storage Mover before OMNIFILE_STORAGE_MOVER is set; record the agents shown.",
    ],
)

manual(
    "RA-50",
    "OMNIFILE_SERVICE_PRINCIPAL_IDS names web, and Storage checks ignore it",
    "a deployment",
    [
        'Run a backend"s checks; record that rbac_role lists worker, api and verifier only.',
    ],
)

manual(
    "RA-51",
    "removing a grant deletes the assignment and the role",
    "the grants of RA-45",
    [
        "Remove a grant on Settings, Azure; record `az role assignment list` and `az role definition list` after.",
    ],
)

manual(
    "RA-52",
    "listing a tenant with many subscriptions",
    "a tenant with over 25 subscriptions",
    [
        "Sign in to Azure; record the time and that the listing says it was cut.",
    ],
)

manual(
    "RA-53",
    "a repeated Copy Blob while one is pending",
    "a blob big enough that its copy into the archive account takes minutes",
    [
        "Start the copy, then start it again while `copy.status` is pending; record the answer.",
        "Record the destination's length while pending, and that the worker's retry reaches the end.",
    ],
)

manual(
    "RA-54",
    "Copy Blob within one account, out of Cool and Cold",
    "a blob in Cool and one in Cold, past their minimums",
    [
        "Copy each to a new name in the same container, landing in Hot.",
        "Record that the copy is online, and that the source's tier and change time didn't move.",
    ],
)

manual(
    "RA-55",
    "Copy Blob out of a locked WORM version",
    "a container with a default version-level policy, and a version under a locked policy",
    [
        "Copy that version to a new name in the same container.",
        "Record the answer, and the new blob's retention. A refusal is the fallback's case.",
    ],
)

manual(
    "RA-56",
    "a copy into Archive within one account under version-level WORM",
    "the same container, while another copy of the same version is running",
    [
        "Land a copy in Hot, read it back in full and compare CRC64-NVME.",
        "Set Blob Tier to Archive on the copy. Record that the original and the other copy are untouched.",
    ],
)

manual(
    "RA-57",
    "retention and a legal hold on a copy's version",
    "a copy's version in a container with a default policy",
    [
        "Set Immutability Policy to a later date, and a legal hold, on that version.",
        "Record that both show in Get Blob Properties, and that the worker clears `worm_sync_needed`.",
    ],
)

manual(
    "RA-58",
    "does Azure accept a past retention date",
    "one unlocked and one locked version",
    [
        "Set Immutability Policy with a date in the past on each, by hand: OmniFile doesn't send one.",
        "Record the answers.",
    ],
)

manual(
    "RA-59",
    "what a split bills",
    "a split out of Cold of at least 1 GB, and the next billing export",
    [
        "Record the retrieval, Copy Blob's operations, and that no early deletion shows for the blob that stayed.",
        "Record the second blob's storage from that day, against the repo's prices.",
    ],
)

manual(
    "RA-60",
    "retiring a replaced row after retention ends",
    "a WORM file that split, with a retention short enough to end during the run",
    [
        "After retention ends, let the worker's `replaced_secondaries` sweep run.",
        "Record that the old version is deleted by id, or kept while another file uses it.",
    ],
)
