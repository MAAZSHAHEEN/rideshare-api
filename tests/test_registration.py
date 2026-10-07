"""Registration integration tests against an explicitly configured PostgreSQL DB.

Set TEST_DATABASE_URL to a postgresql+asyncpg:// URL for a test database, then run:
    python -B -m unittest discover -s tests -v

The database user needs CREATE SCHEMA permission. Each test uses its own schema
inside an outer transaction; endpoint commits use savepoints and teardown rolls
back all test tables, enums, and data. No migrations or app lifespan are run.
"""

import os
import asyncio
import base64
from datetime import datetime, timezone
import json
import secrets
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import bcrypt
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Never let importing the app configure its engine from the project's .env.
# ASGITransport does not run the lifespan, which currently creates app tables.
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "registration-tests-only-not-a-production-key",
}):
    from database import get_db
    from main import app
    from models import User, UserRole
    from routers import auth


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")

        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)
        self.connection = await engine.connect()
        self.addAsyncCleanup(self.connection.close)
        transaction = await self.connection.begin()
        self.addAsyncCleanup(transaction.rollback)

        schema = f"test_registration_{uuid4().hex}"
        await self.connection.execute(CreateSchema(schema))
        await self.connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        await self.connection.run_sync(User.__table__.create)

        async def override_get_db():
            async with AsyncSession(
                bind=self.connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(self.client.aclose)

    def payload(self, role):
        identifier = uuid4()
        return {
            "name": "Registration Test",
            "email": f"registration-{identifier.hex}@example.com",
            "password": "Test-password-123!",
            "cnic": f"{identifier.int % 10**13:013d}",
            "phone_number": "03001234567",
            "role": role,
        }

    async def assert_registration_succeeds(self, role, *, omit_role=False):
        payload = self.payload(role)
        if omit_role:
            payload.pop("role")
        response = await self.client.post("/auth/register", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], role)
        result = await self.connection.execute(
            select(User.__table__).where(User.email == payload["email"])
        )
        user = result.mappings().one()
        self.assertEqual(user["id"], response.json()["id"])
        self.assertEqual(user["role"], UserRole(role))

    async def test_passenger_registration_succeeds(self):
        await self.assert_registration_succeeds("passenger")

    async def test_driver_registration_succeeds(self):
        await self.assert_registration_succeeds("driver")

    async def test_omitted_role_defaults_to_passenger(self):
        await self.assert_registration_succeeds("passenger", omit_role=True)

    async def test_admin_registration_is_rejected_without_creating_a_user(self):
        payload = self.payload("admin")
        response = await self.client.post("/auth/register", json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertTrue(any(
            error["loc"] == ["body", "role"]
            and error["type"] == "literal_error"
            for error in response.json()["detail"]
        ))
        count = await self.connection.scalar(select(func.count()).select_from(User))
        self.assertEqual(count, 0)

    async def test_registration_responses_exclude_sensitive_fields(self):
        for role in ("passenger", "driver"):
            with self.subTest(role=role):
                response = await self.client.post("/auth/register", json=self.payload(role))
                self.assertEqual(response.status_code, 201, response.text)
                body = response.json()
                self.assertEqual(set(body), {"id", "name", "email", "role"})
                for field in ("password", "cnic", "phone_number"):
                    self.assertNotIn(field, body)

    async def register_user(self, password=None):
        payload = self.payload("passenger")
        if password is not None:
            payload["password"] = password
        response = await self.client.post("/auth/register", json=payload)
        self.assertEqual(response.status_code, 201)
        return payload, response.json()

    async def login_user(self, payload):
        return await self.client.post("/auth/login", json={
            "email": payload["email"], "password": payload["password"],
        })

    def sign(self, claims, **kwargs):
        return jwt.encode(claims, kwargs.get("key", auth.SECRET_KEY),
                          algorithm=kwargs.get("algorithm", auth.ALGORITHM))

    async def assert_unauthorized_token(self, token):
        response = await self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(set(response.json()), {"detail"})
        self.assertIsInstance(response.json()["detail"], str)

    async def test_missing_empty_and_wrong_scheme_credentials_return_401(self):
        for authorization in (None, "", "Bearer", "Basic invalid", "Digest invalid"):
            with self.subTest(authorization=authorization):
                headers = {} if authorization is None else {"Authorization": authorization}
                response = await self.client.get("/me", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                self.assertEqual(response.json(), {"detail": "Not authenticated"})

    async def test_duplicate_registration_returns_generic_conflict(self):
        payload, _ = await self.register_user()
        for field in ("email", "cnic"):
            with self.subTest(field=field):
                duplicate = {**self.payload("passenger"), field: payload[field]}
                response = await self.client.post("/auth/register", json=duplicate)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), {"detail": "Registration details already in use"})
                self.assertEqual(await self.connection.scalar(select(func.count()).select_from(User)), 1)

    async def test_database_probe_failure_returns_generic_503(self):
        with patch("main.engine") as engine:
            engine.connect.side_effect = RuntimeError("private diagnostic must not escape")
            response = await self.client.get("/test-db")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Database unavailable"})

    async def token_claims(self):
        _, user = await self.register_user()
        token = auth.create_access_token(user["id"], user["role"])
        return jwt.get_unverified_claims(token)

    async def test_registration_stores_bcrypt_hash_not_plaintext(self):
        payload, _ = await self.register_user()
        stored = await self.connection.scalar(select(User.password).where(User.email == payload["email"]))
        self.assertTrue(stored != payload["password"], "Stored password must be hashed")
        self.assertTrue(stored.startswith("$2b$12$"), "Expected bcrypt with cost 12")
        self.assertTrue(await asyncio.to_thread(bcrypt.checkpw, payload["password"].encode(), stored.encode()))

    async def test_correct_password_and_issued_token_authenticate(self):
        payload, user = await self.register_user()
        response = await self.login_user(payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"access_token", "token_type"})
        token = response.json()["access_token"]
        me = await self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["id"], user["id"])
        self.assertEqual(set(me.json()), {"id", "name", "email", "role"})

    async def test_incorrect_password_and_unknown_email_fail_consistently(self):
        payload, _ = await self.register_user()
        for changes in ({"password": "Incorrect-password!"}, {"email": "unknown@example.com"}):
            with self.subTest(case=next(iter(changes))):
                response = await self.login_user({**payload, **changes})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Invalid email or password"})
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")

    async def test_invalid_password_registration_creates_no_user(self):
        cases = {"empty": "", "short": "short", "blank": " " * 12,
                 "ascii_over_limit": "a" * 73, "utf8_over_limit": "é" * 37}
        for label, password in cases.items():
            with self.subTest(case=label):
                response = await self.client.post("/auth/register", json={
                    **self.payload("passenger"), "password": password,
                })
                self.assertEqual(response.status_code, 422)
                self.assertEqual(await self.connection.scalar(select(func.count()).select_from(User)), 0)

    async def test_password_boundaries_and_spaces_are_preserved(self):
        for label, password in (("minimum", "a" * 12), ("ascii_maximum", "a" * 72),
                                ("utf8_maximum", "é" * 36), ("spaces", "  password with spaces  ")):
            with self.subTest(case=label):
                payload, _ = await self.register_user(password)
                self.assertEqual((await self.login_user(payload)).status_code, 200)
                if label == "spaces":
                    self.assertEqual((await self.login_user({**payload, "password": password.strip()})).status_code, 401)

    async def test_overlong_login_does_not_authenticate_truncated_prefix(self):
        payload, _ = await self.register_user("a" * 72)
        response = await self.login_user({**payload, "password": "a" * 73})
        self.assertEqual(response.status_code, 422)

    async def test_existing_short_bcrypt_password_still_authenticates(self):
        payload, user = await self.register_user()
        # Both prefixes have been used by bcrypt/Passlib installations.
        for prefix in (b"2a", b"2b"):
            stored = await asyncio.to_thread(bcrypt.hashpw, b"legacy", bcrypt.gensalt(rounds=12, prefix=prefix))
            await self.connection.execute(update(User).where(User.id == user["id"]).values(password=stored.decode()))
            response = await self.login_user({**payload, "password": "legacy"})
            self.assertEqual(response.status_code, 200)

    async def assert_offloaded_request(self, helper_name, request):
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        release = threading.Event()
        original = getattr(auth, helper_name)

        def guarded(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise AssertionError("Event loop did not release password worker")
            return original(*args)

        with patch.object(auth, helper_name, guarded):
            task = asyncio.create_task(request())
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                self.assertFalse(task.done())
            finally:
                release.set()
                response = await task
        return response

    async def test_hashing_is_offloaded_and_event_loop_remains_responsive(self):
        payload = self.payload("passenger")
        response = await self.assert_offloaded_request(
            "hash_password", lambda: self.client.post("/auth/register", json=payload),
        )
        self.assertEqual(response.status_code, 201)

    async def test_verification_is_offloaded_and_event_loop_remains_responsive(self):
        payload, _ = await self.register_user()
        response = await self.assert_offloaded_request("verify_password", lambda: self.login_user(payload))
        self.assertEqual(response.status_code, 200)

    async def test_expired_token_is_rejected_without_sleeping(self):
        claims = await self.token_claims()
        await self.assert_unauthorized_token(self.sign({**claims, "exp": 1}))

    async def test_wrong_signature_algorithm_and_unsigned_tokens_are_rejected(self):
        claims = await self.token_claims()
        await self.assert_unauthorized_token(self.sign(claims, key=secrets.token_hex(64)))
        wrong_algorithm = "HS512" if auth.ALGORITHM != "HS512" else "HS256"
        await self.assert_unauthorized_token(self.sign(claims, algorithm=wrong_algorithm))
        token = self.sign(claims)
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
        await self.assert_unauthorized_token(header + "." + token.split(".")[1] + ".")

    async def test_missing_required_claims_are_rejected(self):
        claims = await self.token_claims()
        for field in ("sub", "role", "exp"):
            with self.subTest(field=field):
                await self.assert_unauthorized_token(self.sign({key: value for key, value in claims.items() if key != field}))

    async def test_invalid_and_nonexistent_subjects_are_rejected(self):
        claims = await self.token_claims()
        subjects = [None, 1, True, [], {}, "", "abc", "0", "-1", "+1", "01", "1.0",
                    "9" * 5000, str(2**31), str(2**31 - 1)]
        for index, subject in enumerate(subjects):
            with self.subTest(case=index):
                await self.assert_unauthorized_token(self.sign({**claims, "sub": subject}))

    async def test_invalid_roles_and_role_mismatch_are_rejected(self):
        claims = await self.token_claims()
        for index, role in enumerate(["superadmin", "", None, 1, [], {}, "driver", "admin"]):
            with self.subTest(case=index):
                await self.assert_unauthorized_token(self.sign({**claims, "role": role}))

    async def test_invalid_numeric_dates_are_rejected(self):
        claims = await self.token_claims()
        for field in ("exp", "iat", "nbf"):
            for index, value in enumerate([None, True, [], {}, "123", 1.5, float("inf")]):
                with self.subTest(field=field, case=index):
                    await self.assert_unauthorized_token(self.sign({**claims, field: value}))
        tomorrow = int(datetime.now(timezone.utc).timestamp()) + 86400
        for field in ("iat", "nbf"):
            await self.assert_unauthorized_token(self.sign({**claims, field: tomorrow}))

    async def test_malformed_tokens_are_rejected(self):
        for index, token in enumerate(("", "not-a-token", "a.b.c", "....")):
            with self.subTest(case=index):
                await self.assert_unauthorized_token(token)

    async def test_passenger_cannot_escalate_by_tampering_with_token(self):
        claims = await self.token_claims()
        token = self.sign(claims)
        parts = token.split(".")
        forged = {**claims, "role": "admin"}
        parts[1] = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
        await self.assert_unauthorized_token(".".join(parts))
        await self.assert_unauthorized_token(self.sign(forged, key=secrets.token_hex(64)))

    async def test_existing_admin_login_and_legacy_token_remain_valid(self):
        payload, user = await self.register_user()
        # Controlled test fixture provisioning; never public admin registration.
        await self.connection.execute(update(User).where(User.id == user["id"]).values(role=UserRole.admin))
        response = await self.login_user(payload)
        self.assertEqual(response.status_code, 200)
        token = response.json()["access_token"]
        claims = jwt.get_unverified_claims(token)
        self.assertEqual(claims["role"], "admin")
        for candidate in (token, self.sign({key: value for key, value in claims.items() if key != "iat"})):
            me = await self.client.get("/me", headers={"Authorization": f"Bearer {candidate}"})
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["role"], "admin")


if __name__ == "__main__":
    unittest.main()
