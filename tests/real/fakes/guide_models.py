"""The fake az's models of what WP7's customer guide (docs/guide) calls beyond the fixture,
and the dry-run stand-ins for the few other tools whose calls would reach a network.

Each model keeps state in the fake's state file and refuses what Azure refuses: a show of
something that doesn't exist is ResourceNotFound (exit 3), a link to a zone or VNet that
doesn't exist fails, a private endpoint needs a real target and the right group id, and a name
resolves privately only once its private endpoint, zone group and zone link exist. They are
models of the calls the guide makes, not of Azure.

Called from fakecli.py, which passes itself as F (fakecli runs as __main__).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: The groups Entra Connect syncs from the run's AD in a real tenant (the tenant's
#: prerequisite, docs/guide section 2); the fake tenant has them already.
SYNCED_GROUPS = (
    "OmniFile-ArchiveAdmins",
    "OmniFile-DataStewards",
    "Proj-Writers",
    "Proj-Readers",
    "filer admins",
)
GROUP_IDS = {"blob": "Microsoft.Storage/storageAccounts", "vault": "Microsoft.KeyVault/vaults"}
ZONE_OF = {"blob": "privatelink.blob.core.windows.net", "vault": "privatelink.vaultcore.azure.net"}


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "ofreal-fake:" + "/".join(parts)))


def not_found(F, what: str) -> None:
    F.die(f"(ResourceNotFound) The Resource '{what}' was not found.", 3)


def resource(s, F, rg: str | None, name: str, typ: str) -> dict | None:
    for r in s["resources"]:
        if r["name"] == name and r["type"] == typ and (rg is None or r["resourceGroup"] == rg):
            return r
    return None


def resource_by_id(s, rid: str) -> dict | None:
    return next((r for r in s["resources"] if r["id"].lower() == rid.lower()), None)


def params_of(opts) -> dict[str, Any]:
    """The compiled parameters a deployment was given (`--parameters @file`)."""
    for v in opts.get("--parameters", []):
        if v.startswith("@") and Path(v[1:]).is_file():
            try:
                return json.loads(Path(v[1:]).read_text()).get("parameters", {})
            except json.JSONDecodeError:
                return {}
    return {}


# ------------------------------------------------------------------------------- az


def az(F, s: dict, v: str, opts: dict) -> bool:
    """True when V is one of these models (it has run); False for fakecli to refuse it."""
    o = lambda *n: F.opt(opts, *n)  # noqa: E731
    rg = o("-g", "--resource-group")

    if v == "ad group show":
        key = o("--group", "-g")
        g = next((x for x in s["ad_groups"] if key in (x["id"], x["displayName"])), None)
        if g is None and key in SYNCED_GROUPS:
            g = {"id": stable_id("group", key), "displayName": key, "onPremisesSyncEnabled": True}
        if g is None:
            F.die(
                f"Resource '{key}' does not exist or one of its queried reference-property "
                "objects are not present.",
                3,
            )
        F.emit(g, opts)
    elif v == "ad group member check":
        gid, member = o("--group"), o("--member-id")
        g = next((x for x in s["ad_groups"] if gid in (x["id"], x["displayName"])), None)
        if g is None:
            F.die(f"Resource '{gid}' does not exist", 3)
        F.emit({"value": member in g.get("members", [])}, opts)
    elif v == "acr show":
        r = resource(s, F, rg, o("-n", "--name"), "Microsoft.ContainerRegistry/registries")
        if r is None:
            not_found(F, o("-n", "--name"))
        F.emit({"id": r["id"], "name": r["name"], "loginServer": f"{r['name']}.azurecr.io"}, opts)
    elif v == "acr import":
        name = o("-n", "--name")
        if resource(s, F, None, name, "Microsoft.ContainerRegistry/registries") is None:
            not_found(F, name)
        src = o("--source") or ""
        if "@sha256:" not in src:
            F.die("the guide imports by digest: --source must be <registry>/<repo>@sha256:...", 1)
        repo, digest = src.split("/", 1)[1].split("@", 1)
        s.setdefault("acr_images", {}).setdefault(name, {})[repo] = digest
        F.save(s)
        F.emit({}, opts)
    elif v == "acr repository show":
        name, image = o("-n", "--name"), o("--image") or ""
        repo, _, digest = image.partition("@")
        if s.get("acr_images", {}).get(name, {}).get(repo) != digest:
            F.die(f"(MANIFEST_UNKNOWN) manifest {image} is not found in {name}", 1)
        F.emit({"digest": digest, "name": repo}, opts)
    elif v == "containerapp list":
        F.emit(
            [
                {
                    "name": r["name"],
                    "properties": {"runningStatus": "Running", "provisioningState": "Succeeded"},
                }
                for r in s["resources"]
                if r["resourceGroup"] == rg and r["type"] == "Microsoft.App/containerApps"
            ],
            opts,
        )
    elif v == "containerapp revision list":
        name = o("-n", "--name")
        if resource(s, F, rg, name, "Microsoft.App/containerApps") is None:
            not_found(F, name)
        F.emit(
            [
                {
                    "name": f"{name}--rev1",
                    "properties": {
                        "active": True,
                        "healthState": "Healthy",
                        "runningState": "Running",
                    },
                }
            ],
            opts,
        )
    elif v == "identity show":
        name = o("-n", "--name")
        r = resource(s, F, rg, name, "Microsoft.ManagedIdentity/userAssignedIdentities")
        if r is None:
            not_found(F, name)
        F.emit(
            {
                "id": r["id"],
                "name": name,
                "clientId": stable_id("client", r["id"]),
                "principalId": stable_id("principal", r["id"]),
            },
            opts,
        )
    elif v == "keyvault show":
        name = o("-n", "--name")
        r = resource(s, F, rg, name, "Microsoft.KeyVault/vaults")
        if r is None:
            not_found(F, name)
        F.emit(
            {
                "id": r["id"],
                "name": name,
                "properties": {"vaultUri": f"https://{name}.vault.azure.net/"},
            },
            opts,
        )
    elif v == "keyvault secret show":
        vault, name = o("--vault-name"), o("-n", "--name")
        if resource(s, F, None, vault, "Microsoft.KeyVault/vaults") is None:
            F.die(f"(VaultNotFound) {vault}", 3)
        if name not in s.get("secrets", {}):
            F.die(
                f"(SecretNotFound) A secret with (name/id) {name} was not found in this key vault.",
                3,
            )
        F.emit({"id": f"https://{vault}.vault.azure.net/secrets/{name}/1", "name": name}, opts)
    elif v == "role assignment list":
        F.emit(
            [
                a
                for a in s["assignments"]
                if (not o("--assignee") or a.get("principalId") == o("--assignee"))
                and (not o("--role") or a.get("role") == o("--role"))
                and (not o("--scope") or (a.get("scope") or "").lower() == o("--scope").lower())
            ],
            opts,
        )
    elif v == "role assignment delete":
        before = len(s["assignments"])
        s["assignments"] = [
            a
            for a in s["assignments"]
            if not (
                (not o("--assignee") or a.get("principalId") == o("--assignee"))
                and (not o("--role") or a.get("role") == o("--role"))
                and (not o("--scope") or (a.get("scope") or "").lower() == o("--scope").lower())
            )
        ]
        F.save(s)
        if before == len(s["assignments"]):
            print("No matched assignments were found to delete", file=__import__("sys").stderr)
    elif v == "network vnet show":
        vid = o("--ids")
        r = (
            resource_by_id(s, vid)
            if vid
            else resource(s, F, rg, o("-n", "--name"), "Microsoft.Network/virtualNetworks")
        )
        if r is None:
            not_found(F, vid or o("-n", "--name"))
        # A test run's hub has no GatewaySubnet, so no gateway (OMNI_TEST_HUB_NO_GATEWAY).
        F.emit({"id": r["id"], "name": r["name"], "subnets": []}, opts)
    elif v.startswith("network vnet peering "):
        peering(F, s, v, opts, rg)
    elif v.startswith("network private-dns "):
        private_dns(F, s, v, opts, rg)
    elif v.startswith("network private-endpoint "):
        private_endpoint(F, s, v, opts, rg)
    else:
        return False
    return True


def vnet_exists(s, vid: str) -> bool:
    return resource_by_id(s, vid) is not None


def peering(F, s, v, opts, rg) -> None:
    o = lambda *n: F.opt(opts, *n)  # noqa: E731
    vnet, name = o("--vnet-name"), o("-n", "--name")
    peers = s.setdefault("peerings", {})
    local = resource(s, F, rg, vnet, "Microsoft.Network/virtualNetworks")
    if local is None:
        not_found(F, vnet)
    key = f"{local['id']}|{name}".lower()
    if v == "network vnet peering show":
        if key not in peers:
            not_found(F, name)
        F.emit(peering_view(s, local["id"], peers[key]), opts)
    elif v == "network vnet peering create":
        remote = o("--remote-vnet")
        if not vnet_exists(s, remote):
            F.die(f"(RemoteVnetNotFound) {remote}", 1)
        if o("--use-remote-gateways") == "true":
            F.die("(RemotePeeringIsNotEnabledForGatewayTransit) the hub has no gateway", 1)
        peers[key] = {"name": name, "local": local["id"], "remote": remote}
        F.save(s)
        F.emit(peering_view(s, local["id"], peers[key]), opts)
    elif v == "network vnet peering list":
        F.emit(
            [
                peering_view(s, local["id"], p)
                for p in peers.values()
                if p["local"].lower() == local["id"].lower()
            ],
            opts,
        )
    else:
        F.die(f"fake az: no model of `az {v}`", 2)


def peering_view(s, local: str, p: dict) -> dict:
    """Connected once the other side has its peering back; Initiated until then."""
    back = any(
        x["local"].lower() == p["remote"].lower() and x["remote"].lower() == local.lower()
        for x in s.get("peerings", {}).values()
    )
    return {
        "name": p["name"],
        "remoteVirtualNetwork": {"id": p["remote"]},
        "peeringState": "Connected" if back else "Initiated",
    }


def private_dns(F, s, v, opts, rg) -> None:
    o = lambda *n: F.opt(opts, *n)  # noqa: E731
    zones = s.setdefault("dns_zones", {})
    if v in ("network private-dns zone show", "network private-dns zone create"):
        name = o("-n", "--name")
        zid = f"{F.rg_id(rg)}/providers/Microsoft.Network/privateDnsZones/{name}"
        if v.endswith("create"):
            if rg not in s["groups"]:
                F.die(f"ResourceGroupNotFound: {rg}", 3)
            zones.setdefault(zid.lower(), {"id": zid, "name": name, "links": {}})
            F.save(s)
        elif zid.lower() not in zones and not deployed_zone(s, rg, name):
            not_found(F, name)
        z = zones.get(zid.lower()) or {"id": zid, "name": name, "links": {}}
        F.emit({"id": z["id"], "name": z["name"]}, opts)
        return
    if v in ("network private-dns link vnet show", "network private-dns link vnet create"):
        zname, name = o("-z", "--zone-name"), o("-n", "--name")
        zid = f"{F.rg_id(rg)}/providers/Microsoft.Network/privateDnsZones/{zname}"
        z = zones.get(zid.lower())
        if z is None and deployed_zone(s, rg, zname):
            z = zones.setdefault(zid.lower(), {"id": zid, "name": zname, "links": {}})
        if z is None:
            F.die(f"(ParentResourceNotFound) the private DNS zone {zname} does not exist", 3)
        if v.endswith("show"):
            if name not in z["links"]:
                not_found(F, name)
        else:
            vnet = o("-v", "--virtual-network")
            if not vnet_exists(s, vnet):
                F.die(f"(InvalidVirtualNetwork) {vnet} was not found", 1)
            z["links"][name] = vnet
            F.save(s)
        F.emit({"name": name, "virtualNetwork": {"id": z["links"][name]}}, opts)
        return
    F.die(f"fake az: no model of `az {v}`", 2)


def deployed_zone(s, rg, name: str) -> bool:
    """The zones the deployment makes in its group: the apps' internal domain and PostgreSQL's."""
    for d in s["deployments"].get(rg, []):
        outs = d["properties"]["outputs"]
        dom = (outs.get("internalEnvironmentDefaultDomain") or {}).get("value")
        pg = (outs.get("postgresHost") or {}).get("value", "")
        if name in (dom, f"{pg.split('.')[0]}.private.postgres.database.azure.com"):
            return True
    return False


def private_endpoint(F, s, v, opts, rg) -> None:
    o = lambda *n: F.opt(opts, *n)  # noqa: E731
    pes = s.setdefault("private_endpoints", {})
    if v in ("network private-endpoint show", "network private-endpoint create"):
        name = o("-n", "--name")
        if v.endswith("create"):
            target, group = o("--private-connection-resource-id"), o("--group-id")
            t = resource_by_id(s, target or "")
            if t is None:
                F.die(f"(PrivateLinkServiceNotFound) {target}", 1)
            if GROUP_IDS.get(group) != t["type"]:
                F.die(f"(InvalidGroupId) {group} is not a group id of {t['type']}", 1)
            subnet = o("--subnet") or ""
            if not vnet_exists(s, subnet.split("/subnets/")[0]):
                F.die(f"(InvalidSubnet) {subnet}", 1)
            ip = str(ipaddress.ip_address("10.60.0.132") + len(pes))
            pes[name] = {
                "name": name,
                "rg": rg,
                "target": t["id"],
                "targetName": t["name"],
                "group": group,
                "ip": ip,
                "zoneGroups": {},
            }
            F.save(s)
        elif name not in pes:
            not_found(F, name)
        p = pes[name]
        F.emit(
            {
                "name": name,
                "id": f"{F.rg_id(rg)}/providers/Microsoft.Network/privateEndpoints/{name}",
                "customDnsConfigs": [{"ipAddresses": [p["ip"]]}],
                "provisioningState": "Succeeded",
            },
            opts,
        )
        return
    if v.startswith("network private-endpoint dns-zone-group "):
        ep, name = o("--endpoint-name"), o("-n", "--name")
        if ep not in pes:
            F.die(f"(ParentResourceNotFound) the private endpoint {ep} does not exist", 3)
        if v.endswith("show"):
            if name not in pes[ep]["zoneGroups"]:
                not_found(F, name)
        elif v.endswith("create"):
            zid = o("--private-dns-zone") or ""
            if zid.lower() not in s.get("dns_zones", {}):
                F.die(f"(InvalidPrivateDnsZoneIds) {zid}", 1)
            pes[ep]["zoneGroups"][name] = zid
            F.save(s)
        else:
            F.die(f"fake az: no model of `az {v}`", 2)
        F.emit({"name": name}, opts)
        return
    F.die(f"fake az: no model of `az {v}`", 2)


def on_deploy(F, s, rg: str, opts, p: str) -> None:
    """What a deployment does that the guide reads back: the Key Vault Secrets Officer role for
    the uploader its parameters name (phase 1). Phase 2 names none, and ARM, being incremental,
    leaves the old one: the guide's step 43 removes it."""
    params = params_of(opts)
    uploader = (params.get("keyVaultUploaderObjectId") or {}).get("value")
    kv = resource(s, F, rg, f"kv-{p}", "Microsoft.KeyVault/vaults")
    if uploader and kv is not None:
        a = {
            "id": stable_id("ra", uploader, kv["id"]),
            "scope": kv["id"],
            "role": "Key Vault Secrets Officer",
            "principalId": uploader,
        }
        if not any(x["id"] == a["id"] for x in s["assignments"]):
            s["assignments"].append(a)


