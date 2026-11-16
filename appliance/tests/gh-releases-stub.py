#!/usr/bin/env python3
"""A local stand-in for GitHub Releases downloads (appliance tests; decision 47).

    python3 gh-releases-stub.py PORT ROOT LOG

ROOT/<owner>/<repo>/<tag>/ holds a release's assets, and ROOT/<owner>/<repo>/LATEST names
the latest release's tag. It answers as github.com does for anonymous downloads:

    /<owner>/<repo>/releases/latest/download/<file>  302 -> .../releases/download/<tag>/<file>
    /<owner>/<repo>/releases/download/<tag>/<file>   302 -> /assets/<owner>/<repo>/<tag>/<file>
                                                     (GitHub sends it on to its asset host)
    /assets/...                                      200, the file
    anything else                                    404

so systemd-sysupdate (systemd-pull, curl) has to follow both redirects, as it will against
GitHub. Each request is logged to LOG. Offline: nothing reaches GitHub.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (self.command, self.path))

    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, head):
        p = unquote(self.path.split("?", 1)[0])
        parts = [x for x in p.split("/") if x]
        if any(x in (".", "..") for x in parts):
            return self.send_error(404)
        if len(parts) == 6 and parts[2:5] == ["releases", "latest", "download"]:
            latest = os.path.join(ROOT, parts[0], parts[1], "LATEST")
            if not os.path.isfile(latest):
                return self.send_error(404)
            tag = open(latest).read().strip()
            return self._redirect(f"/{parts[0]}/{parts[1]}/releases/download/{tag}/{parts[5]}")
        if len(parts) == 6 and parts[2:4] == ["releases", "download"]:
            return self._redirect("/assets/" + "/".join([parts[0], parts[1], parts[4], parts[5]]))
        if len(parts) == 5 and parts[0] == "assets":
            f = os.path.join(ROOT, *parts[1:])
            if not os.path.isfile(f):
                return self.send_error(404)
            size = os.path.getsize(f)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if not head:
                with open(f, "rb") as src:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        self.wfile.write(b)
            return None
        return self.send_error(404)

    def do_GET(self):  # noqa: N802
        self._serve(False)

    def do_HEAD(self):  # noqa: N802
        self._serve(True)


if __name__ == "__main__":
    PORT, ROOT, LOG = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
