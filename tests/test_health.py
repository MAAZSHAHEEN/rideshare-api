"""Liveness/readiness probe tests (AST-010).

Real-database cases need TEST_DATABASE_URL (postgresql+asyncpg://...); Redis
cases need TEST_REDIS_URL pointing at a loopback Redis. Outages are simulated
with an unreachable port, a TCP "black hole" that accepts but never answers,
an exhausted one-connection pool, and a patched engine that raises.
"""

import asyncio
import os
import socket
import time
import unittest
from unittest.mock import MagicMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")

with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "health-tests-only-not-a-production-signing-key",
}):
    import health
    from auth_rate_limit import AuthRateLimiter, RateLimitSettings
    from main import app

SECRET_PASSWORD = "probe-secret-password-must-not-leak"
SECRET_HOST = "db-private-host.invalid"
READY = {"status": "ready", "checks": {"database": "ok", "redis": "disabled"}}
DB_DOWN = {"status": "not_ready", "checks": {"database": "unavailable", "redis": "disabled"}}
FAKE_HMAC = "health-tests-fake-hmac-key-0123456789abcdef"


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def enabled_limiter(redis_url):
    return AuthRateLimiter(RateLimitSettings(
        enabled=True, redis_url=redis_url, hmac_key=FAKE_HMAC,
        client_ip_source="verified-asgi", timeout_seconds=0.5,
    ))


class BlackHole:
    """Accepts TCP connections and never replies (a hung DB or Redis)."""

    async def __aenter__(self):
        self.writers = []
        self.active = self.peak = 0

        async def handle(reader, writer):
            self.writers.append(writer)
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                while await reader.read(1024):  # Until the client hangs up.
                    pass
            finally:
                self.active -= 1

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        for writer in self.writers:
            writer.close()
        self.server.close()


class ProbeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)
        timeout = patch.object(health, "READINESS_TIMEOUT_SECONDS", 0.5)
        timeout.start()
        self.addCleanup(timeout.stop)
        original = app.state.auth_rate_limiter
        self.addCleanup(setattr, app.state, "auth_rate_limiter", original)

    def use_engine(self, engine):
        patcher = patch.object(health.database, "engine", engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_no_leak(self, response, *secrets):
        body = response.text
        for secret in (SECRET_PASSWORD, SECRET_HOST, "postgresql", "asyncpg",
                       "redis://", "Traceback", "Error", "Exception", *secrets):
            self.assertNotIn(secret, body)
        self.assertEqual(response.headers.get("cache-control"), "no-store")


class LivenessTests(ProbeTestCase):
    async def test_healthz_is_constant_and_never_touches_dependencies(self):
        engine = MagicMock()
        engine.connect.side_effect = AssertionError("liveness must not touch the database")
        self.use_engine(engine)
        app.state.auth_rate_limiter = enabled_limiter(f"redis://127.0.0.1:{unused_port()}/0")
        self.addAsyncCleanup(app.state.auth_rate_limiter.close)
        started = time.monotonic()
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                response = await self.client.request(method, "/healthz")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual((await self.client.get("/healthz")).json(), {"status": "ok"})
        self.assertLess(time.monotonic() - started, 0.5)
        engine.connect.assert_not_called()

    async def test_probes_need_no_auth_and_reject_other_methods(self):
        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                self.assertEqual((await self.client.post(path)).status_code, 405)

    async def test_probes_are_not_in_the_public_openapi_schema(self):
        paths = app.openapi()["paths"]
        self.assertNotIn("/healthz", paths)
        self.assertNotIn("/readyz", paths)

    async def test_test_db_diagnostic_is_retired(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual((await self.client.request(method, "/test-db")).status_code, 404)
        self.assertNotIn("/test-db", app.openapi()["paths"])


class ReadinessFailureTests(ProbeTestCase):
    async def test_engine_error_returns_generic_503(self):
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError(
            f"connect to {SECRET_HOST} as user:{SECRET_PASSWORD} failed")
        self.use_engine(engine)
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), DB_DOWN)
        self.assert_no_leak(response)
        self.assertEqual((await self.client.get("/healthz")).status_code, 200)

    async def test_unreachable_database_returns_503(self):
        port = unused_port()
        engine = create_async_engine(
            f"postgresql+asyncpg://probe:{SECRET_PASSWORD}@127.0.0.1:{port}/probe")
        self.addAsyncCleanup(engine.dispose)
        self.use_engine(engine)
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), DB_DOWN)
        self.assert_no_leak(response, str(port), "127.0.0.1")
        self.assertEqual((await self.client.get("/healthz")).json(), {"status": "ok"})

    async def test_unresolvable_host_returns_503_without_hostname(self):
        engine = create_async_engine(
            f"postgresql+asyncpg://probe:{SECRET_PASSWORD}@{SECRET_HOST}:5432/probe")
        self.addAsyncCleanup(engine.dispose)
        self.use_engine(engine)
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), DB_DOWN)
        self.assert_no_leak(response)

    async def test_hung_database_is_cut_off_by_the_deadline(self):
        async with BlackHole() as hole:
            engine = create_async_engine(
                f"postgresql+asyncpg://probe:{SECRET_PASSWORD}@127.0.0.1:{hole.port}/probe")
            self.addAsyncCleanup(engine.dispose)
            self.use_engine(engine)
            started = time.monotonic()
            response = await self.client.get("/readyz")
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), DB_DOWN)
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 2.0)
        self.assert_no_leak(response)

    async def test_concurrent_probes_against_hung_database_stay_bounded(self):
        async with BlackHole() as hole:
            engine = create_async_engine(
                f"postgresql+asyncpg://probe:x@127.0.0.1:{hole.port}/probe",
                pool_size=5, max_overflow=0)
            self.addAsyncCleanup(engine.dispose)
            self.use_engine(engine)
            started = time.monotonic()
            responses = await asyncio.gather(*(self.client.get("/readyz") for _ in range(20)))
            elapsed = time.monotonic() - started
            await asyncio.sleep(0.1)
            # The semaphore lets at most two probes hold DB connections at once.
            self.assertGreaterEqual(hole.peak, 1)
            self.assertLessEqual(hole.peak, health.READINESS_MAX_CONCURRENT_CHECKS)
        self.assertEqual({r.status_code for r in responses}, {503})
        self.assertLess(elapsed, 2.0)

    async def test_invalid_timeout_configuration_is_rejected(self):
        for value in ("0", "-1", "11", "nan", "inf", "abc", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"READINESS_TIMEOUT_SECONDS": value}):
                with self.assertRaises(RuntimeError):
                    health._readiness_timeout()
        for value, expected in (("0.1", 0.1), ("10", 10.0)):
            with patch.dict(os.environ, {"READINESS_TIMEOUT_SECONDS": value}):
                self.assertEqual(health._readiness_timeout(), expected)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("READINESS_TIMEOUT_SECONDS", None)
            self.assertEqual(health._readiness_timeout(), 2.0)


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class ReadinessDatabaseTests(ProbeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=1, max_overflow=0)
        self.addAsyncCleanup(self.engine.dispose)
        self.use_engine(self.engine)

    async def test_healthy_database_is_ready(self):
        app.state.auth_rate_limiter = AuthRateLimiter(RateLimitSettings(enabled=False))
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                response = await self.client.request(method, "/readyz")
                self.assertEqual(response.status_code, 200)
        response = await self.client.get("/readyz")
        self.assertEqual(response.json(), READY)
        self.assert_no_leak(response)

    async def test_exhausted_pool_returns_503_then_recovers(self):
        held = await self.engine.connect()
        try:
            started = time.monotonic()
            response = await self.client.get("/readyz")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), DB_DOWN)
        finally:
            await held.close()
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), READY)

    async def test_slow_query_times_out_and_pool_recovers(self):
        real_execute = None

        async def slow_execute(conn, statement):
            await real_execute(conn, __import__("sqlalchemy").text("SELECT pg_sleep(5)"))

        from sqlalchemy.ext.asyncio import AsyncConnection
        real_execute = AsyncConnection.execute
        with patch.object(AsyncConnection, "execute", slow_execute):
            started = time.monotonic()
            response = await self.client.get("/readyz")
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), DB_DOWN)
        self.assertLess(elapsed, 2.0)
        # The cancelled connection must not poison the one-connection pool.
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)

    @unittest.skipUnless(TEST_REDIS_URL, "Set TEST_REDIS_URL to a loopback Redis")
    async def test_redis_checked_only_when_rate_limiting_enabled(self):
        app.state.auth_rate_limiter = enabled_limiter(TEST_REDIS_URL)
        self.addAsyncCleanup(app.state.auth_rate_limiter.close)
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {"status": "ready", "checks": {"database": "ok", "redis": "ok"}})

    async def test_redis_down_fails_readiness_only_when_enabled(self):
        port = unused_port()
        app.state.auth_rate_limiter = enabled_limiter(f"redis://127.0.0.1:{port}/0")
        self.addAsyncCleanup(app.state.auth_rate_limiter.close)
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready",
                                           "checks": {"database": "ok", "redis": "unavailable"}})
        self.assert_no_leak(response, str(port))
        # Rate limiting must not apply to probes, even with Redis down.
        self.assertEqual((await self.client.get("/healthz")).status_code, 200)

        app.state.auth_rate_limiter = AuthRateLimiter(RateLimitSettings(
            enabled=False, redis_url=f"redis://127.0.0.1:{port}/0"))
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), READY)

    async def test_hung_redis_is_cut_off_by_the_deadline(self):
        async with BlackHole() as hole:
            limiter = enabled_limiter(f"redis://127.0.0.1:{hole.port}/0")
            limiter.redis.connection_pool.connection_kwargs.update(
                socket_timeout=30, socket_connect_timeout=30)
            app.state.auth_rate_limiter = limiter
            self.addAsyncCleanup(limiter.close)
            started = time.monotonic()
            response = await self.client.get("/readyz")
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["redis"], "unavailable")
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
