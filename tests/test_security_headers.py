"""Security headers on API, SPA, docs, error and middleware responses (AST-006).

No database or Node build is needed.
"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

with patch.dict(os.environ, {
    "DATABASE_URL": os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/headers_test"),
    "SECRET_KEY": "security-header-tests-only-signing-key",
}):
    from main import app as backend
from frontend_serving import register_frontend
from security_headers import APP_CSP, DOCS_CSP, SecurityHeadersMiddleware

EXPECTED = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-opener-policy": "same-origin",
}


class SecurityHeaderTests(unittest.TestCase):
    def assert_baseline(self, response, csp=APP_CSP):
        for name, value in EXPECTED.items():
            self.assertEqual(response.headers.get(name), value, name)
        self.assertIn("geolocation=()", response.headers["permissions-policy"])
        self.assertEqual(response.headers["content-security-policy"], csp)
        # Each header appears exactly once.
        for name in (*EXPECTED, "content-security-policy"):
            self.assertEqual(len(response.headers.get_list(name)), 1, name)

    def test_real_app_api_error_and_preflight_responses(self):
        client = TestClient(backend)
        self.addCleanup(client.close)
        cases = {
            "openapi": client.get("/openapi.json"),
            "unauthenticated": client.get("/me"),
            "unknown path": client.get("/definitely-not-a-route"),
            "wrong method": client.delete("/me"),
            "validation error": client.post("/auth/login", json={}),
            "cors preflight": client.options("/auth/login", headers={
                "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}),
        }
        self.assertEqual(cases["unauthenticated"].status_code, 401)
        self.assertEqual(cases["unknown path"].status_code, 404)
        self.assertEqual(cases["validation error"].status_code, 422)
        for label, response in cases.items():
            with self.subTest(label):
                self.assert_baseline(response)
        self.assertNotIn("strict-transport-security", cases["openapi"].headers)

    def test_docs_get_scoped_relaxation_only(self):
        client = TestClient(backend)
        self.addCleanup(client.close)
        for path in ("/docs", "/redoc"):
            with self.subTest(path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assert_baseline(response, DOCS_CSP)
        self.assertNotIn("unsafe-inline", APP_CSP)
        self.assertNotIn("http", APP_CSP)

    def test_spa_html_and_assets_receive_same_origin_policy(self):
        with TemporaryDirectory() as temporary:
            build = Path(temporary) / "dist"
            (build / "assets").mkdir(parents=True)
            (build / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
            (build / "assets" / "index-AbCd1234.js").write_text("1", encoding="utf-8")
            app = FastAPI()
            register_frontend(app, build)
            app.add_middleware(SecurityHeadersMiddleware, enabled=True, hsts=False)
            client = TestClient(app)
            self.addCleanup(client.close)
            for path in ("/", "/login", "/assets/index-AbCd1234.js", "/assets/missing.js"):
                with self.subTest(path):
                    self.assert_baseline(client.get(path))
            # Existing route headers are preserved, not duplicated.
            self.assertEqual(client.get("/").headers["cache-control"], "no-cache")

    def test_route_set_header_is_not_overwritten(self):
        app = FastAPI()

        @app.get("/framed")
        def framed():
            return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

        app.add_middleware(SecurityHeadersMiddleware, enabled=True, hsts=False)
        client = TestClient(app)
        self.addCleanup(client.close)
        response = client.get("/framed")
        self.assertEqual(response.headers.get_list("x-frame-options"), ["SAMEORIGIN"])

    def test_hsts_only_when_enabled_and_flags_validate(self):
        app = FastAPI()
        app.get("/")(lambda: {"ok": True})
        app.add_middleware(SecurityHeadersMiddleware, enabled=True, hsts=True, hsts_max_age=600)
        client = TestClient(app)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/").headers["strict-transport-security"],
                         "max-age=600; includeSubDomains")

        with patch.dict(os.environ, {"HSTS_ENABLED": "maybe"}):
            with self.assertRaises(RuntimeError):
                SecurityHeadersMiddleware(app)
        with patch.dict(os.environ, {"HSTS_ENABLED": "", "SECURITY_HEADERS_ENABLED": ""}):
            middleware = SecurityHeadersMiddleware(app)
            self.assertTrue(middleware.enabled)
            self.assertIsNone(middleware.hsts)

    def test_kill_switch_disables_all_headers(self):
        app = FastAPI()
        app.get("/")(lambda: {"ok": True})
        app.add_middleware(SecurityHeadersMiddleware, enabled=False)
        client = TestClient(app)
        self.addCleanup(client.close)
        response = client.get("/")
        self.assertNotIn("content-security-policy", response.headers)
        self.assertNotIn("x-frame-options", response.headers)


if __name__ == "__main__":
    unittest.main()
