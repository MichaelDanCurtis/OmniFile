"""The values the Azure run gives the customer guide: every name docs/guide/omnifile.env.example
defines (WP7's OMNI_* names), and the guide's inputs that never go in a file.

The test topology behind them: the deployment's VNet is 10.60.0.0/16 (main.bicep's
defaults); the test VM's subnet 10.60.1.0/24 stands in for the client's edge, user and admin
networks; the appliance sits behind the VM on libvirt's NAT network 192.168.150.0/24, with
the run's AD realm (a Samba DC container) forwarded by libvirt's DNS.
"""

from __future__ import annotations

import os

from .vm import EDGE_SUBNET_PREFIX

AD_REALM = "OFREAL.TEST"
HUB_VNET = "vnet-ofreal-hub"
HUB_PREFIX = "10.62.0.0/16"


def pg_admin_name(run_id: str) -> str:
    """The run's PostgreSQL admin group: the whole run id in its name (never just the 8-character
    prefix two runs can share), and the run id in its description, since groups take no tags."""
    return f"ofreal-{run_id}-pg-admins"


def omni(st: dict, prefix: str, aws: dict[str, str]) -> dict[str, str]:
    net = EDGE_SUBNET_PREFIX
    return {
        "OMNI_SUBSCRIPTION_ID": st["identity"]["subscription"],
        "OMNI_TENANT_ID": st["identity"]["tenant"],
        "OMNI_LOCATION": st["location"],
        "OMNI_RG": st["rg"],
        "OMNI_PREFIX": prefix,
        "OMNI_RELEASE": st.get("release") or os.environ.get("REAL_RELEASE", "0.0.0"),
        "OMNI_SOURCE_REGISTRY": f"ofreal{prefix}.azurecr.io",
        "OMNI_ACR": f"ofreal{prefix}",
        "OMNI_PG_ADMIN_GROUP": pg_admin_name(st["run_id"]),
        "OMNI_EDGE_CIDRS": net,
        "OMNI_USER_CIDRS": net,
        "OMNI_ADMIN_CIDRS": net,
        "OMNI_VNET_PREFIX": "10.60.0.0/16",
        "OMNI_SUBNET_CAE": "10.60.0.0/26",
        "OMNI_SUBNET_RELAY": "10.60.0.64/27",
        "OMNI_SUBNET_PG": "10.60.0.96/27",
        "OMNI_SUBNET_PE": "10.60.0.128/27",
        # The run's own stand-in hub (harness/azure.py up.hub): an empty, tagged VNet.
        # The stand-in hub has no gateway: step 20 peers without gateway transit only with this.
        "OMNI_TEST_HUB_NO_GATEWAY": "1",
        "OMNI_HUB_VNET_ID": (
            f"/subscriptions/{st['identity']['subscription']}/resourceGroups/"
            f"{st['rg']}/providers/Microsoft.Network/virtualNetworks/{HUB_VNET}"
        ),
        "OMNI_DNS_ZONE_RG": "",
        "OMNI_DNS_RESOLVER_IP": "168.63.129.16",
        # Email fallback's sender: the owner's own mailbox unless REAL_NOTIFY_SENDER says.
        "OMNI_NOTIFY_SENDER": os.environ.get("REAL_NOTIFY_SENDER")
        or st["identity"].get("user", ""),
        "OMNI_ALERT_ACTION_GROUP_ID": "",
        "OMNI_AD_DOMAIN": AD_REALM.lower(),
        "OMNI_AD_WORKGROUP": "OFREAL",
        "OMNI_AD_JOIN_USER": "ofreal-join",
        "OMNI_AD_JOIN_OU": "",
        "OMNI_AD_ADMIN_GROUP": "filer admins",
        "OMNI_AD_ADMIN_USER": "ofreal-join",
        # The run's DC isn't synced to Entra, so no AD group could hold a portal role, and a
        # test tenant may have no Entra ID P1: the owner holds the roles by name (decision 89).
        "OMNI_ROLE_HOLDERS": "people",
        "OMNI_GROUP_ARCHIVEADMIN": "",
        "OMNI_GROUP_DATASTEWARD": "",
        "OMNI_GROUP_USER": "",
        "OMNI_PEOPLE_ARCHIVEADMIN": st["identity"].get("user", ""),
        "OMNI_PEOPLE_DATASTEWARD": st["identity"].get("user", ""),
        "OMNI_FIRST_UID": "20000",
        "OMNI_FIRST_GID": "30000",
        "OMNI_DOMAIN_USERS_GID": "30010",  # samba-dc.sh gives Domain Users this gidNumber
        "OMNI_EDGE_NAME": f"edge-{prefix}",
        "OMNI_EDGE_HOSTNAME": f"edge-{prefix}",
        "OMNI_EDGE_ADDRESS": "192.168.150.50/24",
        "OMNI_EDGE_GATEWAY": "192.168.150.1",
        "OMNI_EDGE_DNS": "192.168.150.1",
        "OMNI_EDGE_RESTART_WINDOW": "Sun *-*-* 03:00",
        "OMNI_DATA_DISK_GB": "64",
        "OMNI_KVM_BRIDGE": "virbr-ofreal",
        "OMNI_SHARE": "projects",
        "OMNI_SHARE_OWNERS": "OmniFile-DataStewards",
        "OMNI_SHARE_WRITERS": "Proj-Writers",
        "OMNI_SHARE_READERS": "Proj-Readers",
        "OMNI_NFS_CLIENTS": "192.168.150.0/24",
        "OMNI_TEST_USER": "geo-writer",
        "OMNI_TEST_UID": "20002",
        "OMNI_OTHER_USER": "geo-reader",
        "OMNI_CLIENT_MOUNT": "/mnt/omnifile",
        # WP7's section 18 (steps 80-82): no API client in a test run, so step 80 does nothing.
        "OMNI_API_CLIENT_APP_ID": "",
        "OMNI_API_CLIENT_NAME": "",
        "OMNI_API_CLIENT_SCOPES": "",
        "OMNI_API_CLIENT_OWNER": "",
        **aws,
    }


