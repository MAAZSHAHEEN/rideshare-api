"""Registration integration tests against an explicitly configured PostgreSQL DB.

Set TEST_DATABASE_URL to a postgresql+asyncpg:// URL for a test database, then run:
    python -B -m unittest discover -s tests -v

The database user needs CREATE SCHEMA permission. Each test uses its own schema
inside an outer transaction; endpoint commits use savepoints and teardown rolls
back all test tables, enums, and data. No migrations or app lifespan are run.
"""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
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


if __name__ == "__main__":
    unittest.main()
