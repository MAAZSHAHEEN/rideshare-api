"""Departure-time guards for booking and acceptance (AST-001).

Runs on real PostgreSQL in an isolated schema; set TEST_DATABASE_URL.
Fixture identities are synthetic: no real CNIC or personal data.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "departure-tests-only-not-a-production-key",
}):
    import clock
    import database
    from main import app
    from models import Base, Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token

DEPARTED = {"detail": "Ride has already departed"}


class HasDepartedTests(unittest.TestCase):
    def test_boundary_is_inclusive(self):
        departure = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        self.assertTrue(clock.has_departed(departure, now=departure))
        self.assertTrue(clock.has_departed(departure, now=departure + timedelta(microseconds=1)))
        self.assertFalse(clock.has_departed(departure, now=departure - timedelta(microseconds=1)))

    def test_compares_instants_across_timezones(self):
        pkt = timezone(timedelta(hours=5))
        departure = datetime(2026, 11, 1, 13, 0, tzinfo=pkt)  # 08:00 UTC
        self.assertTrue(clock.has_departed(departure, now=datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)))
        self.assertFalse(clock.has_departed(departure, now=datetime(2026, 11, 1, 7, 59, tzinfo=timezone.utc)))

    def test_utc_now_is_aware_utc(self):
        self.assertEqual(clock.utc_now().utcoffset(), timedelta(0))


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class DepartureRuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_departure_" + uuid4().hex
        self.engine = create_async_engine(TEST_DATABASE_URL, connect_args={
            "server_settings": {"search_path": self.schema, "lock_timeout": "5000",
                                "statement_timeout": "15000"},
        })
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            users = [User(name=f"fixture-{i}", email=f"departure{i}@example.com",
                          cnic=f"fixture-cnic-{i}", password="unused",
                          phone_number="000", role=role)
                     for i, role in enumerate((UserRole.driver, UserRole.passenger,
                                               UserRole.passenger))]
            db.add_all(users)
            await db.commit()
            self.driver, self.passenger, self.other_passenger = users
        binding = patch.dict(database.SessionLocal.kw, {"bind": self.engine})
        binding.start()
        self.addCleanup(binding.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def headers(self, user):
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    async def seed_ride(self, departure_time, seats=2, with_pending=False):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.driver.id, origin="Lahore", destination="Islamabad",
                        departure_time=departure_time, available_seats=seats, fare_per_seat=1500)
            db.add(ride)
            await db.flush()
            booking_id = None
            if with_pending:
                booking = Booking(ride_id=ride.id, passenger_id=self.passenger.id)
                db.add(booking)
                await db.flush()
                booking_id = booking.id
            await db.commit()
            return ride.id, booking_id

    async def state(self, ride_id):
        async with AsyncSession(self.engine) as db:
            ride = await db.get(Ride, ride_id)
            statuses = (await db.scalars(
                select(Booking.status).where(Booking.ride_id == ride_id).order_by(Booking.id))).all()
            return ride.status, ride.available_seats, list(statuses)

    async def book(self, ride_id, user=None):
        user = user or self.other_passenger
        return await self.client.post(f"/bookings/{ride_id}", headers=self.headers(user))

    async def respond(self, booking_id, accept):
        return await self.client.patch(f"/bookings/{booking_id}/respond",
                                       params={"accept": str(accept).lower()},
                                       headers=self.headers(self.driver))

    async def test_booking_a_departed_ride_is_rejected_without_writes(self):
        ride_id, _ = await self.seed_ride(datetime.now(timezone.utc) - timedelta(hours=1))
        response = await self.book(ride_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), DEPARTED)
        self.assertEqual(await self.state(ride_id), (RideStatus.active, 2, []))

    async def test_booking_a_future_ride_still_succeeds(self):
        ride_id, _ = await self.seed_ride(datetime.now(timezone.utc) + timedelta(hours=1))
        response = await self.book(ride_id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(await self.state(ride_id), (RideStatus.active, 2, [BookingStatus.pending]))

    async def test_accepting_after_departure_is_rejected_without_writes(self):
        ride_id, booking_id = await self.seed_ride(
            datetime.now(timezone.utc) - timedelta(minutes=5), with_pending=True)
        response = await self.respond(booking_id, accept=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), DEPARTED)
        self.assertEqual(await self.state(ride_id), (RideStatus.active, 2, [BookingStatus.pending]))

    async def test_rejecting_after_departure_remains_allowed(self):
        ride_id, booking_id = await self.seed_ride(
            datetime.now(timezone.utc) - timedelta(minutes=5), with_pending=True)
        response = await self.respond(booking_id, accept=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(await self.state(ride_id), (RideStatus.active, 2, [BookingStatus.rejected]))

    async def test_exact_departure_instant_counts_as_departed(self):
        departure = datetime.now(timezone.utc) + timedelta(hours=2)
        ride_id, booking_id = await self.seed_ride(departure, with_pending=True)
        with patch("clock.utc_now", return_value=departure):
            book = await self.book(ride_id)
            accept = await self.respond(booking_id, accept=True)
        self.assertEqual((book.status_code, accept.status_code), (409, 409))
        with patch("clock.utc_now", return_value=departure - timedelta(microseconds=1)):
            book = await self.book(ride_id)
            accept = await self.respond(booking_id, accept=True)
        self.assertEqual((book.status_code, accept.status_code), (201, 200))
        self.assertEqual(await self.state(ride_id),
                         (RideStatus.active, 1, [BookingStatus.accepted, BookingStatus.pending]))

    async def assert_guard_runs_after_lock(self, operation):
        """A request that waits on the ride lock is judged at the time it proceeds."""
        departure = datetime.now(timezone.utc) + timedelta(hours=2)
        ride_id, booking_id = await self.seed_ride(departure, with_pending=True)
        current = {"now": departure - timedelta(minutes=1)}
        with patch("clock.utc_now", side_effect=lambda: current["now"]):
            async with self.engine.connect() as holder:
                transaction = await holder.begin()
                await holder.execute(select(Ride.id).where(Ride.id == ride_id).with_for_update())
                task = asyncio.create_task(
                    self.book(ride_id) if operation == "book" else self.respond(booking_id, True))
                # Wait until the request is blocked on the row lock, then let
                # departure pass before releasing it.
                for _ in range(200):
                    waiting = await self.lock_waiters()
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(waiting, "request never waited on the ride lock")
                current["now"] = departure
                await transaction.rollback()
            response = await asyncio.wait_for(task, timeout=10)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), DEPARTED)
        self.assertEqual(await self.state(ride_id), (RideStatus.active, 2, [BookingStatus.pending]))

    async def lock_waiters(self):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(
                "SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = current_database() LIMIT 1"))

    async def test_booking_waiting_on_lock_rechecks_departure(self):
        await self.assert_guard_runs_after_lock("book")

    async def test_acceptance_waiting_on_lock_rechecks_departure(self):
        await self.assert_guard_runs_after_lock("respond")

    async def test_departed_ride_cannot_be_booked_even_with_seats_and_active_status(self):
        # Guard ordering: departure is reported before seat availability.
        ride_id, _ = await self.seed_ride(datetime.now(timezone.utc) - timedelta(days=1), seats=0)
        response = await self.book(ride_id)
        self.assertEqual(response.json(), DEPARTED)


if __name__ == "__main__":
    unittest.main()
