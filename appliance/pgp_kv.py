#!/usr/bin/env python3
"""OpenPGP signatures made by a key that never leaves Azure Key Vault (owner decision B).

systemd-sysupdate checks a feed's SHA256SUMS against SHA256SUMS.gpg with gpg and the
keyring in the image (/usr/lib/systemd/import-pubring.pgp). The update-signing key is an
EC P-256 key in Key Vault Premium (EC-HSM, not exportable), so gpg can't hold it. This
builds the OpenPGP packets itself (RFC 4880 v4, ECDSA over NIST P-256 per RFC 6637, SHA-256)
and has Key Vault sign only their 32-byte digests (ES256).

    pgp_kv.py pubkey --signer SPEC --uid 'OmniFile updates <updates@example>' \\
                     --created EPOCH --out import-pubring.pgp
    pgp_kv.py sign   --signer SPEC --created EPOCH --ring import-pubring.pgp \
                     --in SHA256SUMS --out SHA256SUMS.gpg
    pgp_kv.py fingerprint --signer SPEC --created EPOCH
    pgp_kv.py ring-fingerprints import-pubring.pgp
    pgp_kv.py check-cert --signer SPEC --cert db.crt

`sign` refuses unless the signer's key, as Key Vault holds it now, is a key of the committed
keyring that images embed (--ring): after a rotation, a feed signed with the new key would
be rejected by every installed box, so it is never made (Codex on #31). `check-cert` does
the same for the Secure Boot key against the certificate clients enrolled.

--created is the key's OpenPGP creation time: it is part of the fingerprint, so the same
value must be used for the keyring and every signature (appliance/release.conf).

SPEC is one of:
  kv:https://<vault>.vault.azure.net/keys/<name>/<version>
      `az keyvault key show` for the public key and `az keyvault key sign --algorithm ES256
      --digest` for each signature, as the signed-in identity (the release pipeline's).
      The version is required: without one Key Vault uses the key's latest version, which a
      rotation changes under the release. $AZ names another az binary.
  fake:<key.pem>
      A local P-256 key through openssl: for tests and dry runs only. A release refuses it
      (release.sh --signer must be kv: unless --allow-partial).

Standard library only, plus openssl (fake) or az (kv).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time

P256_OID = bytes.fromhex("2a8648ce3d030107")
PUBKEY_ECDSA = 19
HASH_SHA256 = 8


def _b64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mpi(b):
    b = b.lstrip(b"\0") or b"\0"
    return struct.pack(">H", (len(b) - 1) * 8 + b[0].bit_length()) + b


def _packet(tag, body):
    n = len(body)
    if n < 192:
        ln = bytes([n])
    elif n < 8384:
        n -= 192
        ln = bytes([(n >> 8) + 192, n & 0xFF])
    else:
        ln = b"\xff" + struct.pack(">I", n)
    return bytes([0xC0 | tag]) + ln + body


def _subpacket(kind, data):
    assert len(data) + 1 < 192
    return bytes([len(data) + 1, kind]) + data


def _der_ecdsa_to_rs(der):
    """r, s (32 bytes each) from a DER ECDSA-Sig-Value."""
    assert der[0] == 0x30
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    out = []
    for _ in range(2):
        assert der[i] == 0x02
        n = der[i + 1]
        out.append(der[i + 2:i + 2 + n].lstrip(b"\0").rjust(32, b"\0"))
        i += 2 + n
    return out[0], out[1]


class FakeSigner:
    """A local P-256 key through openssl (tests and dry runs)."""

    kind = "fake"

    def __init__(self, pem):
        self.pem = pem

    def public_point(self):
        der = subprocess.run(
            ["openssl", "pkey", "-in", self.pem, "-pubout", "-outform", "DER"],
            check=True, capture_output=True,
        ).stdout
        point = der[-65:]
        assert point[0] == 4 and P256_OID in der, "not a P-256 key"
        return point

    def rsa_public(self):
        return _rsa_numbers(["openssl", "rsa", "-in", self.pem, "-noout", "-modulus"],
                            ["openssl", "rsa", "-in", self.pem, "-noout", "-text"])

    def sign_digest(self, digest):
        der = subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", self.pem, "-pkeyopt", "digest:sha256"],
            input=digest, check=True, capture_output=True,
        ).stdout
        return _der_ecdsa_to_rs(der)


class KeyVaultSigner:
    """An EC-HSM P-256 key in Azure Key Vault, through the az CLI."""

    kind = "kv"

    def __init__(self, kid):
        if not re.fullmatch(
            r"https://[a-z0-9-]+\.vault\.azure\.net/keys/[A-Za-z0-9-]+/[0-9a-f]{32}", kid
        ):
            raise SystemExit(
                f"{kid}: give the key's full id with its version "
                "(https://<vault>.vault.azure.net/keys/<name>/<32-hex version>); without one, "
                "Key Vault signs with whatever version is latest"
            )
        self.kid = kid
        self.az = os.environ.get("AZ", "az")

    def rsa_public(self):
        jwk = self._az("show")["key"]
        if not str(jwk.get("kty", "")).startswith("RSA"):
            raise SystemExit(f"{self.kid}: not an RSA key ({jwk.get('kty')})")
        return (int.from_bytes(_b64url(jwk["n"]), "big"),
                int.from_bytes(_b64url(jwk["e"]), "big"))

    def _az(self, *args):
        out = subprocess.run([self.az, "keyvault", "key", *args, "--id", self.kid, "-o", "json"],
                             check=True, capture_output=True).stdout
        return json.loads(out)

    def public_point(self):
        jwk = self._az("show")["key"]
        if jwk.get("crv") != "P-256" or not str(jwk.get("kty", "")).startswith("EC"):
            raise SystemExit(f"{self.kid}: not an EC P-256 key ({jwk.get('kty')}, {jwk.get('crv')})")
        return b"\x04" + _b64url(jwk["x"]).rjust(32, b"\0") + _b64url(jwk["y"]).rjust(32, b"\0")

    def sign_digest(self, digest):
        r = self._az("sign", "--algorithm", "ES256", "--digest",
                     base64.b64encode(digest).decode())
        sig = r.get("signature") or r.get("result")
        raw = _b64url(sig.replace("+", "-").replace("/", "_"))
        if len(raw) != 64:
            raise SystemExit(f"Key Vault returned a {len(raw)}-byte ES256 signature, not 64")
        return raw[:32], raw[32:]


def _rsa_numbers(modulus_cmd, text_cmd):
    mod = subprocess.run(modulus_cmd, check=True, capture_output=True, text=True).stdout
    n = int(mod.strip().split("=", 1)[1], 16)
    text = subprocess.run(text_cmd, check=True, capture_output=True, text=True).stdout
    e = int(re.search(r"(?:publicExponent|Exponent):\s*(\d+)", text).group(1))
    return n, e


def cert_rsa_public(cert):
    return _rsa_numbers(["openssl", "x509", "-in", cert, "-noout", "-modulus"],
                        ["openssl", "x509", "-in", cert, "-noout", "-text"])


def ring_fingerprints(data):
    """The v4 fingerprints of the keys and subkeys in an OpenPGP keyring."""
    out, i = [], 0
    while i < len(data):
        hdr = data[i]
        if not hdr & 0x80:
            raise SystemExit("not an OpenPGP keyring")
        if hdr & 0x40:  # new format
            tag, n = hdr & 0x3F, data[i + 1]
            if n < 192:
                i, ln = i + 2, n
            elif n < 224:
                i, ln = i + 3, ((n - 192) << 8) + data[i + 2] + 192
            else:
                i, ln = i + 6, struct.unpack(">I", data[i + 2:i + 6])[0]
        else:
            tag, size = (hdr >> 2) & 0xF, {0: 1, 1: 2, 2: 4}[hdr & 3]
            ln = int.from_bytes(data[i + 1:i + 1 + size], "big")
            i += 1 + size
        body = data[i:i + ln]
        if tag in (6, 14) and body[:1] == b"\x04":
            out.append(fingerprint(body))
        i += ln
    return out


def signer_from(spec):
    if spec.startswith("kv:"):
        return KeyVaultSigner(spec[3:])
    if spec.startswith("fake:"):
        return FakeSigner(spec[5:])
    raise SystemExit(f"--signer {spec}: use kv:<key id> or fake:<key.pem>")


def key_body(point, created):
    return (bytes([4]) + struct.pack(">I", created) + bytes([PUBKEY_ECDSA, len(P256_OID)])
            + P256_OID + _mpi(point))


def fingerprint(body):
    return hashlib.sha1(b"\x99" + struct.pack(">H", len(body)) + body).digest()


def _signature(signer, sigtype, prefix, fpr, now, extra_hashed=b""):
    hashed = (_subpacket(2, struct.pack(">I", now)) + _subpacket(33, b"\x04" + fpr)
              + extra_hashed)
    head = bytes([4, sigtype, PUBKEY_ECDSA, HASH_SHA256]) + struct.pack(">H", len(hashed)) + hashed
    trailer = b"\x04\xff" + struct.pack(">I", len(head))
    digest = hashlib.sha256(prefix + head + trailer).digest()
    r, s = signer.sign_digest(digest)
    unhashed = _subpacket(16, fpr[-8:])
    body = head + struct.pack(">H", len(unhashed)) + unhashed + digest[:2] + _mpi(r) + _mpi(s)
    return _packet(2, body)


def pubkey(signer, uid, created):
    body = key_body(signer.public_point(), created)
    fpr = fingerprint(body)
    uidb = uid.encode()
    prefix = (b"\x99" + struct.pack(">H", len(body)) + body
              + b"\xb4" + struct.pack(">I", len(uidb)) + uidb)
    # Positive certification: key flags certify + sign; SHA-256 preferred.
    flags = _subpacket(27, b"\x03") + _subpacket(21, bytes([HASH_SHA256]))
    cert = _signature(signer, 0x13, prefix, fpr, created, flags)
    return _packet(6, body) + _packet(13, uidb) + cert, fpr


def sign(signer, created, data, now=None):
    fpr = fingerprint(key_body(signer.public_point(), created))
    return _signature(signer, 0x00, data, fpr, int(now if now is not None else time.time()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pubkey", "sign", "fingerprint"):
        p = sub.add_parser(name)
        p.add_argument("--signer", required=True)
        p.add_argument("--created", type=int, required=True)
        if name == "pubkey":
            p.add_argument("--uid", required=True)
            p.add_argument("--out", required=True)
        if name == "sign":
            p.add_argument("--ring", required=True)
            p.add_argument("--in", dest="inp", required=True)
            p.add_argument("--out", required=True)
    p = sub.add_parser("ring-fingerprints")
    p.add_argument("ring")
    p = sub.add_parser("check-cert")
    p.add_argument("--signer", required=True)
    p.add_argument("--cert", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "ring-fingerprints":
        with open(a.ring, "rb") as f:
            for fp in ring_fingerprints(f.read()):
                print(fp.hex().upper())
        return 0
    signer = signer_from(a.signer)
    if a.cmd == "check-cert":
        if signer.rsa_public() != cert_rsa_public(a.cert):
            raise SystemExit(
                f"the Secure Boot key {a.signer} is not the key of {a.cert}, the certificate "
                "clients enrolled: a rotated key needs a new certificate, enrolled first"
            )
        print("the Secure Boot key matches the enrolled certificate")
        return 0
    if a.cmd == "pubkey":
        ring, fpr = pubkey(signer, a.uid, a.created)
        with open(a.out, "wb") as f:
            f.write(ring)
        print(fpr.hex().upper())
    elif a.cmd == "fingerprint":
        print(fingerprint(key_body(signer.public_point(), a.created)).hex().upper())
    else:
        fpr = fingerprint(key_body(signer.public_point(), a.created))
        with open(a.ring, "rb") as f:
            ring = ring_fingerprints(f.read())
        if fpr not in ring:
            raise SystemExit(
                f"the signer's key ({fpr.hex().upper()}) is not in {a.ring} "
                f"({', '.join(r.hex().upper() for r in ring) or 'empty'}), the keyring images "
                "embed: installed boxes would reject this feed. A rotated key needs a new "
                "keyring in a new image first (docs/design/secure-boot.md, \"Rotating a key\")"
            )
        with open(a.inp, "rb") as f:
            data = f.read()
        sig = sign(signer, a.created, data)
        with open(a.out + ".part", "wb") as f:
            f.write(sig)
        os.replace(a.out + ".part", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
