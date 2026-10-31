"""Pending-request expiry and stale ride closure (Milestone C, c1a7e0b00002).

Unit tests pin the expiry arithmetic, the inclusive boundary and settings.
PostgreSQL tests (set TEST_DATABASE_URL) run the HTTP API and the sweep in an
isolated schema with a frozen ``clock.utc_now``, including asyncio.gather
races between the sweep and driver acceptances. Fixture identities are
synthetic: no real CNIC, phone or personal data.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "expiry-tests-only-not-a-production-key",
}):
    import booking_expiry
    import clock
    import database
    import lifecycle
    from main import app
    from models import Base, Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token

B, R = BookingStatus, RideStatus
EXPIRED = "Booking request has expired"
T0 = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)  # 12:00 PKT


class ExpiryRuleTests(unittest.TestCase):
    def test_new_request_expires_at_earlier_of_ttl_and_departure(self):
        ttl = timedelta(hours=6)
        self.assertEqual(booking_expiry.pending_expires_at(T0, T0 + timedelta(days=1), ttl),
                         T0 + ttl)
        self.assertEqual(booking_expiry.pending_expires_at(T0, T0 + timedelta(hours=2), ttl),
                         T0 + timedelta(hours=2))
        self.assertEqual(booking_expiry.pending_expires_at(T0, T0 + ttl, ttl), T0 + ttl)
        self.assertEqual(booking_expiry.PENDING_TTL, timedelta(hours=6))
        self.assertEqual(booking_expiry.RIDE_AUTO_CLOSE_AFTER, timedelta(hours=24))

    def test_legacy_null_expiry_means_departure_and_boundary_is_inclusive(self):
        ride = SimpleNamespace(departure_time=T0)
        legacy = SimpleNamespace(status=B.pending, expires_at=None)
        fresh = SimpleNamespace(status=B.pending, expires_at=T0 - timedelta(hours=1))
        self.assertEqual(booking_expiry.effective_expires_at(legacy, ride), T0)
        for booking, expiry in ((legacy, T0), (fresh, T0 - timedelta(hours=1))):
            self.assertFalse(booking_expiry.is_expired(booking, ride, expiry - timedelta(microseconds=1)))
            self.assertTrue(booking_expiry.is_expired(booking, ride, expiry))
            self.assertEqual(booking_expiry.display_status(booking, ride, expiry), B.expired)
        # Instants, not wall clocks: 12:00 PKT is 07:00 UTC.
        self.assertTrue(booking_expiry.is_expired(
            legacy, ride, datetime(2026, 11, 2, 12, 0, tzinfo=timezone(timedelta(hours=5)))))
        # An expiry stored later than departure is capped at departure.
        late = SimpleNamespace(status=B.pending, expires_at=T0 + timedelta(hours=3))
        self.assertTrue(booking_expiry.is_expired(late, ride, T0))

    def test_only_pending_requests_expire(self):
        ride = SimpleNamespace(departure_time=T0)
        for status in (B.accepted, B.rejected, B.cancelled, B.expired):
            booking = SimpleNamespace(status=status, expires_at=T0 - timedelta(days=1))
            self.assertFalse(booking_expiry.is_expired(booking, ride, T0 + timedelta(days=1)))
            self.assertEqual(booking_expiry.display_status(booking, ride, T0 + timedelta(days=1)), status)

    def test_expire_transition_is_pending_only_and_seat_neutral(self):
        ride = SimpleNamespace(available_seats=0, status=R.active, departure_time=T0)
        booking = SimpleNamespace(status=B.pending)
        lifecycle.apply_booking_transition(ride, booking, "expire")
        self.assertEqual((booking.status, ride.available_seats), (B.expired, 0))
        for status in (B.accepted, B.rejected, B.cancelled, B.expired):
            with self.assertRaises(HTTPException) as raised:
                lifecycle.apply_booking_transition(ride, SimpleNamespace(status=status), "expire")
            self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse([key for key in lifecycle.BOOKING_TRANSITIONS if key[0] == B.expired])

    def test_settings_are_validated(self):
        with patch.dict(os.environ, {"BOOKING_PENDING_TTL_MINUTES": "30", "RIDE_AUTO_CLOSE_AFTER_HOURS": "2"}):
            self.assertEqual(booking_expiry.pending_ttl_from_environment(), timedelta(minutes=30))
            self.assertEqual(booking_expiry.auto_close_from_environment(), timedelta(hours=2))
        for name, bad in (("BOOKING_PENDING_TTL_MINUTES", "4"), ("BOOKING_PENDING_TTL_MINUTES", "10081"),
                          ("BOOKING_PENDING_TTL_MINUTES", "6h"), ("RIDE_AUTO_CLOSE_AFTER_HOURS", "0"),
                          ("RIDE_AUTO_CLOSE_AFTER_HOURS", "721")):
            with self.subTest(name=name, bad=bad), patch.dict(os.environ, {name: bad}):
                with self.assertRaises(RuntimeError):
                    (booking_expiry.pending_ttl_from_environment()
                     if name.startswith("BOOKING") else booking_expiry.auto_close_from_environment())


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class ExpiryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_expiry_" + uuid4().hex
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=15, max_overflow=10,
                                          connect_args={"server_settings": {
                                              "search_path": self.schema, "lock_timeout": "10000",
                                              "statement_timeout": "20000"}})
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            roles = [UserRole.driver] + [UserRole.passenger] * 6
            users = [User(name=f"fixture-{i}", email=f"expiry{i}@example.com",
                          cnic=f"fixture-cnic-{i}", password="unused",
                          phone_number="000", role=role) for i, role in enumerate(roles)]
            db.add_all(users)
            await db.commit()
            self.driver, self.passengers = users[0], users[1:]
            self.passenger = self.passengers[0]
        binding = patch.dict(database.SessionLocal.kw, {"bind": self.engine})
        binding.start()
        self.addCleanup(binding.stop)
        self.now = T0
        frozen = patch.object(clock, "utc_now", lambda: self.now)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def headers(self, user):
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    async def seed(self, departure, seats=2, bookings=()):
        """bookings: (passenger, status, expires_at) tuples."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.driver.id, origin="Lahore", destination="Islamabad",
                        departure_time=departure, available_seats=seats, fare_per_seat=1500)
            db.add(ride)
            await db.flush()
            rows = [Booking(ride_id=ride.id, passenger_id=user.id, status=status, expires_at=expires)
                    for user, status, expires in bookings]
            db.add_all(rows)
            await db.commit()
            return ride.id, [row.id for row in rows]

    async def book(self, ride_id, user=None):
        return await self.client.post(f"/bookings/{ride_id}", headers=self.headers(user or self.passenger))

    async def respond(self, booking_id, accept):
        return await self.client.patch(f"/bookings/{booking_id}/respond",
                                       params={"accept": str(accept).lower()},
                                       headers=self.headers(self.driver))

    async def rows(self, ride_id):
        async with AsyncSession(self.engine) as db:
            ride = await db.get(Ride, ride_id)
            bookings = (await db.execute(select(Booking.status, Booking.expires_at).where(
                Booking.ride_id == ride_id).order_by(Booking.id))).all()
            return ride.status, ride.available_seats, [tuple(row) for row in bookings]

    async def sweep(self, **kwargs):
        async with AsyncSession(self.engine) as db:
            return await booking_expiry.sweep(db, self.now, **kwargs)

    # --- Request path -----------------------------------------------------------

    async def test_new_request_stores_expiry_capped_by_departure(self):
        far, _ = await self.seed(T0 + timedelta(days=2))
        near, _ = await self.seed(T0 + timedelta(hours=1))
        for ride_id, expected in ((far, T0 + timedelta(hours=6)), (near, T0 + timedelta(hours=1))):
            response = await self.book(ride_id)
            self.assertEqual(response.status_code, 201, response.text)
            # seats_requested: additive field from c1a7e0b00003 (deliberate contract update).
            self.assertEqual(set(response.json()), {"id", "ride_id", "passenger_id", "status", "seats_requested"})
            self.assertEqual((await self.rows(ride_id))[2], [(B.pending, expected)])

    async def test_accept_refuses_expired_request_at_and_after_expiry(self):
        ride_id, (booking_id,) = await self.seed(T0 + timedelta(days=1), bookings=[
            (self.passenger, B.pending, T0 + timedelta(hours=1))])
        for offset in (timedelta(hours=1), timedelta(hours=1, microseconds=1)):
            self.now = T0 + offset
            response = await self.respond(booking_id, True)
            self.assertEqual((response.status_code, response.json()), (409, {"detail": EXPIRED}))
            self.assertEqual(await self.rows(ride_id),
                             (R.active, 2, [(B.pending, T0 + timedelta(hours=1))]))
        self.now = T0 + timedelta(hours=1) - timedelta(microseconds=1)
        response = await self.respond(booking_id, True)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "accepted"))
        self.assertEqual((await self.rows(ride_id))[1], 1)

    async def test_departure_guard_keeps_its_message_for_legacy_requests(self):
        ride_id, (booking_id,) = await self.seed(T0, bookings=[(self.passenger, B.pending, None)])
        self.now = T0
        response = await self.respond(booking_id, True)
        self.assertEqual(response.json(), {"detail": "Ride has already departed"})

    async def test_legacy_request_without_expiry_is_still_acceptable_before_departure(self):
        ride_id, (booking_id,) = await self.seed(T0 + timedelta(minutes=1), bookings=[
            (self.passenger, B.pending, None)])
        response = await self.respond(booking_id, True)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "accepted"))

    async def test_expired_request_can_still_be_rejected_or_withdrawn(self):
        ride_id, (first, second) = await self.seed(T0 + timedelta(days=1), bookings=[
            (self.passengers[0], B.pending, T0), (self.passengers[1], B.pending, T0)])
        rejected = await self.respond(first, False)
        withdrawn = await self.client.patch(f"/bookings/{second}/cancel",
                                            headers=self.headers(self.passengers[1]))
        self.assertEqual((rejected.status_code, rejected.json()["status"]), (200, "rejected"))
        self.assertEqual((withdrawn.status_code, withdrawn.json()["status"]), (200, "cancelled"))
        self.assertEqual((await self.rows(ride_id))[1], 2)

    async def test_persisted_expired_booking_is_terminal(self):
        ride_id, (booking_id,) = await self.seed(T0 + timedelta(days=1), bookings=[
            (self.passenger, B.expired, T0)])
        accept = await self.respond(booking_id, True)
        cancel = await self.client.patch(f"/bookings/{booking_id}/cancel", headers=self.headers(self.passenger))
        self.assertEqual(accept.json(), {"detail": "Booking already responded to"})
        self.assertEqual(cancel.json(), {"detail": "Booking cannot be cancelled"})

    async def test_rerequest_after_own_expired_request_replaces_it(self):
        ride_id, (old_id,) = await self.seed(T0 + timedelta(days=1), bookings=[
            (self.passenger, B.pending, T0 - timedelta(minutes=1))])
        response = await self.book(ride_id)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotEqual(response.json()["id"], old_id)
        self.assertEqual((await self.rows(ride_id))[2], [
            (B.expired, T0 - timedelta(minutes=1)), (B.pending, T0 + timedelta(hours=6))])
        # A live (unexpired) request still blocks a duplicate.
        duplicate = await self.book(ride_id)
        self.assertEqual(duplicate.json(), {"detail": "You already booked this ride"})

    async def test_listings_show_lazily_expired_requests_without_writing(self):
        ride_id, ids = await self.seed(T0 + timedelta(days=1), seats=4, bookings=[
            (self.passengers[0], B.pending, T0),                        # due now
            (self.passengers[1], B.pending, T0 + timedelta(hours=1)),   # live
            (self.passengers[2], B.pending, None),                      # legacy, ride in future
            (self.passengers[3], B.expired, T0 - timedelta(hours=1)),   # swept
        ])
        legacy_ride, (legacy_id,) = await self.seed(T0 - timedelta(minutes=5), bookings=[
            (self.passengers[2], B.pending, None)])                    # legacy, departed
        before = await self.rows(ride_id)
        driver = await self.client.get(f"/rides/{ride_id}/bookings", headers=self.headers(self.driver))
        self.assertEqual(driver.status_code, 200, driver.text)
        self.assertEqual({item["id"]: item["status"] for item in driver.json()}, {
            ids[0]: "expired", ids[1]: "pending", ids[2]: "pending", ids[3]: "expired"})
        for status, expected in (("expired", {ids[0], ids[3]}), ("pending", {ids[1], ids[2]})):
            filtered = await self.client.get(f"/rides/{ride_id}/bookings", params={"status": status},
                                             headers=self.headers(self.driver))
            self.assertEqual({item["id"] for item in filtered.json()}, expected, status)
        mine = await self.client.get("/bookings/me", headers=self.headers(self.passengers[2]))
        self.assertEqual({item["id"]: item["status"] for item in mine.json()},
                         {ids[2]: "pending", legacy_id: "expired"})
        for status, expected in (("expired", [legacy_id]), ("pending", [ids[2]])):
            filtered = await self.client.get("/bookings/me", params={"status": status},
                                             headers=self.headers(self.passengers[2]))
            self.assertEqual([item["id"] for item in filtered.json()], expected, status)
        self.assertEqual(await self.rows(ride_id), before)
        self.assertEqual((await self.rows(legacy_ride))[2], [(B.pending, None)])

    # --- Sweep --------------------------------------------------------------

    async def test_sweep_expires_due_requests_only_and_is_idempotent(self):
        future = T0 + timedelta(days=1)
        ride_a, a = await self.seed(future, bookings=[
            (self.passengers[0], B.pending, T0), (self.passengers[1], B.pending, T0 + timedelta(seconds=1)),
            (self.passengers[2], B.accepted, T0 - timedelta(days=1))])
        ride_b, b = await self.seed(T0 - timedelta(hours=1), bookings=[
            (self.passengers[0], B.pending, None)])           # legacy, departed, within grace
        ride_c, c = await self.seed(future, bookings=[(self.passengers[3], B.pending, None)])
        result = await self.sweep()
        self.assertEqual((result.expired_requests, result.rides_completed, result.rides_cancelled), (2, 0, 0))
        self.assertEqual([s for s, _ in (await self.rows(ride_a))[2]], [B.expired, B.pending, B.accepted])
        self.assertEqual(await self.rows(ride_b), (R.active, 2, [(B.expired, None)]))
        self.assertEqual((await self.rows(ride_c))[2], [(B.pending, None)])
        self.assertEqual((await self.rows(ride_a))[1], 2)
        again = await self.sweep()
        self.assertEqual(again.as_lines(), ["expired_requests=0", "rides_completed=0",
                                            "rides_cancelled=0", "rides_skipped_locked=0"])

    async def test_sweep_closes_stale_rides_after_grace_without_touching_seats(self):
        stale = T0 - timedelta(hours=24)
        with_passenger, _ = await self.seed(stale, seats=1, bookings=[
            (self.passengers[0], B.accepted, None), (self.passengers[1], B.pending, None),
            (self.passengers[2], B.cancelled, None)])
        empty, _ = await self.seed(stale, seats=3, bookings=[(self.passengers[1], B.pending, None)])
        in_grace, _ = await self.seed(stale + timedelta(microseconds=1), seats=2)
        done, _ = await self.seed(stale - timedelta(days=3), seats=2)
        async with self.engine.begin() as conn:
            await conn.execute(update(Ride).where(Ride.id == done).values(status=R.completed))
        result = await self.sweep()
        self.assertEqual((result.rides_completed, result.rides_cancelled, result.expired_requests), (1, 1, 2))
        self.assertEqual(await self.rows(with_passenger),
                         (R.completed, 1, [(B.accepted, None), (B.expired, None), (B.cancelled, None)]))
        self.assertEqual(await self.rows(empty), (R.cancelled, 3, [(B.expired, None)]))
        self.assertEqual(await self.rows(in_grace), (R.active, 2, []))
        self.assertEqual((await self.rows(done))[0], R.completed)
        self.assertEqual((await self.sweep()).as_lines()[:3],
                         ["expired_requests=0", "rides_completed=0", "rides_cancelled=0"])

    async def test_sweep_skips_a_ride_locked_by_a_request_and_catches_up_next_run(self):
        ride_id, _ = await self.seed(T0 + timedelta(days=1), bookings=[(self.passenger, B.pending, T0)])
        stale_id, _ = await self.seed(T0 - timedelta(days=2))
        async with self.engine.connect() as holder:
            await holder.execute(text("SELECT id FROM rides WHERE id IN (:a, :b) FOR UPDATE"),
                                 {"a": ride_id, "b": stale_id})
            result = await asyncio.wait_for(self.sweep(), timeout=10)  # never waits
            self.assertEqual((result.expired_requests, result.rides_skipped_locked), (0, 2))
            await holder.rollback()
        result = await self.sweep()
        self.assertEqual((result.expired_requests, result.rides_cancelled), (1, 1))

    async def test_sweep_racing_acceptances_never_accepts_an_expired_request(self):
        future = T0 + timedelta(days=1)
        ride_id, ids = await self.seed(future, seats=3, bookings=[
            (user, B.pending, T0 if i % 2 else T0 + timedelta(hours=1))
            for i, user in enumerate(self.passengers)])
        live = {booking_id for i, booking_id in enumerate(ids) if not i % 2}
        responses = await asyncio.wait_for(asyncio.gather(
            *(self.respond(booking_id, True) for booking_id in ids), self.sweep(), self.sweep()), timeout=60)
        final = await self.sweep()  # rides a racing sweep skipped (SKIP LOCKED)
        codes = {booking_id: r.status_code for booking_id, r in zip(ids, responses)}
        self.assertEqual({i for i, code in codes.items() if code == 200}, live)
        for booking_id, response in zip(ids, responses):
            if booking_id not in live:
                self.assertIn(response.json()["detail"], (EXPIRED, "Booking already responded to"))
        status, seats, rows = await self.rows(ride_id)
        self.assertEqual(seats, 0)
        self.assertEqual([s for s, _ in rows], [B.accepted if i in live else B.expired for i in ids])
        self.assertEqual(sum(r.expired_requests for r in (*responses[-2:], final)), 3)

    async def test_completion_by_driver_still_works_and_sweep_respects_it(self):
        ride_id, _ = await self.seed(T0 - timedelta(days=2), bookings=[(self.passenger, B.accepted, None)])
        async with self.engine.begin() as conn:
            await conn.execute(update(Ride).where(Ride.id == ride_id).values(available_seats=1))
        completed = await self.client.patch(f"/rides/{ride_id}/complete", headers=self.headers(self.driver))
        self.assertEqual(completed.status_code, 200, completed.text)
        result = await self.sweep()
        self.assertEqual((result.rides_completed, result.rides_cancelled), (0, 0))
        self.assertEqual(await self.rows(ride_id), (R.completed, 1, [(B.accepted, None)]))


if __name__ == "__main__":
    unittest.main()
