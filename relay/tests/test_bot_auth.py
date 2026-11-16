"""Bot Framework JWT validation, end to end through POST /api/messages."""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest

from .conftest import BOT_APP_ID, OTHER_TENANT, b64url, make_activity


def test_valid_token_is_accepted_and_forwarded(post, bot_token, intent, sink):
    resp = post(make_activity(intent()), bot_token())
    assert resp.status_code == 200
    body = resp.json()
    assert body["statusCode"] == 200
    assert body["type"] == "application/vnd.microsoft.card.adaptive"
    assert len(sink.forwards) == 1
    fwd = sink.forwards[0]
    assert fwd.action == "recall"
    assert fwd.clicker_object_id == "0f0f0f0f-1111-2222-3333-444444444444"


@pytest.mark.parametrize(
    "claims, reason",
    [
        ({"aud": "someone-elses-bot"}, "wrong audience"),
        ({"aud": None}, "missing audience"),
        ({"iss": "https://evil.example"}, "wrong issuer"),
        ({"iss": "https://sts.windows.net/f8cdef31-a31e-4b4a-93e4-5f571e91255a/"}, "emulator"),
        ({"exp": int(time.time()) - 600}, "expired beyond 5 min skew"),
        ({"exp": None}, "no expiry"),
        ({"nbf": int(time.time()) + 600}, "not yet valid"),
        ({"serviceUrl": "https://smba.trafficmanager.net/amer/"}, "serviceUrl claim mismatch"),
        ({"serviceUrl": None}, "no serviceUrl claim"),
    ],
)
def test_bad_claims_are_rejected(post, bot_token, intent, sink, claims, reason):
    resp = post(make_activity(intent()), bot_token(**claims))
    assert resp.status_code == 401, reason
    assert sink.forwards == []


def test_expiry_within_clock_skew_is_accepted(post, bot_token, intent, sink):
    resp = post(make_activity(intent()), bot_token(exp=int(time.time()) - 120))
    assert resp.status_code == 200
    assert len(sink.forwards) == 1


def test_wrong_signing_key_is_rejected(post, bot_token, intent, sink):
    # Signed by an attacker's key but claiming Microsoft's kid.
    resp = post(make_activity(intent()), bot_token(kid="kid-a", signer="attacker"))
    assert resp.status_code == 401
    assert sink.forwards == []


def test_unknown_kid_is_rejected(post, bot_token, intent, sink):
    resp = post(make_activity(intent()), bot_token(kid="attacker"))
    assert resp.status_code == 401
    assert sink.forwards == []


def _unsigned(alg: str, kid: str = "kid-a", signature: str = "") -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": alg, "typ": "JWT", "kid": kid}).encode())
    payload = b64url(
        json.dumps(
            {
                "iss": "https://api.botframework.com",
                "aud": BOT_APP_ID,
                "exp": now + 3600,
                "serviceUrl": "https://smba.trafficmanager.net/emea/",
            }
        ).encode()
    )
    return f"{header}.{payload}.{signature}"


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "RS256"])
def test_unsigned_and_alg_none_are_rejected(post, intent, sink, alg):
    resp = post(make_activity(intent()), _unsigned(alg))
    assert resp.status_code == 401
    assert sink.forwards == []


def test_hs256_with_public_key_as_secret_is_rejected(post, rsa_keys, intent, sink):
    # Classic key-confusion attack: HMAC "signed" with the public key's PEM.
    from cryptography.hazmat.primitives import serialization

    pem = (
        rsa_keys["kid-a"]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    import hashlib
    import hmac

    body = _unsigned("HS256")[:-1]
    sig = hmac.new(pem, body.encode(), hashlib.sha256).digest()
    resp = post(make_activity(intent()), body + "." + b64url(sig))
    assert resp.status_code == 401
    assert sink.forwards == []


def test_algorithm_not_in_metadata_is_rejected(make_client, post, bot_token, intent, microsoft):
    microsoft.algs = ["RS512"]  # metadata no longer lists RS256
    c = make_client()
    resp = post(make_activity(intent()), bot_token(alg="RS256"), c)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "header",
    [None, "Basic abc", "bearer", "Bearer ", "Bearer a b"],
)
def test_missing_or_malformed_authorization(client, intent, sink, header):
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["authorization"] = header
    resp = client.post(
        "/api/messages", content=json.dumps(make_activity(intent())), headers=headers
    )
    assert resp.status_code == 401
    assert sink.forwards == []


def test_garbage_token_is_rejected(post, intent, sink):
    resp = post(make_activity(intent()), "not-a-jwt")
    assert resp.status_code == 401
    assert sink.forwards == []


def test_channel_without_endorsement_is_forbidden(make_client, post, bot_token, intent, microsoft):
    microsoft.endorsements = {"kid-a": ["webchat"]}
    c = make_client()
    resp = post(make_activity(intent()), bot_token(), c)
    assert resp.status_code == 403


