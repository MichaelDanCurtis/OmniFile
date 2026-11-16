#!/usr/bin/env python3
"""A local stand-in for the IAM Roles Anywhere CreateSession endpoint (appliance tests).

    python3 ra-stub.py PORT LOG

It answers POST /sessions with one fixed set of temporary credentials, so
aws_signing_helper can be tested offline: no AWS. Each request is recorded in
LOG as one JSON line: the path, the signing algorithm from Authorization, the
subject of the certificate in X-Amz-X509, the ARNs (query string) and the
session length (JSON body). The test
then checks that the helper signed with the appliance's certificate, for the
ARNs the env file gave it.

The stub does not verify the signature (it has no trust anchor logic): that is
AWS's side, checked on real AWS (docs/milestones/real-aws-checks.md).
"""

import base64
import datetime
import json
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer

ACCESS_KEY = "ASIASTUBROLESANYWHERE"


def subject_of(b64der: str) -> str:
    """The certificate's subject, through openssl (no Python X.509 parser needed)."""
    try:
        der = base64.b64decode(b64der)
        out = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-subject", "-nameopt", "RFC2253"],
            input=der,
            capture_output=True,
            check=True,
        ).stdout.decode()
        return out.strip().removeprefix("subject=")
    except Exception as e:  # noqa: BLE001 - recorded, the test fails on it
        return f"unreadable: {e}"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server's name
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        try:
            req = json.loads(body or b"{}")
        except json.JSONDecodeError:
            req = {}
        auth = self.headers.get("Authorization", "")
        # CreateSession takes the ARNs in the query string and the duration in the body.
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        arg = lambda k: query.get(k) or req.get(k, "")  # noqa: E731
        record = {
            "path": url.path,
            "algorithm": auth.split(" ", 1)[0],
            "x509_subject": subject_of(self.headers.get("X-Amz-X509", "")),
            "trustAnchorArn": arg("trustAnchorArn"),
            "profileArn": arg("profileArn"),
            "roleArn": arg("roleArn"),
            "durationSeconds": req.get("durationSeconds"),
        }
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if url.path != "/sessions":
            self.send_response(404)
            self.end_headers()
            return
        exp = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        out = {
            "credentialSet": [
                {
                    "assumedRoleUser": {
                        "arn": "arn:aws:sts::123456789012:assumed-role/stub/omnifile",
                        "assumedRoleId": "AROASTUB:omnifile",
                    },
                    "credentials": {
                        "accessKeyId": ACCESS_KEY,
                        "secretAccessKey": "stub-secret-not-a-real-key",
                        "sessionToken": "stub-session-token",
                        "expiration": exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    "packedPolicySize": 0,
                    "roleArn": record["roleArn"],
                    "sourceIdentity": record["x509_subject"],
                }
            ],
            "subjectArn": "arn:aws:rolesanywhere:eu-west-1:123456789012:subject/stub",
        }
        data = json.dumps(out).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # quiet
        pass


if __name__ == "__main__":
    PORT, LOG = int(sys.argv[1]), sys.argv[2]
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
