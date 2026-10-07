"""Ride API tests against isolated PostgreSQL schemas; set TEST_DATABASE_URL.

The ride clock is fixed independently of JWT validation. No sleeps are needed.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "ride-tests-only-not-a-production-signing-key",
}):
    from database import Base, get_db
    from main import app
    from models import Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token


NOW = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class RideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = f"test_rides_{uuid4().hex}"
        self.engine = create_async_engine(TEST_DATABASE_URL, connect_args={
            "server_settings": {"search_path": self.schema},
        })
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.execute(CreateSchema(self.schema))
            await connection.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            self.users = {}
            for role in UserRole:
                user = User(name=role.value, email=f"{role.value}@example.com",
                            password="unused-test-hash", cnic=role.value,
                            phone_number="00000000000", role=role)
                session.add(user)
                self.users[role] = user
            await session.commit()

        async def override_get_db():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                yield session

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        clock = patch("routers.rides.datetime")
        clock.start().now.return_value = NOW
        self.addCleanup(clock.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True))

    def headers(self, role=UserRole.passenger):
        user = self.users[role]
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    async def seed(self, **values):
        fields = dict(driver_id=self.users[UserRole.driver].id, origin="Peshawar",
                      destination="Islamabad", departure_time=NOW + timedelta(hours=1),
                      available_seats=2, fare_per_seat=500, status=RideStatus.active)
        fields.update(values)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            ride = Ride(**fields)
            session.add(ride)
            await session.commit()
            return ride.id

    async def search(self, **params):
        response = await self.client.get("/rides/", params=params, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json(), list)
        return [item["id"] for item in response.json()]

    async def count_rides(self):
        async with AsyncSession(self.engine) as session:
            return await session.scalar(select(func.count()).select_from(Ride))

    def payload(self, departure=None):
        return dict(origin="Peshawar", destination="Islamabad", available_seats=2,
                    fare_per_seat=500, departure_time=departure or (NOW + timedelta(hours=1)).isoformat())

    async def test_default_custom_limit_offset_and_maximum(self):
        # More than the maximum proves both default and explicit SQL bounds.
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            rides = [Ride(driver_id=self.users[UserRole.driver].id, origin="Peshawar",
                          destination="Islamabad", departure_time=NOW + timedelta(hours=1),
                          available_seats=2, fare_per_seat=500) for _ in range(105)]
            session.add_all(rides)
            await session.commit()
            ids = sorted(ride.id for ride in rides)
        self.assertEqual(await self.search(), ids[:20])
        self.assertEqual(await self.search(limit=3), ids[:3])
        self.assertEqual(await self.search(limit=3, offset=3), ids[3:6])
        self.assertEqual(await self.search(limit=100), ids[:100])
        self.assertEqual(await self.search(offset=105), [])

    async def test_invalid_pagination_and_seat_bounds_return_422(self):
        for params in ({"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": "bad"},
                       {"limit": "1.5"}, {"offset": -1}, {"offset": "bad"},
                       {"offset": 2**63}, {"min_seats": 0}, {"min_seats": -1},
                       {"min_seats": 2**31}, {"min_seats": "bad"},
                       {"origin": ""}, {"destination": ""}):
            with self.subTest(params=params):
                response = await self.client.get("/rides/", params=params, headers=self.headers())
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(set(response.json()), {"detail"})
                self.assertIsInstance(response.json()["detail"], list)

    async def test_only_future_active_rides_with_seats_are_searchable(self):
        await self.seed(departure_time=NOW - timedelta(microseconds=1))
        await self.seed(departure_time=NOW)
        future = await self.seed(departure_time=NOW + timedelta(microseconds=1))
        await self.seed(status=RideStatus.completed)
        await self.seed(status=RideStatus.cancelled)
        await self.seed(available_seats=0)
        self.assertEqual(await self.search(), [future])
        self.assertEqual(await self.search(departure_from=(NOW - timedelta(days=1)).isoformat()), [future])

    async def test_exact_location_filters_individually_and_combined(self):
        first = await self.seed()
        second = await self.seed(destination="Lahore")
        third = await self.seed(origin="Lahore")
        await self.seed(origin="peshawar", destination="islamabad")
        self.assertEqual(await self.search(origin="Peshawar"), [first, second])
        self.assertEqual(await self.search(destination="Islamabad"), [first, third])
        self.assertEqual(await self.search(origin="Peshawar", destination="Islamabad"), [first])
        self.assertEqual(await self.search(origin="Pesh"), [])
        self.assertEqual(await self.search(origin=" Peshawar"), [])
        # Filtering precedes offset and limit, not the other way around.
        self.assertEqual(await self.search(destination="Islamabad", offset=1, limit=1), [third])

    async def test_minimum_seats(self):
        await self.seed(available_seats=1)
        enough = await self.seed(available_seats=3)
        self.assertEqual(await self.search(min_seats=3), [enough])
        self.assertEqual(await self.search(min_seats=4), [])

    async def test_departure_order_and_id_tie_break_across_pages(self):
        late = await self.seed(departure_time=NOW + timedelta(hours=3))
        early = await self.seed()
        tied = await self.seed()
        self.assertLess(late, early)  # Insertion order differs from departure order.
        self.assertEqual(await self.search(), [early, tied, late])
        self.assertEqual(await self.search(offset=1, limit=1), [tied])

    async def test_time_window_inclusive_start_exclusive_end_and_timezones(self):
        first = await self.seed()
        second = await self.seed(departure_time=NOW + timedelta(hours=2))
        await self.seed(departure_time=NOW + timedelta(hours=3))
        self.assertEqual(await self.search(departure_from="2030-01-15T18:00:00+05:00",
                                           departure_before="2030-01-15T10:00:00-05:00"), [first, second])
        self.assertEqual(await self.search(departure_before="2030-01-15T14:00:00Z"), [first])
        self.assertEqual(await self.search(departure_before=NOW.isoformat()), [])
        self.assertEqual(await self.search(departure_from="2030-01-15T14:00:00Z", limit=1), [second])

    async def test_invalid_or_naive_time_filters_return_422(self):
        for params in ({"departure_from": "2030-01-15T13:00:00"},
                       {"departure_before": "2030-01-15T14:00:00"},
                       {"departure_from": "bad"},
                       {"departure_from": "2030-01-15T14:00:00Z", "departure_before": "2030-01-15T14:00:00Z"},
                       {"departure_from": "2030-01-15T15:00:00Z", "departure_before": "2030-01-15T14:00:00Z"}):
            with self.subTest(params=params):
                response = await self.client.get("/rides/", params=params, headers=self.headers())
                self.assertEqual(response.status_code, 422, response.text)

    async def test_creation_rejects_past_present_and_naive_without_inserting(self):
        for departure in ((NOW - timedelta(seconds=1)).isoformat(), NOW.isoformat(),
                          "2030-01-15T13:00:00", "2030-01-15T16:00:00+05:00"):
            with self.subTest(departure=departure):
                response = await self.client.post("/rides/", json=self.payload(departure),
                                                  headers=self.headers(UserRole.driver))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(await self.count_rides(), 0)

    async def test_creation_accepts_offset_and_stores_correct_utc_instant(self):
        response = await self.client.post("/rides/", json=self.payload("2030-01-15T18:00:00+05:00"),
                                          headers=self.headers(UserRole.driver))
        self.assertEqual(response.status_code, 201, response.text)
        expected = NOW + timedelta(hours=1)
        self.assertEqual(datetime.fromisoformat(response.json()["departure_time"]), expected)
        async with AsyncSession(self.engine) as session:
            ride = await session.get(Ride, response.json()["id"])
            self.assertEqual(ride.departure_time, expected)
            self.assertIsNotNone(ride.departure_time.tzinfo)

    async def test_authorization_regression(self):
        for role in UserRole:
            response = await self.client.get("/rides/", headers=self.headers(role))
            self.assertEqual(response.status_code, 200, response.text)
            response = await self.client.post("/rides/", json=self.payload(), headers=self.headers(role))
            self.assertEqual(response.status_code, 201 if role == UserRole.driver else 403, response.text)
        for method in ("get", "post"):
            kwargs = {"json": self.payload()} if method == "post" else {}
            response = await getattr(self.client, method)("/rides/", **kwargs)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
            response = await getattr(self.client, method)("/rides/", headers={"Authorization": "Bearer malformed"}, **kwargs)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(await self.count_rides(), 1)


if __name__ == "__main__":
    unittest.main()