# ------------------------------------------------------------------------------ DNS


def dns(F, names: list[str]) -> None:
    """The stand-in for docs/guide/tools/check-private-dns.sh: each name resolves privately
    only when the fake's network says so, else it doesn't resolve (as on the real workstation
    before section 6 is done)."""
    s = F.load()
    F.record("check-private-dns.sh", names)
    hub = next((r["id"] for r in s["resources"] if r["name"] == "vnet-ofreal-hub"), None)
    bad = 0
    for raw in names:
        host = raw.split("://")[-1].split("/")[0].split(":")[0]
        ip = private_ip(s, host, hub)
        if ip is None:
            print(f"  {host}: does not resolve (nodename nor servname provided, or not known)")
            bad += 1
        else:
            print(f"  {host}: {ip}")
    if bad:
        print(
            f"{bad} name(s) not private: link the private DNS zones to the hub and add the "
            "forwarders (section 6)"
        )
        raise SystemExit(1)
    print("all private")


def linked_to(s, zone: str, vnet: str | None) -> bool:
    if vnet is None:
        return False
    for z in s.get("dns_zones", {}).values():
        if z["name"] == zone and any(v.lower() == vnet.lower() for v in z["links"].values()):
            return True
    return False


def private_ip(s, host: str, hub: str | None) -> str | None:
    for kind, zone in ZONE_OF.items():
        suffix = ".blob.core.windows.net" if kind == "blob" else ".vault.azure.net"
        if host.endswith(suffix):
            name = host[: -len(suffix)]
            for pe in s.get("private_endpoints", {}).values():
                if pe["targetName"] == name and pe["group"] == kind and pe["zoneGroups"]:
                    return pe["ip"] if linked_to(s, zone, hub) else None
            return None
    for deps in s["deployments"].values():
        for d in deps:
            outs = {k: (v or {}).get("value") for k, v in d["properties"]["outputs"].items()}
            dom, pg = outs.get("internalEnvironmentDefaultDomain"), outs.get("postgresHost") or ""
            if dom and host.endswith("." + dom):
                return outs.get("internalEnvironmentStaticIp") if linked_to(s, dom, hub) else None
            if pg and host == pg:
                pz = f"{pg.split('.')[0]}.private.postgres.database.azure.com"
                return "10.60.0.100" if linked_to(s, pz, hub) else None
    return None


