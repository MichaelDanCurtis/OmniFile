"""The caller's address behind the ingress (M7 review): the sign-in rate
limit and audit rows key on it, so it must be the caller, never the ingress,
and never an address the caller wrote itself."""

from __future__ import annotations

import ipaddress

from starlette.requests import Request

from omnifile_service.web.context import client_addr

INGRESS = (ipaddress.ip_network("10.20.0.0/24"),)


def _req(peer: str, *xff: str) -> Request:
    headers = [(b"x-forwarded-for", v.encode()) for v in xff]
    return Request({"type": "http", "client": (peer, 40000), "headers": headers})


def test_behind_the_ingress_the_address_it_appended() -> None:
    # The caller claims 1.2.3.4; the ingress appended the real address last.
    assert client_addr(_req("10.20.0.7", "1.2.3.4, 203.0.113.9"), INGRESS) == "203.0.113.9"
    assert client_addr(_req("10.20.0.7", "1.2.3.4", "203.0.113.9"), INGRESS) == "203.0.113.9"
    assert client_addr(_req("10.20.0.7"), INGRESS) == "10.20.0.7"


def test_a_peer_outside_the_ingress_is_never_believed() -> None:
    assert client_addr(_req("10.30.0.7", "203.0.113.9"), INGRESS) == "10.30.0.7"
    assert client_addr(_req("10.20.0.7", "203.0.113.9"), ()) == "10.20.0.7"
