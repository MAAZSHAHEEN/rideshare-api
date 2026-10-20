"""Real disposable Redis/PostgreSQL tests; never use production service URLs."""

import asyncio
from dataclasses import replace
import os
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

with patch.dict(os.environ, {
    "DATABASE_URL": os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/unused"),
    "SECRET_KEY": "rate-limit-tests-only-not-a-production-signing-key",
    "AUTH_RATE_LIMIT_ENABLED": "false",
}):
    from auth_rate_limit import AuthRateLimiter, AuthRateLimitMiddleware, RateLimitSettings
    from database import get_db
    from models import User
    from routers import auth

REDIS_URL = os.getenv("TEST_REDIS_URL")
DATABASE_URL = os.getenv("TEST_DATABASE_URL")


class RateLimitConfigurationTests(unittest.TestCase):
    def test_invalid_timeout_types_and_values_are_secret_safe(self):
        for value in (None, True, "sensitive-timeout", [], {}, 10**1000,
                      float("nan"), float("inf"), float("-inf"), 0, -1, 6):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(RuntimeError, "^Rate-limit timeout") as caught:
                    RateLimitSettings(timeout_seconds=value)
                self.assertNotIn("sensitive", str(caught.exception))

    def test_invalid_environment_timeout_is_secret_safe(self):
        for value in ("sensitive-timeout", "", "nan", "inf", "0", "-1", "6"):
            with patch.dict(os.environ, {
                "AUTH_RATE_LIMIT_ENABLED": "false",
                "AUTH_RATE_LIMIT_TIMEOUT_SECONDS": value,
            }):
                with self.assertRaisesRegex(RuntimeError, "^Rate-limit timeout") as caught:
                    RateLimitSettings.from_environment()
                self.assertNotIn("sensitive", str(caught.exception))

    def test_disabled_by_default_and_enabling_requires_verified_boundary(self):
        self.assertFalse(RateLimitSettings().enabled)
        with self.assertRaises(RuntimeError):
            RateLimitSettings(enabled=True)

    def test_invalid_configuration_fails_without_echoing_values(self):
        for change in (
            {"login_limit": 0}, {"timeout_seconds": float("nan")},
            {"timeout_seconds": 6}, {"namespace": "unsafe/email@example.com"},
            {"enabled": True, "client_ip_source": "verified-asgi",
             "hmac_key": "short-sensitive"},
            {"enabled": True, "client_ip_source": "verified-asgi",
             "hmac_key": "test-only-hmac-key-with-at-least-32-bytes",
             "redis_url": "redis://sensitive-password@external.example"},
        ):
            with self.subTest(change=list(change)):
                with self.assertRaises(RuntimeError) as caught:
                    RateLimitSettings(**change)
                self.assertNotIn("sensitive", str(caught.exception))


@unittest.skipUnless(REDIS_URL and DATABASE_URL,
                     "Set TEST_REDIS_URL and TEST_DATABASE_URL to disposable local services")
class AuthRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for url in (REDIS_URL, DATABASE_URL):
            if urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise RuntimeError("Rate-limit tests require disposable loopback services")
        self.settings = RateLimitSettings(
            enabled=True, client_ip_source="verified-asgi", redis_url=REDIS_URL,
            hmac_key="disposable-test-hmac-key-at-least-32-bytes",
            namespace=f"test:auth:{uuid4().hex}", login_limit=3, register_limit=2,
            login_identity_limit=2, timeout_seconds=2,
        )
        self.limiter = AuthRateLimiter(self.settings)
        self.addAsyncCleanup(self.limiter.close)
        await self.limiter.redis.ping()
        self.addAsyncCleanup(self.clear_keys)
        self.schema = f"test_auth_limits_{uuid4().hex}"
        self.engine = create_async_engine(
            DATABASE_URL, connect_args={"server_settings": {"search_path": self.schema}},
        )
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.execute(CreateSchema(self.schema))
            await connection.run_sync(User.__table__.create)
        self.addAsyncCleanup(self.drop_schema)
        self.app = FastAPI()
        self.app.state.auth_rate_limiter = self.limiter
        self.app.add_middleware(AuthRateLimitMiddleware)
        self.app.add_middleware(
            CORSMiddleware, allow_origins=["http://localhost:5173"],
            allow_methods=["POST"], allow_headers=["Content-Type"],
            expose_headers=["Retry-After"],
        )
        self.app.include_router(auth.router)
        self.sql_calls = 0

        class CountingSession(AsyncSession):
            async def execute(session, *args, **kwargs):
                self.sql_calls += 1
                return await super().execute(*args, **kwargs)

        async def test_db():
            async with CountingSession(self.engine, expire_on_commit=False) as session:
                yield session

        self.app.dependency_overrides[get_db] = test_db
        self.client = self.client_for(self.app)

    def client_for(self, app, host="192.0.2.1"):
        client = AsyncClient(
            transport=ASGITransport(app=app, client=(host, 1234)),
            base_url="http://test",
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def clear_keys(self):
        keys = [key async for key in self.limiter.redis.scan_iter(
            match=self.settings.namespace + ":*")]
        if keys:
            await self.limiter.redis.delete(*keys)

    async def drop_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True))

    async def login(self, email="missing@example.com", client=None):
        return await (client or self.client).post(
            "/auth/login", json={"email": email, "password": "Test-password-123!"},
        )

    async def test_malformed_requests_count_before_validation_and_have_safe_429(self):
        for _ in range(3):
            response = await self.client.post("/auth/login", content="{")
            self.assertEqual(response.status_code, 422)
        response = await self.client.post("/auth/login", content="{")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": "Too many authentication attempts"})
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.sql_calls, 0)

    async def test_registration_has_separate_limit_and_no_writes_when_denied(self):
        for _ in range(2):
            self.assertEqual((await self.client.post("/auth/register", json={})).status_code, 422)
        self.assertEqual((await self.client.post("/auth/register", json={})).status_code, 429)
        self.assertEqual((await self.client.post("/auth/login", json={})).status_code, 422)
        async with AsyncSession(self.engine) as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(User)), 0)

    async def test_identity_gate_precedes_sql_and_bcrypt(self):
        with patch.object(auth, "verify_password", return_value=False) as verify:
            self.assertEqual((await self.login()).status_code, 401)
            self.assertEqual((await self.login()).status_code, 401)
            calls = self.sql_calls
            self.assertEqual((await self.login()).status_code, 429)
            self.assertEqual(self.sql_calls, calls)
            self.assertEqual(verify.call_count, 2)

    async def test_registration_and_correct_login_keep_existing_contract(self):
        payload = dict(name="Test", email="valid@example.com", password="Test-password-123!",
                       cnic="1234567890123", phone_number="03001234567", role="passenger")
        registered = await self.client.post("/auth/register", json=payload)
        self.assertEqual(registered.status_code, 201)
        self.assertNotIn("password", registered.json())
        response = await self.login("valid@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"access_token", "token_type"})

    async def test_atomic_threshold_across_independent_clients(self):
        second = AuthRateLimiter(self.settings)
        self.addAsyncCleanup(second.close)
        self.assertNotEqual(await self.limiter.redis.client_id(), await second.redis.client_id())

        async def attempt(limiter):
            try:
                await limiter.coarse({"client": ("192.0.2.1", 1)}, "login")
                return 200
            except HTTPException as exc:
                return exc.status_code

        results = await asyncio.wait_for(asyncio.gather(
            *(attempt(self.limiter if i % 2 else second) for i in range(20)),
        ), timeout=10)
        self.assertEqual(results.count(200), 3)
        self.assertEqual(results.count(429), 17)
        # A new instance sees the same counter; restart does not reset the quota.
        self.assertEqual(await attempt(second), 429)

    async def test_expiry_uses_real_redis_and_denial_does_not_extend_ttl(self):
        limiter = AuthRateLimiter(replace(self.settings, login_limit=1, login_window_seconds=1))
        self.addAsyncCleanup(limiter.close)
        scope = {"client": ("192.0.2.1", 1)}
        await limiter.coarse(scope, "login")
        key = limiter.key("login", ["192.0.2.1"])
        before = await limiter.redis.pttl(key)
        with self.assertRaises(HTTPException) as caught:
            await limiter.coarse(scope, "login")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertLessEqual(await limiter.redis.pttl(key), before)
        async with asyncio.timeout(4):
            while await limiter.redis.exists(key):
                await asyncio.sleep(0.02)
        await limiter.coarse(scope, "login")

    async def test_cors_preflight_is_not_counted_and_429_has_cors(self):
        headers = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}
        for _ in range(4):
            self.assertEqual((await self.client.options("/auth/login", headers=headers)).status_code, 200)
        for _ in range(3):
            self.assertEqual((await self.client.post("/auth/login", json={})).status_code, 422)
        response = await self.client.post("/auth/login", json={}, headers={"Origin": headers["Origin"]})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], headers["Origin"])
        self.assertIn("Retry-After", response.headers["Access-Control-Expose-Headers"])

    async def test_malformed_registration_json_is_counted(self):
        for _ in range(2):
            self.assertEqual((await self.client.post("/auth/register", content="{")).status_code, 422)
        self.assertEqual((await self.client.post("/auth/register", content="{")).status_code, 429)
        self.assertEqual(self.sql_calls, 0)

    async def test_login_pair_is_normalized_but_separate_for_other_emails_and_ips(self):
        scope = {"client": ("192.0.2.1", 1)}
        await self.limiter.login_identity(scope, "Person@Example.com")
        await self.limiter.login_identity(scope, "person@example.com")
        with self.assertRaises(HTTPException) as caught:
            await self.limiter.login_identity(scope, "PERSON@example.com")
        self.assertEqual(caught.exception.status_code, 429)
        await self.limiter.login_identity(scope, "other@example.com")
        await self.limiter.login_identity({"client": ("192.0.2.2", 1)}, "person@example.com")

    async def test_spoofed_headers_cannot_change_untrusted_client(self):
        client = self.client_for(ProxyHeadersMiddleware(self.app, trusted_hosts=["127.0.0.1"]))
        for i in range(3):
            response = await client.post("/auth/login", json={}, headers={
                "X-Forwarded-For": f"198.51.100.{i + 1}",
                "CF-Connecting-IP": f"198.51.100.{i + 1}",
            })
            self.assertEqual(response.status_code, 422)
        self.assertEqual((await client.post("/auth/login", json={})).status_code, 429)
        other = self.client_for(self.app, "192.0.2.2")
        self.assertEqual((await other.post("/auth/login", json={})).status_code, 422)

    async def test_trusted_server_boundary_and_ipv6_normalization(self):
        client = self.client_for(
            ProxyHeadersMiddleware(self.app, trusted_hosts=["127.0.0.1"]), "127.0.0.1")
        for _ in range(3):
            self.assertEqual((await client.post("/auth/login", json={},
                             headers={"X-Forwarded-For": "2001:db8::1"})).status_code, 422)
        direct = self.client_for(self.app, "2001:0db8:0:0:0:0:0:1")
        self.assertEqual((await direct.post("/auth/login", json={})).status_code, 429)
        self.assertEqual(self.limiter.client_ip({"client": ("::ffff:192.0.2.1", 1)}), "192.0.2.1")

    async def test_sensitive_identifiers_are_hmac_only_in_redis(self):
        await self.limiter.login_identity({"client": ("192.0.2.1", 1)}, "Private@Example.com")
        keys = [key.decode() async for key in self.limiter.redis.scan_iter(
            match=self.settings.namespace + ":*")]
        self.assertEqual(len(keys), 1)
        self.assertNotIn("192.0.2.1", keys[0])
        self.assertNotIn("example", keys[0].lower())
        self.assertRegex(keys[0].split(":")[-1], r"^[0-9a-f]{64}$")
        self.assertEqual(keys[0], self.limiter.key("login-identity",
                                                 ["192.0.2.1", "private@example.com"]))

    async def test_unavailable_redis_fails_closed_bounded_with_cors(self):
        # A local server that accepts TCP but never responds exercises real timeout.
        async def stall(reader, writer):
            try:
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(stall, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        limiter = AuthRateLimiter(replace(
            self.settings, redis_url=f"redis://127.0.0.1:{port}", timeout_seconds=0.1))
        self.addAsyncCleanup(limiter.close)
        with patch.object(self.app.state, "auth_rate_limiter", limiter):
            start = time.monotonic()
            response = await self.client.post("/auth/login", json={},
                                             headers={"Origin": "http://localhost:5173"})
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "Authentication temporarily unavailable"})
            self.assertIn("Access-Control-Allow-Origin", response.headers)
            self.assertEqual(self.sql_calls, 0)
            self.assertEqual((await self.client.get("/openapi.json")).status_code, 200)
        self.assertEqual((await self.client.post("/auth/login", json={})).status_code, 422)

    async def test_missing_client_fails_closed_and_disabled_requires_no_redis(self):
        with self.assertRaises(HTTPException) as caught:
            await self.limiter.coarse({"client": None}, "login")
        self.assertEqual(caught.exception.status_code, 503)
        disabled = AuthRateLimiter(RateLimitSettings())
        self.assertIsNone(disabled.redis)
        await disabled.coarse({"client": None}, "login")

    async def test_nonexpiring_counter_preserves_count_and_repairs_expiry(self):
        scope = {"client": ("192.0.2.1", 1)}
        key = self.limiter.key("login", ["192.0.2.1"])
        for count in (2, 3, 4):
            with self.subTest(count=count):
                await self.limiter.redis.set(key, count)  # Deliberately no expiry.
                self.assertEqual(await self.limiter.redis.pttl(key), -1)
                if count < self.settings.login_limit:
                    await self.limiter.coarse(scope, "login")
                    expected = count + 1
                else:
                    with self.assertRaises(HTTPException) as caught:
                        await self.limiter.coarse(scope, "login")
                    self.assertEqual(caught.exception.status_code, 429)
                    self.assertGreaterEqual(int(caught.exception.headers["Retry-After"]), 1)
                    expected = count
                self.assertEqual(int(await self.limiter.redis.get(key)), expected)
                self.assertGreater(await self.limiter.redis.pttl(key), 0)
                self.assertLessEqual(await self.limiter.redis.pttl(key), 60_000)

    async def test_malformed_redis_responses_fail_closed_without_sql(self):
        for result in (
            None, [], [1], [1, 1000, 0], "sensitive-response",
            {"allowed": 1}, [True, 1000], [2, 1000], ["1", 1000],
            [0, None], [0, True], [0, "sensitive-ttl"], [0, -1],
            [1, float("nan")], [0, 10**1000],
        ):
            with self.subTest(result_type=type(result).__name__):
                with patch.object(self.limiter.redis, "eval", new=AsyncMock(return_value=result)):
                    response = await self.client.post("/auth/login", json={})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": "Authentication temporarily unavailable"})
                self.assertEqual(response.headers["Retry-After"], "1")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.sql_calls, 0)

    async def test_unexpected_client_failures_are_safe_in_both_gates(self):
        original_eval = self.limiter.redis.eval
        for gate in ("coarse", "identity"):
            for error_type in (RuntimeError, ValueError, TypeError, AttributeError):
                with self.subTest(gate=gate, error_type=error_type.__name__):
                    await self.clear_keys()

                    async def broken_eval(*args, **kwargs):
                        if gate == "identity" and ":login-identity:" not in args[2]:
                            return await original_eval(*args, **kwargs)
                        raise error_type("sensitive-client-failure")

                    with patch.object(self.limiter.redis, "eval", side_effect=broken_eval) as evaluate:
                        response = await self.login()
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {"detail": "Authentication temporarily unavailable"})
                    self.assertEqual(response.headers["Retry-After"], "1")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(evaluate.call_count, 1 if gate == "coarse" else 2)
        self.assertEqual(self.sql_calls, 0)

    async def test_request_cancellation_is_not_converted_to_service_unavailable(self):
        with patch.object(self.limiter.redis, "eval", new=AsyncMock(
            side_effect=asyncio.CancelledError,
        )):
            with self.assertRaises(asyncio.CancelledError):
                await self.limiter.coarse({"client": ("192.0.2.1", 1)}, "login")