# ------------------------------------------------------- psql, curl, openssl, uv


def deployment_outputs(s) -> dict[str, Any]:
    outs: dict[str, Any] = {}
    for deps in s["deployments"].values():
        for d in deps:
            outs.update({k: (v or {}).get("value") for k, v in d["properties"]["outputs"].items()})
    return outs


def psql(F, argv: list[str]) -> None:
    F.record("psql", argv)
    if argv[:1] == ["--version"]:
        print("psql (PostgreSQL) 17.2 (fake: dry run)")
        return
    s = F.load()
    conn = next((a for a in argv if a.startswith("host=")), "")
    host = re.search(r"host=(\S+)", conn)
    if not host or host.group(1) != deployment_outputs(s).get("postgresHost"):
        F.die(
            f'psql: error: could not translate host name "{host and host.group(1)}" to address', 2
        )
    if os.environ.get("PGPASSWORD") != "fake-token":
        F.die("psql: error: FATAL:  password authentication failed (no Entra token)", 2)
    if "-f" in argv:
        sql = argv[argv.index("-f") + 1]
        if Path(sql).name == "create-principals.sql":
            prefix = next((a.split("=", 1)[1] for a in argv if a.startswith("prefix=")), "")
            for app in ("api", "enrol", "web", "worker", "verifier"):
                print(f"psql:{sql}: NOTICE:  created role id-{prefix}-{app}")
            return
        F.die(f"psql: refused: the fake models no SQL file {sql} (only create-principals.sql)", 3)
    print("")  # an empty result


