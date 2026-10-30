"""Sanitised 500s, request IDs and the request body limit (AST-007b).

Run: python -B -m unittest tests.test_safe_errors
The PostgreSQL tests need TEST_DATABASE_URL (same rules as test_registration).
All CNIC, phone, password, token and DSN values are obviously fake markers.
"""

import asyncio
import json
import logging
import os
import runpy
import socket
import subprocess
import sys
import time
import traceback
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]

with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/safe_errors_unused",
    "SECRET_KEY": "safe-error-tests-only-not-a-production-signing-key",
}):
    import http_safety
    from config import MAX_REQUEST_BODY_BYTES
    from database import get_db
    from frontend_serving import register_frontend
    from main import app
    from models import User
    from routers import auth

FAKE_CNIC = "99999-9999999-1"
FAKE_CNIC_DIGITS = "9999999999991"
FAKE_PHONE = "03009999991"
FAKE_PASSWORD = "Fake-password-marker-0001!"
FAKE_TOKEN = "fake.jwt.token-not-real-0001"
FAKE_DSN = "postgresql://rideshare:fake-dsn-password-0001@db.internal/rideshare"
FAKE_SECRET = "fake-secret-marker-0001"
GENERIC_500 = {"detail": "Internal server error"}


class CaptureAll(logging.Handler):
    """Root-level capture of every logger, formatted with any traceback."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []
        self.records = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        if record.name.split(".")[0] in ("httpx", "httpcore"):
            return  # The test client's own request log is client-side, not the server's.
        self.records.append(record)
        self.lines.append(self.format(record))

    @property
    def text(self):
        return "\n".join(self.lines)


class LogCaptureMixin:
    def capture_logs(self):
        handler = CaptureAll()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        # Loggers that would carry a traceback or SQL if anything reached them.
        touched = {name: logging.getLogger(name).level
                   for name in ("uvicorn", "uvicorn.error", "sqlalchemy", "asyncpg", "rideshare")}
        for name in touched:
            logging.getLogger(name).setLevel(logging.DEBUG)

        def restore():
            root.removeHandler(handler)
            root.setLevel(old_level)
            for name, level in touched.items():
                logging.getLogger(name).setLevel(level)

        self.addCleanup(restore)
        return handler

    def error_records(self, handler):
        return [json.loads(r.getMessage()) for r in handler.records if r.name == "rideshare.errors"]

    def assert_no_markers(self, text_value, markers):
        for marker in markers:
            self.assertNotIn(marker, text_value)


class SafeErrorTests(LogCaptureMixin, unittest.IsolatedAsyncioTestCase):
    """Real main.app; the database dependency is replaced, nothing connects."""

    async def asyncSetUp(self):
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def override_db(self, dependency):
        overrides = patch.dict(app.dependency_overrides, {get_db: dependency})
        overrides.start()
        self.addCleanup(overrides.stop)

    async def login(self, **headers):
        # ASGITransport re-raises any exception that escapes the app, so a
        # returned response proves Starlette/Uvicorn never saw the exception.
        return await self.client.post("/auth/login", json={
            "email": "fake-user@example.com", "password": FAKE_PASSWORD,
        }, headers={"Authorization": f"Bearer {FAKE_TOKEN}", **headers})

    async def test_runtime_error_with_secret_is_generic_and_logged_safely(self):
        async def broken_db():
            raise RuntimeError(f"connect to {FAKE_DSN} failed; secret={FAKE_SECRET} cnic={FAKE_CNIC}")
            yield  # pragma: no cover

        self.override_db(broken_db)
        logs = self.capture_logs()
        response = await self.login(**{"X-Request-ID": "req-abcdef12"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), GENERIC_500)
        self.assertEqual(response.headers["x-request-id"], "req-abcdef12")
        records = self.error_records(logs)
        self.assertEqual(len(records), 1)
        location = records[0].pop("location")
        self.assertRegex(location, r"^tests/test_safe_errors\.py:[0-9]+:broken_db$")
        self.assertEqual(records[0], {
            "event": "unhandled_exception", "request_id": "req-abcdef12", "method": "POST",
            "route": "/auth/login", "exc_type": "builtins.RuntimeError",
        })
        self.assertTrue(all(r.exc_info is None for r in logs.records))
        markers = [FAKE_DSN, "fake-dsn-password", FAKE_SECRET, FAKE_CNIC, FAKE_PASSWORD, FAKE_TOKEN, "Traceback"]
        self.assert_no_markers(logs.text, markers)
        self.assert_no_markers(response.text, markers)

    async def test_synthetic_unmapped_integrity_error_logs_only_sqlstate_and_constraint(self):
        class Cause(Exception):
            sqlstate = "23505"
            constraint_name = "uq_fake_unmapped"

        class Adapted(Exception):
            sqlstate = "23505"

        async def broken_db():
            cause = Cause(f'DETAIL:  Key (phone_number)=({FAKE_PHONE}) already exists. cnic {FAKE_CNIC}')
            orig = Adapted(str(cause))
            orig.__cause__ = cause
            raise IntegrityError("INSERT INTO users", {"cnic": FAKE_CNIC, "password": FAKE_PASSWORD}, orig)
            yield  # pragma: no cover

        self.override_db(broken_db)
        logs = self.capture_logs()
        response = await self.login()
        self.assertEqual((response.status_code, response.json()), (500, GENERIC_500))
        [record] = self.error_records(logs)
        self.assertEqual(record["exc_type"], "sqlalchemy.exc.IntegrityError")
        self.assertEqual((record["sqlstate"], record["constraint"], record["db_error"]),
                         ("23505", "uq_fake_unmapped", "Cause"))
        self.assertEqual(len(record["request_id"]), 36)  # Minted uuid4.
        self.assertEqual(response.headers["x-request-id"], record["request_id"])
        self.assert_no_markers(logs.text + response.text, [FAKE_PHONE, FAKE_CNIC, FAKE_PASSWORD, "DETAIL", "Key ("])

    async def test_route_template_logged_not_raw_path(self):
        async def broken_db():
            raise RuntimeError(FAKE_SECRET)
            yield  # pragma: no cover

        self.override_db(broken_db)
        logs = self.capture_logs()
        response = await self.client.get("/rides/987654321", params={"q": FAKE_PHONE})
        self.assertEqual(response.status_code, 500)
        [record] = self.error_records(logs)
        self.assertEqual((record["method"], record["route"]), ("GET", "/rides/{ride_id}"))
        self.assert_no_markers(logs.text, ["987654321", FAKE_PHONE, FAKE_SECRET])

    async def test_hostile_db_identifiers_are_not_logged_verbatim(self):
        class Cause(Exception):
            sqlstate = "23505"
            constraint_name = f"uq_x\n{FAKE_CNIC}"

        class Adapted(Exception):
            sqlstate = f"2350{FAKE_CNIC}"

        async def broken_db():
            orig = Adapted("x")
            orig.__cause__ = Cause("x")
            raise IntegrityError("INSERT", {}, orig)
            yield  # pragma: no cover

        self.override_db(broken_db)
        logs = self.capture_logs()
        await self.login()
        [record] = self.error_records(logs)
        self.assertIsNone(record["sqlstate"])
        self.assertEqual(record["constraint"], "<invalid>")
        self.assertNotIn(FAKE_CNIC, logs.text)

    async def test_exception_in_middleware_layer_is_caught_by_outer_layer(self):
        async def exploding_coarse(scope, operation):
            raise RuntimeError(f"redis://:{FAKE_SECRET}@cache.internal down")

        logs = self.capture_logs()
        with patch.object(app.state.auth_rate_limiter, "coarse", exploding_coarse):
            response = await self.login()
        self.assertEqual((response.status_code, response.json()), (500, GENERIC_500))
        [record] = self.error_records(logs)
        self.assertEqual(record["route"], "<unmatched>")
        self.assertTrue(record["location"].endswith(":exploding_coarse"), record["location"])
        self.assertNotIn(FAKE_SECRET, logs.text)

    async def test_http_and_validation_errors_are_untouched(self):
        async def teapot_db():
            raise HTTPException(status_code=418, detail="teapot")
            yield  # pragma: no cover

        logs = self.capture_logs()
        invalid = await self.client.post("/auth/login", json={"email": "not-an-email", "password": FAKE_PASSWORD})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(set(invalid.json()["detail"][0]), {"type", "loc", "msg"})
        missing = await self.client.get("/no-such-route")
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Not Found"}))
        unauthenticated = await self.client.get("/me")
        self.assertEqual(unauthenticated.status_code, 401)
        self.override_db(teapot_db)
        response = await self.login()
        self.assertEqual((response.status_code, response.json()), (418, {"detail": "teapot"}))
        self.assertEqual(self.error_records(logs), [])

    async def test_request_id_validation(self):
        for inbound, echoed in (("abcd1234", True), ("A" * 64, True), ("abc1234", False),
                                ("A" * 65, False), ("bad id 1234", False), ("abcd1234\r\nX: y", False)):
            with self.subTest(inbound=inbound):
                response = await self.client.get("/healthz", headers={"X-Request-ID": inbound}) \
                    if "\n" not in inbound else await self.raw_get_with_header(inbound)
                returned = response.headers["x-request-id"]
                self.assertEqual(returned == inbound, echoed)
                if not echoed:
                    self.assertRegex(returned, r"^[0-9a-f-]{36}$")

    async def raw_get_with_header(self, value):
        # httpx refuses CR/LF in headers; drive the ASGI app directly.
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                   "scheme": "http", "path": "/healthz", "raw_path": b"/healthz", "query_string": b"",
                   "root_path": "", "headers": [(b"x-request-id", value.encode())],
                   "client": ("127.0.0.1", 1), "server": ("test", 80)}, receive, send)
        start = messages[0]

        class R:
            headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return R

    async def test_cors_headers_present_on_generic_500(self):
        async def broken_db():
            raise RuntimeError(FAKE_SECRET)
            yield  # pragma: no cover

        self.override_db(broken_db)
        self.capture_logs()
        with patch("starlette.middleware.cors.CORSMiddleware.is_allowed_origin", return_value=True):
            response = await self.client.post(
                "/auth/login", json={"email": "fake-user@example.com", "password": FAKE_PASSWORD},
                headers={"Origin": "https://frontend.example"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("access-control-allow-origin", response.headers)
        self.assertIn("X-Request-ID", response.headers["access-control-expose-headers"])


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class RealIntegrityErrorTests(LogCaptureMixin, unittest.IsolatedAsyncioTestCase):
    """Induce real PostgreSQL violations whose DETAIL quotes CNIC/phone/hash."""

    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)
        self.connection = await engine.connect()
        self.addAsyncCleanup(self.connection.close)
        transaction = await self.connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        schema = f"test_safe_errors_{uuid4().hex}"
        await self.connection.execute(CreateSchema(schema))
        await self.connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        await self.connection.run_sync(User.__table__.create)
        # Test-only constraints the register route does not map to 409.
        await self.connection.execute(text("CREATE UNIQUE INDEX uq_test_users_phone ON users (phone_number)"))
        await self.connection.execute(text(
            f"ALTER TABLE users ADD CONSTRAINT ck_test_users_cnic CHECK (cnic <> '{FAKE_CNIC_DIGITS}')"))

        async def override_get_db():
            async with AsyncSession(bind=self.connection, expire_on_commit=False,
                                    join_transaction_mode="create_savepoint") as session:
                yield session

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

        # Positive control: record the full exception text the handler was given.
        self.raw_exception_text = []
        real_log = http_safety.log_unhandled

        def spy(exc, scope):
            self.raw_exception_text.append("".join(traceback.format_exception(exc)))
            return real_log(exc, scope)

        spy_patch = patch.object(http_safety, "log_unhandled", spy)
        spy_patch.start()
        self.addCleanup(spy_patch.stop)

    def payload(self, **overrides):
        identifier = uuid4().hex
        return {"name": "Fake Person", "email": f"fake-{identifier}@example.com",
                "password": FAKE_PASSWORD, "cnic": str(int(identifier[:12], 16) % 10**13).zfill(13),
                "phone_number": f"0300{int(identifier[12:20], 16) % 10**7:07d}", **overrides}

    async def test_unmapped_unique_violation_on_phone(self):
        first = await self.client.post("/auth/register", json=self.payload(phone_number=FAKE_PHONE))
        self.assertEqual(first.status_code, 201, first.text)
        logs = self.capture_logs()
        response = await self.client.post("/auth/register", json=self.payload(phone_number=FAKE_PHONE))
        self.assertEqual((response.status_code, response.json()), (500, GENERIC_500))
        [record] = self.error_records(logs)
        self.assertEqual((record["exc_type"], record["sqlstate"], record["constraint"], record["route"]),
                         ("sqlalchemy.exc.IntegrityError", "23505", "uq_test_users_phone", "/auth/register"))
        self.assertEqual(record["db_error"], "UniqueViolationError")
        self.assertIn(FAKE_PHONE, self.raw_exception_text[0])  # The trap was real.
        self.assert_no_markers(logs.text + response.text, [FAKE_PHONE, FAKE_PASSWORD, "DETAIL", "Key (", "$2b$"])

    async def test_unmapped_check_violation_failing_row_with_cnic_and_hash(self):
        logs = self.capture_logs()
        payload = self.payload(cnic=FAKE_CNIC_DIGITS, phone_number=FAKE_PHONE)
        response = await self.client.post("/auth/register", json=payload)
        self.assertEqual((response.status_code, response.json()), (500, GENERIC_500))
        [record] = self.error_records(logs)
        self.assertEqual((record["sqlstate"], record["constraint"], record["db_error"]),
                         ("23514", "ck_test_users_cnic", "CheckViolationError"))
        raw = self.raw_exception_text[0]
        for marker in (FAKE_CNIC_DIGITS, FAKE_PHONE, "$2b$", "Failing row"):
            self.assertIn(marker, raw)  # The trap was real.
        self.assert_no_markers(logs.text + response.text,
                               [FAKE_CNIC_DIGITS, FAKE_PHONE, FAKE_PASSWORD, payload["email"], "$2b$",
                                "Failing row", "DETAIL"])

    async def assert_commit_time_conflict(self, column, constraint):
        payload = self.payload()
        conflicting = self.payload()
        conflicting[column] = payload[column]

        async def race(func, *args):
            # Another writer inserts the duplicate after the precheck, before commit.
            await self.connection.execute(User.__table__.insert().values(
                name="Racer", email=conflicting["email"], password="x", cnic=conflicting["cnic"],
                phone_number=conflicting["phone_number"], role="passenger"))
            return func(*args)

        logs = self.capture_logs()
        with patch.object(auth, "run_in_threadpool", race):
            response = await self.client.post("/auth/register", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "Registration details already in use"})
        self.assertEqual(self.error_records(logs), [])
        self.assertEqual(self.raw_exception_text, [])
        return constraint

    async def test_mapped_email_conflict_at_commit_still_409(self):
        await self.assert_commit_time_conflict("email", "ix_users_email")

    async def test_mapped_cnic_conflict_at_commit_still_409(self):
        await self.assert_commit_time_conflict("cnic", "users_cnic_key")


async def echo_app(scope, receive, send):
    """Reads the whole body and reports its length."""
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = json.dumps({"received": size}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimitUnitTests(unittest.IsolatedAsyncioTestCase):
    LIMIT = 1024

    async def call(self, chunks, content_length=None, app_=None):
        middleware = http_safety.RequestBodyLimitMiddleware(app_ or echo_app, max_bytes=self.LIMIT)
        headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
        pending = list(chunks) or [b""]
        sent = []
        app_calls = []

        async def receive():
            body = pending.pop(0) if pending else b""
            app_calls.append(len(body))
            return {"type": "http.request", "body": body, "more_body": bool(pending)}

        async def send(message):
            sent.append(message)

        await middleware({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive, send)
        status = sent[0]["status"]
        body = b"".join(m.get("body", b"") for m in sent[1:])
        return status, body, app_calls

    async def test_declared_content_length_boundaries(self):
        for size, expected in ((self.LIMIT - 1, 200), (self.LIMIT, 200), (self.LIMIT + 1, 413)):
            with self.subTest(size=size):
                status, body, reads = await self.call([b"x" * size], content_length=size)
                self.assertEqual(status, expected)
                if expected == 413:
                    self.assertEqual(json.loads(body), {"detail": "Request body too large"})
                    self.assertEqual(reads, [])  # Rejected before reading the body.
                else:
                    self.assertEqual(json.loads(body), {"received": size})

    async def test_chunked_streaming_boundaries(self):
        for size, expected in ((self.LIMIT - 1, 200), (self.LIMIT, 200), (self.LIMIT + 1, 413)):
            with self.subTest(size=size):
                chunks = [b"y" * 100] * (size // 100) + [b"y" * (size % 100)]
                status, body, _ = await self.call(chunks)
                self.assertEqual(status, expected)
                if expected == 200:
                    self.assertEqual(json.loads(body), {"received": size})

    async def test_under_declared_content_length_is_still_enforced(self):
        status, _, reads = await self.call([b"z" * 600, b"z" * 600], content_length=10)
        self.assertEqual(status, 413)
        self.assertEqual(sum(reads), 1200)  # Stopped at the first over-limit chunk.

    async def test_app_that_swallows_the_read_error_still_gets_413(self):
        async def swallowing(scope, receive, send):
            try:
                while (await receive()).get("more_body"):
                    pass
            except Exception:
                pass
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"bad body"})

        status, body, _ = await self.call([b"a" * 800, b"a" * 800], app_=swallowing)
        self.assertEqual((status, json.loads(body)), (413, {"detail": "Request body too large"}))

    async def test_overflow_propagating_through_safe_error_layer_is_413_not_500(self):
        # An app that lets the read error escape: SafeErrorMiddleware must pass it
        # through to the limiter rather than log it as an unhandled 500.
        inner = http_safety.SafeErrorMiddleware(echo_app)
        with self.assertNoLogs("rideshare.errors"):
            status, body, _ = await self.call([b"b" * 800, b"b" * 800], app_=inner)
        self.assertEqual((status, json.loads(body)), (413, {"detail": "Request body too large"}))

    def test_rejects_invalid_limits(self):
        for value in (0, -1, 1.5, "65536", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    http_safety.RequestBodyLimitMiddleware(echo_app, max_bytes=value)


class BodyLimitAppTests(LogCaptureMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_calls = 0

        async def teapot_db():
            self.db_calls += 1  # Reached only after FastAPI read the full body.
            raise HTTPException(status_code=418, detail="reached route")
            yield  # pragma: no cover

        overrides = patch.dict(app.dependency_overrides, {get_db: teapot_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def login_body(self, size):
        base = json.dumps({"email": "fake-user@example.com", "password": FAKE_PASSWORD, "pad": ""})
        body = base[:-3] + '"' + "p" * (size - len(base)) + '"}'
        self.assertEqual(len(body.encode()), size)
        return body.encode()

    async def stream(self, body):
        for start in range(0, len(body), 4096):
            yield body[start:start + 4096]

    def test_default_limit_is_64_kib(self):
        self.assertEqual(MAX_REQUEST_BODY_BYTES, 65536)

    async def test_boundaries_on_real_app_content_length_and_chunked(self):
        logs = self.capture_logs()
        for chunked in (False, True):
            for size, expected in ((MAX_REQUEST_BODY_BYTES - 1, 418), (MAX_REQUEST_BODY_BYTES, 418),
                                   (MAX_REQUEST_BODY_BYTES + 1, 413)):
                with self.subTest(chunked=chunked, size=size):
                    body = self.login_body(size)
                    content = self.stream(body) if chunked else body
                    response = await self.client.post(
                        "/auth/login", content=content, headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, expected, response.text)
                    if expected == 413:
                        self.assertEqual(response.json(), {"detail": "Request body too large"})
                        self.assertIn("x-request-id", response.headers)
        self.assertEqual(self.db_calls, 4)
        self.assertEqual(self.error_records(logs), [])  # 413 is not an unhandled error.

    async def test_health_probes_and_frontend_unaffected(self):
        for method in ("GET", "HEAD"):
            response = await self.client.request(method, "/healthz")
            self.assertEqual(response.status_code, 200)
        oversized = await self.client.request("GET", "/healthz", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
        self.assertEqual(oversized.status_code, 413)

    async def test_large_static_responses_are_not_limited(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            (dist / "assets").mkdir()
            (dist / "index.html").write_text("<!doctype html><title>fake</title>")
            (dist / "assets" / "app-abc123.js").write_bytes(b"/*x*/" * 60000)  # 300 KB
            frontend = FastAPI()
            register_frontend(frontend, dist)
            frontend.add_middleware(http_safety.RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
            async with AsyncClient(transport=ASGITransport(app=frontend), base_url="http://test") as client:
                index = await client.get("/")
                asset = await client.get("/assets/app-abc123.js")
                head = await client.head("/login")
        self.assertEqual(index.status_code, 200)
        self.assertEqual((asset.status_code, len(asset.content)), (200, 300000))
        self.assertEqual(head.status_code, 200)


class BodyLimitConfigTests(unittest.TestCase):
    def load(self, value):
        values = {"SECRET_KEY": "config-tests-only-not-a-production-signing-key-0001",
                  "ALGORITHM": "HS256", "ACCESS_TOKEN_EXPIRE_MINUTES": "60"}
        if value is not None:
            values["MAX_REQUEST_BODY_BYTES"] = value
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, values, clear=True):
            return runpy.run_path(str(ROOT / "config.py"))["MAX_REQUEST_BODY_BYTES"]

    def test_valid_values(self):
        self.assertEqual(self.load(None), 65536)
        for value, expected in (("1024", 1024), ("10485760", 10485760), (" 131072 ", 131072)):
            with self.subTest(value=value):
                self.assertEqual(self.load(value), expected)

    def test_invalid_values_fail_startup(self):
        for value in ("", "abc", "-1", "0", "1023", "10485761", "1e5", "64k", "6.5", "６５５３６"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "MAX_REQUEST_BODY_BYTES"):
                    self.load(value)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RealUvicornLogTests(unittest.TestCase):
    """Uvicorn logs 'Exception in ASGI application' with the full chain if an
    exception escapes; prove nothing escapes and no marker reaches stderr."""

    def setUp(self):
        self.port = free_port()
        env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/unused",
               "SECRET_KEY": "uvicorn-test-only-not-a-production-signing-key-0001",
               "AUTH_RATE_LIMIT_ENABLED": "false", "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'tests'}",
               "PYTHONUNBUFFERED": "1"}
        env.pop("MAX_REQUEST_BODY_BYTES", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "safe_errors_server_app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--lifespan", "off", "--log-level", "debug"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        self.fail("uvicorn did not start: " + self.stop())

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc.stdout.closed:
            return ""
        output = self.proc.stdout.read().decode(errors="replace")
        self.proc.stdout.close()
        return output

    def post(self, kind):
        body = json.dumps({"email": "fake-user@example.com", "password": FAKE_PASSWORD}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/auth/login", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Fail-Kind": kind,
                     "Authorization": "Bearer fake.jwt.token-not-real-0001"})
        try:
            # Bypass any HTTP(S)_PROXY in the environment: talk to localhost directly.
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        self.fail("expected an HTTP error")

    def raw_chunked(self, total):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(b"POST /auth/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            sent = 0
            try:
                while sent < total:
                    chunk = b"q" * min(8192, total - sent)
                    sock.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    sent += len(chunk)
                sock.sendall(b"0\r\n\r\n")
            except OSError:
                pass  # Server may close early after answering 413.
            response = b""
            while True:
                try:
                    data = sock.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                response += data
        return response

    def test_no_traceback_or_markers_reach_uvicorn_output(self):
        for kind in ("runtime", "integrity"):
            status, body = self.post(kind)
            self.assertEqual((status, json.loads(body)), (500, GENERIC_500))
        oversized = self.raw_chunked(MAX_REQUEST_BODY_BYTES + 100_000)
        self.assertTrue(oversized.startswith(b"HTTP/1.1 413"), oversized[:100])
        output = self.stop()
        self.assertEqual(output.count('"event": "unhandled_exception"'), 2, output)
        self.assertIn('"sqlstate": "23505"', output)
        self.assertIn('"constraint": "uq_fake_unmapped_phone"', output)
        for marker in ("Traceback", "Exception in ASGI application", "99999-9999999-1", "+923009999991",
                       "fake-dsn-password-not-real-0001", "fake.jwt.token-not-real-0001", FAKE_PASSWORD,
                       "DETAIL", "postgresql://"):
            self.assertNotIn(marker, output)


if __name__ == "__main__":
    unittest.main()