def test_non_teams_channel_is_forbidden(post, bot_token, intent, sink):
    resp = post(make_activity(intent(), channelId="webchat"), bot_token())
    assert resp.status_code == 403
    assert sink.forwards == []


def test_token_tid_for_other_tenant_is_forbidden(post, bot_token, intent, sink):
    resp = post(make_activity(intent()), bot_token(tid=OTHER_TENANT))
    assert resp.status_code == 403
    assert sink.forwards == []


def test_activity_from_other_tenant_is_forbidden(post, bot_token, intent, sink):
    resp = post(make_activity(intent(tid=OTHER_TENANT), tenant=OTHER_TENANT), bot_token())
    assert resp.status_code == 403
    assert sink.forwards == []


def test_activity_with_mixed_tenants_is_forbidden(post, bot_token, intent, sink):
    activity = make_activity(intent())
    activity["channelData"]["tenant"]["id"] = OTHER_TENANT
    resp = post(activity, bot_token())
    assert resp.status_code == 403


# --- JWKS caching and rotation ---------------------------------------------------------------


def test_jwks_is_cached(post, bot_token, intent, microsoft):
    for _ in range(3):
        assert post(make_activity(intent()), bot_token()).status_code == 200
    assert microsoft.metadata_hits == 1
    assert microsoft.jwks_hits == 1


def test_rotation_new_key_picked_up_after_floor(post, bot_token, intent, microsoft, clock, sink):
    assert post(make_activity(intent()), bot_token()).status_code == 200
    # Microsoft publishes kid-b and starts signing with it.
    microsoft.published = ["kid-a", "kid-b"]
    clock.advance(301)  # past jwks_min_refresh_seconds
    resp = post(make_activity(intent()), bot_token(kid="kid-b"))
    assert resp.status_code == 200
    assert microsoft.jwks_hits == 2
    assert len(sink.forwards) == 2


def test_unknown_kid_refresh_is_rate_limited(post, bot_token, intent, microsoft, clock):
    assert post(make_activity(intent()), bot_token()).status_code == 200
    microsoft.published = ["kid-a", "kid-b"]
    clock.advance(10)
    # Within the floor: no refetch, so the new kid is not yet known.
    for _ in range(5):
        assert post(make_activity(intent()), bot_token(kid="kid-b")).status_code == 401
    assert microsoft.jwks_hits == 1


def test_rotation_retired_key_is_rejected(post, bot_token, intent, microsoft, clock):
    assert post(make_activity(intent()), bot_token()).status_code == 200
    microsoft.published = ["kid-b"]
    clock.advance(6 * 3600 + 1)  # scheduled refresh (well inside 24 h)
    assert post(make_activity(intent()), bot_token(kid="kid-a")).status_code == 401
    assert post(make_activity(intent()), bot_token(kid="kid-b")).status_code == 200


def test_stale_keys_served_while_microsoft_down_then_fail_closed(
    post, bot_token, intent, microsoft, clock
):
    assert post(make_activity(intent()), bot_token()).status_code == 200
    microsoft.down = True
    clock.advance(7 * 3600)
    assert post(make_activity(intent()), bot_token()).status_code == 200
    clock.advance(48 * 3600)
    assert post(make_activity(intent()), bot_token()).status_code == 503


def test_microsoft_unreachable_at_start_fails_closed(post, bot_token, intent, microsoft, sink):
    microsoft.down = True
    assert post(make_activity(intent()), bot_token()).status_code == 503
    assert sink.forwards == []


def test_metadata_pointing_jwks_elsewhere_is_ignored(post, bot_token, intent, microsoft, sink):
    microsoft.jwks_uri = "https://attacker.example/keys"
    assert post(make_activity(intent()), bot_token()).status_code == 503
    assert microsoft.jwks_hits == 0
    assert sink.forwards == []


def test_no_switch_disables_validation():
    from omnifile_relay import config

    fields = config.Settings.__dataclass_fields__
    assert not any("verify" in f or "disable" in f or "insecure" in f for f in fields)


def test_token_header_tampering(post, bot_token, intent, sink):
    token = bot_token()
    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["aud"] = BOT_APP_ID
    claims["serviceUrl"] = "https://smba.trafficmanager.net/amer/"
    forged = ".".join([header, b64url(json.dumps(claims).encode()), sig])
    activity = make_activity(intent(), service_url="https://smba.trafficmanager.net/amer/")
    assert post(activity, forged).status_code == 401
    assert sink.forwards == []


def test_pyjwt_rejects_none_even_if_asked():
    # Guard against a PyJWT regression: alg=none must never verify with a real key list.
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(_unsigned("none"), key="", algorithms=["RS256"])