def curl(F, argv: list[str]) -> None:
    F.record("curl", argv)
    s = F.load()
    url = next((a for a in argv if a.startswith("https://") or a.startswith("http://")), "")
    host = url.split("://", 1)[-1].split("/")[0]
    outs = deployment_outputs(s)
    known = {
        (outs.get(k) or "").split("://", 1)[-1].split("/")[0]
        for k in ("relayPublicUrl", "webUrl", "enrolUrl")
    }
    hub = next((r["id"] for r in s["resources"] if r["name"] == "vnet-ofreal-hub"), None)
    public = host == (outs.get("relayPublicUrl") or "").split("://", 1)[-1]
    if host not in known or not (public or private_ip(s, host, hub)):
        F.die(f"curl: (6) Could not resolve host: {host}", 6)
    path = "/" + url.split("://", 1)[-1].split("/", 1)[1] if "/" in url.split("://", 1)[-1] else "/"
    known_path = path.split("?")[0] == "/healthz"
    if "--write-out" in argv:
        print("200" if known_path else "404", end="")
    elif known_path:
        print('{"status":"ok"}', end="")
    elif "--fail" in argv or "-f" in argv:
        F.die("curl: (22) The requested URL returned error: 404", 22)
    else:
        print('{"detail":"Not Found"}', end="")


