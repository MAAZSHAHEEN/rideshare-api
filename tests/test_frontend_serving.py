"""Serving and API isolation; temporary builds need no Node or database server."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

with patch.dict(os.environ, {
    "DATABASE_URL": os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/frontend_test"),
    "SECRET_KEY": "frontend-serving-tests-only-signing-key",
}):
    from main import app as backend
from frontend_serving import CLIENT_PATHS, register_frontend


class FrontendServingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.build = Path(temporary.name) / "dist"
        (self.build / "assets").mkdir(parents=True)
        self.html = '<!doctype html><html><div id="root">RideShare</div></html>'
        (self.build / "index.html").write_text(self.html, encoding="utf-8")
        (self.build / "assets" / "index-AbCd1234.js").write_text('console.log("test build")', encoding="utf-8")
        for name in ("favicon.svg", "journey.svg"):
            (self.build / name).write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        self.client = self.make_client(self.build)

    def make_client(self, directory):
        # Reuse the actual API routes/dependencies; only the frontend build differs.
        app = FastAPI(lifespan=backend.router.lifespan_context)
        app.router.routes.extend(route for route in backend.routes
                                 if isinstance(route, APIRoute) and route.include_in_schema)
        register_frontend(app, directory)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_root_and_deep_links_serve_html_with_revalidation(self):
        for path in ("/", *CLIENT_PATHS):
            response = self.client.get(path + "?source=test")
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertEqual(response.text, self.html)
            self.assertEqual(response.headers["cache-control"], "no-cache")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            head = self.client.head(path)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b"")

    def test_actual_api_routes_and_docs_remain_accessible(self):
        for path in ("/me", "/rides/", "/rides/me", "/rides/1", "/rides/1/bookings", "/bookings/me"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
            self.assertIn("application/json", response.headers["content-type"])
        for path in ("/auth/register", "/auth/login"):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 422)
            self.assertIsInstance(response.json()["detail"], list)
        for path in ("/docs", "/redoc"):
            self.assertEqual(self.client.get(path).status_code, 200)
        spec = self.client.get("/openapi.json")
        self.assertEqual(spec.status_code, 200)
        self.assertEqual(spec.json()["paths"], backend.openapi()["paths"])
        self.assertNotIn("/login", spec.json()["paths"])

    def test_unknown_api_paths_and_wrong_methods_never_receive_spa_html(self):
        for path in ("/api/not-found", "/auth/not-found", "/auth", "/me/not-found",
                     "/rides/1/unknown", "/bookings/1/unknown", "/docs/unknown",
                     "/unknown-page", "/login/nested", "/index.html", "/.env", "/main.py"):
            response = self.client.get(path, headers={"Accept": "text/html"})
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.json(), {"detail": "Not Found"})
        for method, path in (("POST", "/"), ("POST", "/login"), ("DELETE", "/me")):
            response = self.client.request(method, path)
            self.assertEqual(response.status_code, 405)
            self.assertIn("application/json", response.headers["content-type"])

    def test_hashed_assets_are_immutable_including_conditional_requests(self):
        path = "/assets/index-AbCd1234.js"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        cached = self.client.get(path, headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.headers["cache-control"], response.headers["cache-control"])

    def test_public_assets_revalidate_and_missing_assets_do_not_fall_back(self):
        for name in ("journey.svg", "favicon.svg"):
            response = self.client.get(f"/{name}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-cache")
        for path in ("/assets/missing.js", "/assets/%2e%2e/index.html", "/frontend/src/main.tsx"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("text/html", response.headers.get("content-type", ""))
            self.assertNotIn("immutable", response.headers.get("cache-control", ""))

    def test_missing_build_preserves_backend_only_development(self):
        client = self.make_client(self.build.parent / "absent")
        self.assertEqual(client.get("/").json(), {"message": "welcome to ride share api"})
        self.assertEqual(client.get("/me").status_code, 401)
        self.assertEqual(client.get("/docs").status_code, 200)
        self.assertEqual(client.get("/openapi.json").status_code, 200)
        for path in (*CLIENT_PATHS, "/assets/missing.js", "/journey.svg", "/favicon.svg"):
            self.assertEqual(client.get(path).status_code, 404)
