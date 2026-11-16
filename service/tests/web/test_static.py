"""The SPA's files: index fallback, assets, no path traversal, strict headers."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from omnifile_service.api.db import Catalog
from omnifile_service.web.app import create_app
from omnifile_service.web.config import OidcEndpoints, WebSettings
from omnifile_service.web.context import WebServices


@pytest.fixture
def spa(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>OmniFile</title>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    settings = WebSettings(
        origin="https://portal.test", tenant_id="t", client_id="c",
        oidc=OidcEndpoints.dev("https://portal.test"), static_dir=dist,
    )  # fmt: skip
    services = WebServices(catalog=Catalog("postgresql://unused/x"), settings=settings,
                           auth=None, preview=None)  # fmt: skip
    app = create_app(services, manage_lifecycle=False)
    return TestClient(app, base_url="https://portal.test")


def test_client_routes_get_index_html(spa):
    for path in ("/", "/browse/12", "/policies/3/edit"):
        r = spa.get(path)
        assert r.status_code == 200 and r.text.startswith("<!doctype html>")
        assert r.headers["cache-control"] == "no-cache"
        assert "script-src 'self'" in r.headers["content-security-policy"]


def test_hashed_assets_are_immutable(spa):
    r = spa.get("/assets/app-abc123.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_missing_files_and_traversal_are_404(spa):
    for path in ("/assets/nope.js", "/../secret.txt", "/assets/..%2f..%2fsecret.txt",
                 "/%2e%2e/secret.txt"):  # fmt: skip
        r = spa.get(path)
        assert r.status_code == 404, path
        assert "nope" not in r.text


def test_api_paths_never_fall_back_to_the_spa(spa):
    r = spa.get("/v1/does-not-exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem")
    assert r.headers["cache-control"] == "no-store"
    assert spa.get("/healthz").json() == {"status": "ok"}