def openssl(F, argv: list[str]) -> None:
    """openssl s_client, as the guide's tls-issuer.sh runs it: the server's certificate and
    its issuer, from a CA the fake keeps, verified for the host it was asked for."""
    if argv[:1] != ["s_client"]:
        F.record("openssl", argv, "refused: no network in a dry run")
        F.die(
            f"openssl {argv[0] if argv else ''}: refused: a dry run reaches no network, and "
            "only s_client is modelled",
            2,
        )
    F.record("openssl", argv)
    s = F.load()
    target = argv[argv.index("-connect") + 1] if "-connect" in argv else ""
    host = target.rsplit(":", 1)[0]
    outs = deployment_outputs(s)
    known = {
        (outs.get(k) or "").split("://", 1)[-1].split("/")[0].split(":")[0]
        for k in ("enrolUrl", "webUrl", "apiUrl", "relayPublicUrl")
    }
    if host not in known:
        print(f"connect: getaddrinfo: {host}: nodename nor servname provided")
        raise SystemExit(1)
    leaf, issuer = tls_chain(host)
    print("CONNECTED(00000003)\n---\nCertificate chain")
    print(f" 0 s:CN={host}\n   i:CN=Fake TLS Issuing CA 01")
    print(leaf, end="")
    print(" 1 s:CN=Fake TLS Issuing CA 01\n   i:CN=Fake TLS Root")
    print(issuer, end="")
    print("---\nVerify return code: 0 (ok)\n---")