def aws_values() -> dict[str, str]:
    """OMNI_AWS_*: always empty in the Azure run, so the guide's AWS section (section 17) skips
    itself. That section makes AWS resources the Azure run can't pin to an allow-listed
    account or tear down; the AWS run (`--cloud aws`) covers AWS instead."""
    keys = (
        "ACCOUNT_ID",
        "REGION",
        "VPC_ID",
        "SUBNET_IDS",
        "SG_ID",
        "SOURCE_BUCKETS",
        "OPS_BUCKET",
        "NAME_SUFFIX",
    )
    return {f"OMNI_AWS_{k}": "" for k in keys}


#: The test-only values customer.bicepparam reads through omnifile.env (empty = the customer
#: default; agreed with WP7 for docs/guide on #33). They make the guide's deployment the small,
#: tagged, removable one a test run needs: run tags, a purgeable vault, a 1-day unlocked WORM
#: share with no lock, and the guide's small size (WP16, #59), whose server (Burstable B1ms,
#: 32 GB) and replica counts a test run takes as they are: no size override of its own.
TEST_VALUES = {
    # Empty: the size's own server and replica counts (small's B1ms, 32 GB, 1 api replica).
    "OMNI_TEST_PG_SKU_TIER": "",
    "OMNI_TEST_PG_SKU_NAME": "",
    "OMNI_TEST_PG_STORAGE_GB": "",
    "OMNI_TEST_API_MIN_REPLICAS": "",
    "OMNI_TEST_API_MAX_REPLICAS": "",
    "OMNI_TEST_WORKER_MAX_REPLICAS": "",
    "OMNI_TEST_KV_PURGE_PROTECTION": "false",
    "OMNI_TEST_KV_SOFT_DELETE_DAYS": "7",
    "OMNI_TEST_WORM_RETENTION_DAYS": "1",
    "OMNI_TEST_LOCK_WORM": "false",
    "OMNI_TEST_BLOB_SOFT_DELETE_DAYS": "1",
    "OMNI_TEST_ALERTS": "false",
    # The guide's size (omnifile.env.example; WP16, #59): a test run deploys small.
    "OMNI_DEPLOYMENT_SIZE": "small",
}


def test_values(ctx) -> dict[str, str]:
    from .core import iso_minutes

    return {
        "OMNI_TEST_RUN_ID": ctx.run_id,
        "OMNI_TEST_RUN_EXPIRES": iso_minutes(ctx.expires),
        **TEST_VALUES,
    }


def secrets(ctx) -> dict[str, str]:
    """The guide's inputs (<secret:NAME>, input=NAME): generated test passwords."""
    return {
        "OMNI_AD_JOIN_PASSWORD": ctx.secret("AD_JOIN_PASSWORD"),
        "OMNI_TEST_USER_PASSWORD": ctx.secret("AD_USER_PASSWORD"),
        "OMNI_OTHER_USER_PASSWORD": ctx.secret("AD_OTHER_PASSWORD"),
        "AD_JOIN_PASSWORD": ctx.secret("AD_JOIN_PASSWORD"),
    }
