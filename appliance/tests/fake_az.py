#!/usr/bin/env python3
"""A stand-in for `az keyvault key show|sign` (tests of pgp_kv.py's Key Vault path).

It answers as Key Vault does for an EC-HSM P-256 key, from the local key in
$FAKE_AZ_KEY (PKCS#8 PEM), and logs each call to $FAKE_AZ_LOG. No Azure.
    az keyvault key show --id KID -o json          {"key": {"kty": "EC-HSM", "crv": "P-256", ...}}
    az keyvault key sign --algorithm ES256 --digest B64 --id KID -o json
                                                   {"kid": KID, "signature": base64url(r || s)}
"""

import base64
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pgp_kv  # noqa: E402


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main(argv):
    key = os.environ["FAKE_AZ_KEY"]
    with open(os.environ.get("FAKE_AZ_LOG", os.devnull), "a") as log:
        log.write(" ".join(argv) + "\n")
    args = dict(zip(argv[3::2], argv[4::2]))
    if argv[:3] == ["keyvault", "key", "show"]:
        signer = pgp_kv.FakeSigner(key)
        if "BEGIN RSA" in open(key).read() or b"\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01" in \
                subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-outform", "DER"],
                               check=True, capture_output=True).stdout:
            n, e = signer.rsa_public()
            nb = n.to_bytes((n.bit_length() + 7) // 8, "big")
            eb = e.to_bytes((e.bit_length() + 7) // 8, "big")
            print(json.dumps({"key": {"kid": args.get("--id"), "kty": "RSA-HSM",
                                      "n": b64url(nb), "e": b64url(eb)}}))
            return 0
        point = signer.public_point()
        print(json.dumps({"key": {"kid": args.get("--id"), "kty": "EC-HSM", "crv": "P-256",
                                  "x": b64url(point[1:33]), "y": b64url(point[33:])}}))
        return 0
    if argv[:3] == ["keyvault", "key", "sign"]:
        if args.get("--algorithm") != "ES256":
            print("only ES256", file=sys.stderr)
            return 1
        digest = base64.b64decode(args["--digest"])
        der = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", key, "-pkeyopt",
                              "digest:sha256"], input=digest, check=True,
                             capture_output=True).stdout
        r, s = pgp_kv._der_ecdsa_to_rs(der)
        print(json.dumps({"kid": args.get("--id"), "signature": b64url(r + s)}))
        return 0
    print(f"fake az: unsupported {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