def tls_chain(host: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = Path(os.environ["REAL_FAKE_STATE"]).parent / "fake-tls"
    d.mkdir(exist_ok=True)
    kf, cf = d / "issuer.key", d / "issuer.pem"
    now = datetime(2026, 11, 1, tzinfo=UTC)
    if not kf.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake TLS Issuing CA 01")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256())
        )
        kf.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key = serialization.load_pem_private_key(kf.read_bytes(), None)
    issuer = x509.load_pem_x509_certificate(cf.read_bytes())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(issuer.subject)
        .public_key(leaf_key.public_key())
        .serial_number(2)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return leaf.public_bytes(pem).decode(), issuer.public_bytes(pem).decode()


def uv_service(F, argv: list[str]) -> None:
    """The service's database commands the guide runs (catalog.migrate, admin backend add):
    recorded, with their connection checked, never run against any database."""
    F.record("uv", argv, "the service's database command (dry run: not run)")
    s = F.load()
    url = os.environ.get("OMNIFILE_DATABASE_URL", "")
    host = re.search(r"host=(\S+)", url)
    if not host or host.group(1) != deployment_outputs(s).get("postgresHost"):
        F.die(
            f"psycopg.OperationalError: could not translate host name {host and host.group(1)!r}", 1
        )
    if os.environ.get("PGPASSWORD") != "fake-token":
        F.die("psycopg.OperationalError: password authentication failed", 1)
    if "omnifile_service.catalog.migrate" in argv:
        members = os.environ.get("OMNIFILE_DB_MEMBERS", "")
        print("catalog: migrations applied (fake: dry run)")
        print(f"catalog: privileges for {members or 'no members'}")
    elif "omnifile_service.admin" in argv:
        i = argv.index("omnifile_service.admin")
        print("added: " + " ".join(argv[i + 1 :]) + " (fake: dry run)")


# ------------------------------------------------------------------------ the CA


def edge_ca() -> tuple[Any, Any]:
    """The edge CA enrol's init-kv-ca makes (its key stays in the fake, as in Key Vault)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = Path(os.environ["REAL_FAKE_STATE"]).parent / "fake-ca"
    d.mkdir(exist_ok=True)
    kf, cf = d / "ca.key", d / "ca.pem"
    if not kf.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime(2026, 11, 1, tzinfo=UTC)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OmniFile edge CA (fake)")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        kf.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return (
        serialization.load_pem_private_key(kf.read_bytes(), None),
        x509.load_pem_x509_certificate(cf.read_bytes()),
    )


def ca_pem() -> str:
    from cryptography.hazmat.primitives import serialization

    return edge_ca()[1].public_bytes(serialization.Encoding.PEM).decode()


def issue_relay_cert(csr_b64: str) -> str:
    """enrol's issue-relay-cert: the CSR's key, signed by the edge CA, CN=omnifile-relay."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    key, ca = edge_ca()
    csr = x509.load_pem_x509_csr(base64.b64decode(csr_b64))
    if not csr.is_signature_valid:
        raise SystemExit("issue-relay-cert: the CSR's signature does not verify")
    now = datetime(2026, 11, 1, tzinfo=UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(int(hashlib.sha256(csr_b64.encode()).hexdigest()[:15], 16))
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()
