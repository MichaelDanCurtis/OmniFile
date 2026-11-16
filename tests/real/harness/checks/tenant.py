"""docs/milestones/real-tenant-checks.md, line by line (RT-nn).

Graph checks call Graph with the owner's own az sign-in (`az rest`), as the service would
with its identity. What only a person can see (a card on a phone) is a manual step.
Lines proven by the Azure run's exit (the portal's real Entra sign-in, WORM's fresh
sign-in) name that run's step as `azure:<step>`.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

from . import CheckFailed, Pending, auto, by_exit, manual

GRAPH = "https://graph.microsoft.com/v1.0"


class TenantEnv:
    def __init__(self, ctx, tenant) -> None:
        self.ctx, self.tenant = ctx, tenant

    def graph(self, path: str, method: str = "get", body: Any = None) -> Any:
        args = ["rest", "--method", method, "--url", f"{GRAPH}{path}"]
        if body is not None:
            args += ["--body", json.dumps(body)]
        return self.ctx.az(*args)

    @property
    def user(self) -> str:
        return self.tenant.al.one("m365-test-user")

    @property
    def azure(self) -> dict[str, Any]:
        return self.tenant.azure_state()


# -------------------------------------------------------------------- M4 exit

by_exit("RT-01", "a real Teams user gets a card for their own stub read", "card-seen")
by_exit(
    "RT-02", "clicking Recall reaches the relay, queues a recall, the card follows", "card-follows"
)
by_exit(
    "RT-03",
    "an admin edits a policy and sees the cost update (real Entra app roles)",
    "policy-cost",
)

# ----------------------------------------------------------- Teams, Bot Framework

manual(
    "RT-04",
    "Teams calls the relay only from the AzureBotService ranges",
    "a week of clicks",
    [
        "Keep a deployment up a week; every card click reaches the relay (no ingress 403s in the "
        "Container Apps logs); note the source addresses.",
        "Across a Microsoft range update: rerun relay-ranges.sh, redeploy, nothing dropped.",
        "Try --regional; count the prefixes against the ingress rule limit.",
        "Compare a real `az network list-service-tags` with deploy/azure/testdata/service-tags.json.",
    ],
)
manual(
    "RT-05",
    "proactive install under the tenant's app policies, and a blocked install",
    "a tenant whose Teams admin can change app permission policies",
    [
        "Block the app for the test user in a permission policy; trigger a card; record the "
        "status and error code Graph returns, and that the email fallback fires on it.",
        "Check TeamsAppInstallation.ReadWriteSelfForUser.All reads the installation's chat.",
    ],
)
by_exit("RT-06", "the service URL that worked for proactive messages", "card-sent")
by_exit("RT-07", "a user-assigned managed-identity bot gets Bot Connector tokens", "card-sent")
manual(
    "RT-08",
    "card send and update in place: days later, rate, 20 KB on every client",
    "days, and the desktop, web and mobile clients",
    [
        "Update a card sent two days earlier (PUT activities/{id}).",
        "Send 7 updates a second to one conversation; record any 429.",
        "Open a 20 KB card in Teams desktop, web and mobile; screenshot each.",
    ],
)
by_exit("RT-09", "the invoke from a real click carries the fields the relay checks", "click-recall")
manual(
    "RT-10",
    "the invoke response replaces the card; the update 6 s later lands after it",
    "a person on desktop and on mobile",
    [
        "Click Recall on desktop and on mobile; watch the card change at once and again ~6 s "
        "later; record screenshots.",
    ],
)
manual(
    "RT-11",
    "Action.Execute on Teams mobile and in the new Teams client",
    "a phone",
    [
        "Click each card button on Teams mobile and in the new Teams desktop client; record results.",
    ],
)
manual(
    "RT-12",
    "a user who removes the app or blocks the bot",
    "the test user's Teams",
    [
        "Remove the app as the test user; trigger a card; record Bot Connector's answer, the "
        "re-install, and the email after a second failure.",
    ],
)

# ------------------------------------------------------------------------- Graph


def _sid(env: TenantEnv) -> str:
    u = env.graph(f"/users/{quote(env.user)}?$select=id,onPremisesSecurityIdentifier")
    sid = (u or {}).get("onPremisesSecurityIdentifier")
    if not sid:
        raise CheckFailed(f"{env.user} has no onPremisesSecurityIdentifier: not synced from AD")
    return sid


@auto("RT-13", "$filter on onPremisesSecurityIdentifier works as a plain filter, and its latency")
def rt13(env: TenantEnv):
    sid = _sid(env)
    t0 = time.monotonic()
    got = env.graph(f"/users?$filter=onPremisesSecurityIdentifier eq '{sid}'&$select=id")
    ms = round((time.monotonic() - t0) * 1000)
    if len((got or {}).get("value", [])) != 1:
        raise CheckFailed(f"the plain filter found {len((got or {}).get('value', []))} users")
    return {"sid": sid, "latency_ms": ms, "consistency_level": "none"}


@auto("RT-14", "transitiveMemberOf returns onPremisesSecurityIdentifier for synced groups")
def rt14(env: TenantEnv):
    got = env.graph(
        f"/users/{quote(env.user)}/transitiveMemberOf/microsoft.graph.group"
        "?$select=id,displayName,onPremisesSecurityIdentifier,onPremisesSyncEnabled"
    )
    synced = [g for g in (got or {}).get("value", []) if g.get("onPremisesSyncEnabled")]
    if not synced:
        raise CheckFailed("the test user is in no synced group")
    bad = [g["displayName"] for g in synced if not g.get("onPremisesSecurityIdentifier")]
    if bad:
        raise CheckFailed(f"synced groups without a SID: {bad}")
    return {"synced_groups": len(synced)}


manual(
    "RT-15",
    "users whose primary group isn't Domain Users",
    "the client's on-prem AD",
    [
        "On the client's AD: `Get-ADUser -Filter 'primaryGroupID -ne 513'`; record the count and "
        "whether any of them need share access by their primary group.",
    ],
)
manual(
    "RT-16",
    "group directory sync completes within the nightly window",
    "the client's directory size",
    [
        "Time the worker's nightly group sync in its logs at the client's group count.",
    ],
)
manual(
    "RT-17",
    "sendMail limited to the service mailbox",
    "Exchange RBAC for Applications",
    [
        "Scope Mail.Send to the service mailbox (RBAC for Applications); send as it: works; as "
        "any other mailbox: 403.",
    ],
)


@auto("RT-18", "admin consent: the worker identity holds its four Graph app roles")
def rt18(env: TenantEnv):
    oid = env.azure.get("outputs", {}).get("workerPrincipalId")
    if not oid:
        raise Pending("no worker identity in the Azure run's outputs")
    graph_sp = env.graph(
        "/servicePrincipals?$filter=appId eq '00000003-0000-0000-c000-000000000000'"
        "&$select=id,appRoles"
    )["value"][0]
    names = {r["id"]: r["value"] for r in graph_sp["appRoles"]}
    got = env.graph(f"/servicePrincipals/{oid}/appRoleAssignments")
    have = {names.get(a["appRoleId"]) for a in (got or {}).get("value", [])}
    want = {
        "User.Read.All",
        "GroupMember.Read.All",
        "TeamsAppInstallation.ReadWriteSelfForUser.All",
        "Mail.Send",
    }
    if want - have:
        raise CheckFailed(f"missing app roles: {sorted(want - have)}")
    return {"roles": sorted(have & want)}


# ------------------------------------------------------------------ Entra sign-in

by_exit("RT-19", "the groups claim carries on-premises group SIDs (seen in /v1/me)", "policy-cost")
by_exit("RT-20", "the onprem_sid claim is present for the synced user", "policy-cost")
manual(
    "RT-21",
    "group overage above 200 groups",
    "a user in more than 200 groups",
    [
        "Sign in to the portal as a user in 201+ groups; /v1/me lists groups from the worker's "
        "Graph lookup (the token carries _claim_names).",
    ],
)
manual(
    "RT-22",
    "app roles through AD-synced groups, nested ones too",
    "nested synced groups",
    [
        "Assign an app role to a synced group with a nested group; sign in as a nested member; "
        "record whether `roles` has it (Entra doesn't expand nesting for app roles).",
    ],
)
by_exit(
    "RT-23",
    "web's managed identity redeems the code with a federated client assertion",
    "azure:edge-enrolled",
)
by_exit(
    "RT-24",
    "prompt=login with max_age=0 gives a fresh auth_time that WORM enable accepts",
    "azure:worm-enable",
)


@auto("RT-25", "a partner client-credentials token: idtyp=app, v2 issuer, our audience, its roles")
def rt25(env: TenantEnv):
    app = env.ctx.state.get("partner_app")
    if not app:
        raise Pending("the partner probe app was not made (run up)")
    from ..pki import leaf

    cert = leaf(env.ctx, "partner-probe", f"omnifile-partner-probe-{env.ctx.run_id}", None)
    vm = env.azure.get("vm", {}).get("ip")
    if not vm:
        raise Pending("the Azure run has no test VM to reach web from")
    cfg = {
        "tenant": env.ctx.state["identity"]["tenant"],
        "client_id": app["appId"],
        "cert": cert["cert"],
        "key": cert["key"],
        "web": env.azure.get("outputs", {}).get("webUrl"),
    }
    key = env.ctx.out.parent / env.azure["run_id"] / "ssh" / "id_ed25519"
    p = env.ctx.run(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            f"omnitest@{vm}",
            "/srv/ofreal/venv/bin/python /srv/ofreal/vm/azure_checks.py RT-25",
        ],
        input=json.dumps({"args": cfg}),
    )
    r = json.loads(p.stdout.strip().splitlines()[-1])
    if not r.get("ok"):
        raise CheckFailed(r.get("error", "partner token refused"))
    return r.get("evidence", {})


manual(
    "RT-26",
    "session cookies on the private ingress, and the first navigation from Teams",
    "a card link clicked in Teams",
    [
        "In the portal's dev tools: the session cookie is __Host-, Secure, SameSite=Strict.",
        "Click 'Open in portal' on a card: record whether the first navigation keeps the session.",
    ],
)

# --------------------------------------------------------------- private network

manual(
    "RT-27",
    "download links reach the blob private endpoint from the client's LAN",
    "a browser on the client's network",
    [
        "From a desktop on the client's LAN, start a portal download; record whether the "
        "private endpoint's name resolves and the download completes.",
    ],
)
manual(
    "RT-28",
    "'Open in portal' links from Teams, inside and outside the network",
    "the client's network and a phone off it",
    [
        "Click 'Open in portal' on the client's network (opens) and off it (fails cleanly).",
    ],
)

# --------------------------------------------------------- Teams track assumptions

by_exit("RT-29", "Graph's chat id is the Bot Connector conversation id", "card-sent")
by_exit("RT-30", "installedApps with $expand and $filter on the catalog id", "card-sent")


@auto("RT-31", "transitiveMembers of a group returns nested users with their SIDs")
def rt31(env: TenantEnv):
    groups = env.graph(f"/users/{quote(env.user)}/memberOf/microsoft.graph.group?$select=id")
    ids = [g["id"] for g in (groups or {}).get("value", [])]
    if not ids:
        raise CheckFailed("the test user is in no group")
    got = env.graph(
        f"/groups/{ids[0]}/transitiveMembers/microsoft.graph.user"
        "?$select=id,onPremisesSecurityIdentifier"
    )
    users = (got or {}).get("value", [])
    if not any(u.get("onPremisesSecurityIdentifier") for u in users):
        raise CheckFailed("no member came back with a SID")
    return {"group": ids[0], "members": len(users)}


for _cid, _title, _steps in (
    (
        "RT-32",
        "card text escaping renders as plain characters",
        [
            "Make a file named `a_b.c [x](https://example.com).segy`, read its stub, and check "
            "the card on desktop, web and mobile: plain text, no link."
        ],
    ),
    (
        "RT-33",
        "the activity summary is the banner and chat-list text",
        ["Trigger a card and its update; screenshot the banner and the chat list."],
    ),
    (
        "RT-34",
        "the bot's 50 sends a second, and Retry-After on a 429",
        [
            "Send 60 cards a second to 60 test users for 10 s (a test tenant only); record 429s "
            "and their Retry-After."
        ],
    ),
    (
        "RT-35",
        "Bot Connector's 403 for an uninstalled app, 404 for a deleted conversation",
        [
            "Uninstall the app as the test user and send: record the status; delete the chat "
            "and send: record the status."
        ],
    ),
    (
        "RT-36",
        "api sees the relay's certificate through ingress, and refuses it on edge paths",
        [
            "After a click, api's log shows the relay identity on the Teams path; replay the "
            "relay's certificate to an edge path from the relay's container: refused."
        ],
    ),
):
    manual(_cid, _title, "the tenant and a person", _steps)

by_exit(
    "RT-37", "auth_time in the ID token after prompt=login is that sign-in's", "azure:worm-enable"
)

# --------------------------------------------------------- M4 integration stand-ins

manual(
    "RT-38",
    "the relay fetches Microsoft's Bot Framework keys, and they rotate",
    "a key rotation by Microsoft",
    [
        "The tenant run's click proves the fetch; keep the relay up across a rotation "
        "(weeks) and record that clicks keep working without a restart.",
    ],
)


@auto("RT-39", "the relay's and api's clocks agree within the 10-minute window")
def rt39(env: TenantEnv):
    az = env.azure
    if not az.get("rg"):
        raise Pending("no Azure run")
    times = {}
    for app in ("relay", "api"):
        pre = az.get("outputs", {}).get("namePrefix", az["prefix"])
        out = env.ctx.run(
            [
                "az",
                "containerapp",
                "exec",
                "-n",
                f"{pre}-{app}",
                "-g",
                az["rg"],
                "--command",
                "date -u +%s",
            ],
            check=True,
        ).stdout
        times[app] = int(out.strip().splitlines()[-1])
    skew = abs(times["relay"] - times["api"])
    if skew > 60:
        raise CheckFailed(f"relay and api differ by {skew} s")
    return {"skew_s": skew}


by_exit("RT-40", "the edge's stub read from a real AD user produces the user's SID", "stub-read")
by_exit(
    "RT-41",
    "TLS in front of web: the browser's Origin and the callback host are right",
    "azure:edge-enrolled",
)
manual(
    "RT-42",
    "the portal in Edge and Safari, and the link from Teams' in-app browser",
    "the client's managed desktops",
    [
        "Open the portal in Edge and Safari; sign in; open a file and a recall.",
        "Open 'Open in portal' from Teams desktop and mobile.",
    ],
)
manual(
    "RT-43",
    "the session sweep on two clocks",
    "nothing: NTP only (listed for completeness)",
    [
        "Nothing to run: record pass once RT-39 has passed.",
    ],
)

manual(
    "RT-44",
    "the card to a pin's setter when another file's date moved the blob",
    "one card, or one mail, in the tenant",
    [
        "Share one blob between two files, pin one Hot as a real user, and take the worker's "
        "write rights off the storage account.",
        "Let the other file's archive date come. The pinned file goes to Archive.",
        "Confirm the setter gets one Teams card with no buttons, saying the pin wasn't kept. "
        "It opens the portal, and names no other file.",
        "Block the app for that user and repeat: one mail instead.",
    ],
)

manual(
    "RT-45",
    "the card for a recall whose file was replaced or deleted",
    "the requester's card updated in place, or one mail, in the tenant",
    [
        "As a real AD user, open an archived file on a share; wait for the card.",
        "Before the recall ends, overwrite the file from another client. The card "
        "becomes '<name> was replaced', with one sentence and only 'Open in portal'; "
        "the earlier Keep online button is gone.",
        "Repeat, deleting the file over SMB as another AD user: the card says "
        "'<name> was deleted' and 'Deleted by <display name>'.",
        "Repeat, deleting it over NFS sec=sys: the card names no one.",
        "Block the app for the requester and repeat once: one mail instead.",
    ],
)
