"""Multi-seat bookings (Milestone C, c1a7e0b00003).

Unit tests pin the seat arithmetic, schema bounds and settings. PostgreSQL
tests (set TEST_DATABASE_URL) run the HTTP API in an isolated schema, including
asyncio.gather races against real row locks that check the seat invariant:
0 <= available_seats <= capacity and sum(accepted seats) + available = capacity.
Fixture identities are synthetic: no real CNIC, phone or personal data.
"""
import asyncio
import os
import random
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "multiseat-tests-only-not-a-production-key",
}):
    import booking_expiry
    import database
    import idempotency
    import lifecycle
    from main import app
    from models import Base, Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token
    from schemas import BookingCreate

B, R = BookingStatus, RideStatus
NO_SEATS = "No seats available"
NOT_ENOUGH = "Not enough seats available"


class SeatArithmeticTests(unittest.TestCase):
    def ride(self, available):
        return SimpleNamespace(available_seats=available, status=R.active)

    def test_accept_debits_and_cancel_restores_exactly_seats_requested(self):
        ride = self.ride(5)
        booking = SimpleNamespace(status=B.pending, seats_requested=3)
        lifecycle.apply_booking_transition(ride, booking, "accept")
        self.assertEqual((booking.status, ride.available_seats), (B.accepted, 2))
        lifecycle.apply_booking_transition(ride, booking, "cancel")
        self.assertEqual((booking.status, ride.available_seats), (B.cancelled, 5))

    def test_non_debiting_transitions_never_move_seats(self):
        for action in ("reject", "cancel", "expire"):
            ride = self.ride(1)
            booking = SimpleNamespace(status=B.pending, seats_requested=8)
            lifecycle.apply_booking_transition(ride, booking, action)
            self.assertEqual(ride.available_seats, 1, action)

    def test_insufficient_seats_is_409_without_mutation(self):
        for available, detail in ((2, NOT_ENOUGH), (0, NO_SEATS)):
            ride = self.ride(available)
            booking = SimpleNamespace(status=B.pending, seats_requested=3)
            with self.assertRaises(HTTPException) as raised:
                lifecycle.apply_booking_transition(ride, booking, "accept")
            self.assertEqual((raised.exception.status_code, raised.exception.detail), (409, detail))
            self.assertEqual((booking.status, ride.available_seats), (B.pending, available))
        # Exactly enough is allowed (boundary).
        ride = self.ride(3)
        lifecycle.apply_booking_transition(ride, SimpleNamespace(status=B.pending, seats_requested=3), "accept")
        self.assertEqual(ride.available_seats, 0)

    def test_legacy_single_seat_messages_unchanged(self):
        ride = self.ride(0)
        with self.assertRaises(HTTPException) as raised:
            lifecycle.apply_booking_transition(ride, SimpleNamespace(status=B.pending), "accept")
        self.assertEqual(raised.exception.detail, NO_SEATS)

    def test_seat_count_defaults_unflushed_to_one_and_rejects_out_of_range(self):
        self.assertEqual(lifecycle.seat_count(SimpleNamespace()), 1)
        self.assertEqual(lifecycle.seat_count(SimpleNamespace(seats_requested=None)), 1)
        self.assertEqual(lifecycle.seat_count(SimpleNamespace(seats_requested=8)), 8)
        for bad in (0, 9, -1, True, 2.0, "2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lifecycle.seat_count(SimpleNamespace(seats_requested=bad))

    def test_schema_bounds_strict_and_no_extras(self):
        self.assertEqual(BookingCreate().seats, 1)
        for good in (1, 8):
            self.assertEqual(BookingCreate(seats=good).seats, good)
        for bad in (0, 9, -1, "2", 2.0, True, None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                BookingCreate.model_validate({"seats": bad})
        with self.assertRaises(ValidationError):
            BookingCreate.model_validate({"seats": 2, "extra": 1})

    def test_max_seats_setting_is_validated(self):
        self.assertEqual(lifecycle.MAX_SEATS, 8)
        with patch.dict(os.environ, {"BOOKING_MAX_SEATS": "1"}):
            self.assertEqual(lifecycle.max_seats_from_environment(), 1)
        for bad in ("0", "9", "two", ""):
            with self.subTest(bad=bad), patch.dict(os.environ, {"BOOKING_MAX_SEATS": bad}):
                with self.assertRaises(RuntimeError):
                    lifecycle.max_seats_from_environment()

    def test_fingerprint_for_one_seat_is_the_pre_multiseat_fingerprint(self):
        # Keys stored before c1a7e0b00003 hashed {"ride_id"} only; a 1-seat
        # retry must still match, and any other seat count must not.
        self.assertEqual(idempotency.fingerprint("booking.create", {"ride_id": 7}),
                         idempotency.fingerprint("booking.create", {"ride_id": 7}))
        self.assertNotEqual(idempotency.fingerprint("booking.create", {"ride_id": 7}),
                            idempotency.fingerprint("booking.create", {"ride_id": 7, "seats": 2}))

    def test_no_seat_arithmetic_outside_lifecycle(self):
        """Every seat change must go through lifecycle.apply_booking_transition,
        so restorations always use seats_requested. Catches ``+= 1`` style code
        (e.g. a suspension cascade) added on another branch at integration."""
        # ``x.available_seats += n`` / ``-=`` / ``x.available_seats = x.available_seats ...``
        pattern = re.compile(r"\.available_seats\s*[-+]=|\.available_seats\s*=[^=].*available_seats")
        offenders = []
        for path in PROJECT_ROOT.rglob("*.py"):
            relative = path.relative_to(PROJECT_ROOT)
            if relative.parts[0] in ("tests", "alembic", ".venv", "frontend", "node_modules") \
                    or relative.as_posix() == "lifecycle.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line.split("#")[0]):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class MultiSeatApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_multiseat_" + uuid4().hex
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=20, max_overflow=10,
                                          connect_args={"server_settings": {
                                              "search_path": self.schema, "lock_timeout": "10000",
                                              "statement_timeout": "20000"}})
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            roles = [UserRole.driver] + [UserRole.passenger] * 8
            users = [User(name=f"fixture-{i}", email=f"multiseat{i}@example.com",
                          cnic=f"fixture-cnic-{i}", password="unused",
                          phone_number="000", role=role) for i, role in enumerate(roles)]
            db.add_all(users)
            await db.commit()
            self.driver, self.passengers = users[0], users[1:]
            self.passenger = self.passengers[0]
        binding = patch.dict(database.SessionLocal.kw, {"bind": self.engine})
        binding.start()
        self.addCleanup(binding.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def headers(self, user, key=None):
        headers = {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    async def seed_ride(self, seats, bookings=(), departure=None):
        """``bookings``: (passenger, seats_requested, status) tuples."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.driver.id, origin="Lahore", destination="Islamabad",
                        departure_time=departure or datetime.now(timezone.utc) + timedelta(days=1),
                        available_seats=seats, fare_per_seat=1500)
            db.add(ride)
            await db.flush()
            rows = [Booking(ride_id=ride.id, passenger_id=user.id, seats_requested=n, status=status)
                    for user, n, status in bookings]
            db.add_all(rows)
            await db.commit()
            return ride.id, [row.id for row in rows]

    async def book(self, ride_id, body=..., user=None, key=None, **kwargs):
        if body is not ...:
            kwargs["json"] = body
        return await self.client.post(f"/bookings/{ride_id}",
                                      headers=self.headers(user or self.passenger, key), **kwargs)

    async def respond(self, booking_id, accept=True, key=None):
        return await self.client.patch(f"/bookings/{booking_id}/respond",
                                       params={"accept": str(accept).lower()},
                                       headers=self.headers(self.driver, key))

    async def withdraw(self, booking_id, user):
        return await self.client.patch(f"/bookings/{booking_id}/cancel", headers=self.headers(user))

    async def state(self, ride_id):
        async with AsyncSession(self.engine) as db:
            ride = await db.scalar(select(Ride).where(Ride.id == ride_id))
            rows = (await db.execute(select(Booking.status, Booking.seats_requested)
                                     .where(Booking.ride_id == ride_id).order_by(Booking.id))).all()
            return ride.status, ride.available_seats, [tuple(row) for row in rows]

    async def assert_invariant(self, ride_id, capacity):
        status, available, rows = await self.state(ride_id)
        accepted = sum(n for s, n in rows if s == B.accepted)
        self.assertGreaterEqual(available, 0)
        self.assertLessEqual(available, capacity)
        self.assertEqual(accepted + available, capacity, rows)
        return status, available, rows

    async def booking_count(self):
        async with AsyncSession(self.engine) as db:
            return await db.scalar(select(func.count()).select_from(Booking))

    # --- Create ---------------------------------------------------------------

    async def test_create_without_body_is_one_seat_and_response_is_additive(self):
        ride_id, _ = await self.seed_ride(3)
        for body, user in ((..., self.passengers[0]), ({}, self.passengers[1]),
                           (None, self.passengers[2]), ({"seats": 1}, self.passengers[3])):
            response = await self.book(ride_id, body, user=user)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(set(response.json()),
                             {"id", "ride_id", "passenger_id", "status", "seats_requested"})
            self.assertEqual(response.json()["seats_requested"], 1)
        self.assertEqual((await self.state(ride_id))[1], 3)  # requests never debit

    async def test_create_multi_seat_bounded_by_available(self):
        ride_id, _ = await self.seed_ride(3)
        response = await self.book(ride_id, {"seats": 3})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual((response.json()["status"], response.json()["seats_requested"]), ("pending", 3))
        response = await self.book(ride_id, {"seats": 4}, user=self.passengers[1])
        self.assertEqual((response.status_code, response.json()), (409, {"detail": NOT_ENOUGH}))
        full_id, _ = await self.seed_ride(0)
        response = await self.book(full_id, {"seats": 2})
        self.assertEqual((response.status_code, response.json()), (409, {"detail": NO_SEATS}))
        self.assertEqual(await self.booking_count(), 1)

    async def test_invalid_seats_is_422_without_echo_or_write(self):
        ride_id, _ = await self.seed_ride(8)
        sentinel = "seat-sentinel-77"
        for body in ({"seats": 0}, {"seats": 9}, {"seats": -3}, {"seats": sentinel},
                     {"seats": "2"}, {"seats": 2.5}, {"seats": 2.0}, {"seats": True},
                     {"seats": None}, {"seats": [2]}, {"seats": 2, "note": sentinel}, [sentinel]):
            with self.subTest(body=body):
                response = await self.book(ride_id, body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn(sentinel, response.text)
                self.assertNotIn('"input"', response.text)
        response = await self.client.post(
            f"/bookings/{ride_id}", content=b'{"seats": "' + sentinel.encode(),
            headers={**self.headers(self.passenger), "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(sentinel, response.text)
        self.assertEqual(await self.booking_count(), 0)

    async def test_unauthenticated_invalid_body_is_401_not_422(self):
        ride_id, _ = await self.seed_ride(2)
        response = await self.client.post(f"/bookings/{ride_id}", json={"seats": 99})
        self.assertEqual(response.status_code, 401)

    async def test_runtime_max_seats_lowers_the_bound(self):
        ride_id, _ = await self.seed_ride(5)
        with patch.object(lifecycle, "MAX_SEATS", 1):
            response = await self.book(ride_id, {"seats": 2})
            self.assertEqual((response.status_code, response.json()),
                             (422, {"detail": "At most 1 seats per booking"}))
            response = await self.book(ride_id, {"seats": 1})
            self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(await self.booking_count(), 1)

    # --- Accept and restorations ----------------------------------------------

    async def test_accept_debits_seats_requested_and_rechecks_under_lock(self):
        a, b, c = self.passengers[:3]
        ride_id, (b1, b2, b3) = await self.seed_ride(5, [(a, 3, B.pending), (b, 2, B.pending),
                                                         (c, 1, B.pending)])
        response = await self.respond(b1)
        self.assertEqual((response.status_code, response.json()["seats_requested"]), (200, 3))
        self.assertEqual((await self.state(ride_id))[1], 2)
        # Request was valid when made; now only 2 left for a 2-seat request: ok.
        self.assertEqual((await self.respond(b2)).status_code, 200)
        response = await self.respond(b3)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": NO_SEATS}))
        await self.assert_invariant(ride_id, 5)

    async def test_accept_more_than_remaining_is_not_enough_seats(self):
        a, b = self.passengers[:2]
        ride_id, (b1, b2) = await self.seed_ride(4, [(a, 2, B.pending), (b, 3, B.pending)])
        self.assertEqual((await self.respond(b1)).status_code, 200)
        response = await self.respond(b2)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": NOT_ENOUGH}))
        self.assertEqual(await self.state(ride_id), (R.active, 2, [(B.accepted, 2), (B.pending, 3)]))

    async def test_passenger_cancel_restores_exact_count_and_pending_withdraw_none(self):
        a, b = self.passengers[:2]
        ride_id, (b1, b2) = await self.seed_ride(2, [(a, 3, B.accepted), (b, 2, B.pending)])
        self.assertEqual((await self.withdraw(b2, b)).status_code, 200)
        self.assertEqual((await self.state(ride_id))[1], 2)
        response = await self.withdraw(b1, a)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "cancelled"))
        self.assertEqual((await self.state(ride_id))[1], 5)

    async def test_ride_cancel_restores_sum_of_accepted_seats(self):
        a, b, c = self.passengers[:3]
        ride_id, _ = await self.seed_ride(1, [(a, 3, B.accepted), (b, 2, B.accepted),
                                              (c, 4, B.pending)])
        response = await self.client.patch(f"/rides/{ride_id}/cancel", headers=self.headers(self.driver))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.state(ride_id),
                         (R.cancelled, 6, [(B.cancelled, 3), (B.cancelled, 2), (B.cancelled, 4)]))

    async def test_ride_complete_keeps_accepted_seats(self):
        a, b = self.passengers[:2]
        ride_id, _ = await self.seed_ride(
            1, [(a, 3, B.accepted), (b, 2, B.pending)],
            departure=datetime.now(timezone.utc) - timedelta(minutes=5))
        response = await self.client.patch(f"/rides/{ride_id}/complete", headers=self.headers(self.driver))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.state(ride_id),
                         (R.completed, 1, [(B.accepted, 3), (B.cancelled, 2)]))

    async def test_expiry_and_stale_closure_never_move_seats(self):
        a, b, c = self.passengers[:3]
        departed = datetime.now(timezone.utc) - timedelta(days=3)
        stale_id, _ = await self.seed_ride(2, [(a, 3, B.accepted), (b, 2, B.pending)], departure=departed)
        live_id, (due,) = await self.seed_ride(4, [(c, 4, B.pending)])
        async with AsyncSession(self.engine) as db:
            await db.execute(Booking.__table__.update().where(Booking.id == due)
                             .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
            await db.commit()
        response = await self.respond(due)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": "Booking request has expired"}))
        async with AsyncSession(self.engine) as db:
            result = await booking_expiry.sweep(db)
        self.assertEqual((result.expired_requests, result.rides_completed), (2, 1))
        self.assertEqual(await self.state(stale_id), (R.completed, 2, [(B.accepted, 3), (B.expired, 2)]))
        self.assertEqual(await self.state(live_id), (R.active, 4, [(B.expired, 4)]))

    async def test_listings_show_seats_requested(self):
        a = self.passenger
        ride_id, (b1,) = await self.seed_ride(5, [(a, 3, B.accepted)])
        mine = await self.client.get("/bookings/me", headers=self.headers(a))
        self.assertEqual([(i["id"], i["seats_requested"]) for i in mine.json()], [(b1, 3)])
        driver = await self.client.get(f"/rides/{ride_id}/bookings", headers=self.headers(self.driver))
        self.assertEqual([(i["id"], i["seats_requested"]) for i in driver.json()], [(b1, 3)])
        spec = (await self.client.get("/openapi.json")).json()["components"]["schemas"]
        self.assertIn("seats_requested", spec["BookingResponse"]["required"])
        self.assertEqual(spec["BookingCreate"]["properties"]["seats"]["maximum"], 8)

    # --- Idempotency ----------------------------------------------------------

    async def test_idempotency_fingerprint_includes_seats(self):
        ride_id, _ = await self.seed_ride(5)
        first = await self.book(ride_id, {"seats": 2}, key="k-seats")
        again = await self.book(ride_id, {"seats": 2}, key="k-seats")
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(again.json(), first.json())
        other = await self.book(ride_id, {"seats": 3}, key="k-seats")
        self.assertEqual((other.status_code, other.json()["detail"]), (422, idempotency.KEY_REUSED))
        omitted = await self.book(ride_id, key="k-seats")
        self.assertEqual(omitted.status_code, 422)
        self.assertEqual(await self.booking_count(), 1)

    async def test_idempotency_omitted_and_explicit_one_seat_are_the_same_request(self):
        ride_id, _ = await self.seed_ride(5)
        first = await self.book(ride_id, key="k-one")
        again = await self.book(ride_id, {"seats": 1}, key="k-one")
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")

    async def test_key_stored_before_multiseat_still_replays_its_one_seat_retry(self):
        # A key claimed by pre-c1a7e0b00003 code hashed {"ride_id"} only.
        from models import IdempotencyKey
        ride_id, (b1,) = await self.seed_ride(5, [(self.passenger, 1, B.pending)])
        stored = {"id": b1, "ride_id": ride_id, "passenger_id": self.passenger.id, "status": "pending"}
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as db:
            db.add(IdempotencyKey(
                user_id=self.passenger.id, key="pre-deploy", operation="booking.create",
                request_hash=idempotency.fingerprint("booking.create", {"ride_id": ride_id}),
                status_code=201, response_body=stored, created_at=now,
                expires_at=now + timedelta(hours=1)))
            await db.commit()
        replay = await self.book(ride_id, key="pre-deploy")
        self.assertEqual((replay.status_code, replay.json()), (201, stored))
        self.assertEqual(replay.headers.get("Idempotent-Replayed"), "true")
        other = await self.book(ride_id, {"seats": 2}, key="pre-deploy")
        self.assertEqual(other.status_code, 422)
        self.assertEqual(await self.booking_count(), 1)

    async def test_concurrent_same_key_accepts_debit_seats_once(self):
        ride_id, (b1,) = await self.seed_ride(5, [(self.passenger, 3, B.pending)])
        responses = await asyncio.gather(*(self.respond(b1, key="acc-1") for _ in range(6)))
        self.assertEqual([r.status_code for r in responses], [200] * 6)
        self.assertEqual(await self.state(ride_id), (R.active, 2, [(B.accepted, 3)]))

    # --- Concurrency ----------------------------------------------------------

    async def test_eight_concurrent_two_seat_accepts_against_five_seats(self):
        ride_id, ids = await self.seed_ride(5, [(p, 2, B.pending) for p in self.passengers])
        responses = await asyncio.gather(*(self.respond(i) for i in ids))
        codes = sorted(r.status_code for r in responses)
        self.assertEqual(codes, [200, 200] + [409] * 6)
        self.assertEqual({r.json()["detail"] for r in responses if r.status_code == 409}, {NOT_ENOUGH})
        status, available, rows = await self.assert_invariant(ride_id, 5)
        self.assertEqual(available, 1)
        self.assertEqual(sum(1 for s, _ in rows if s == B.accepted), 2)

    async def test_concurrent_creates_with_seats_and_duplicate_guard(self):
        ride_id, _ = await self.seed_ride(8)
        responses = await asyncio.gather(*(self.book(ride_id, {"seats": n}) for n in (1, 2, 3, 4)))
        self.assertEqual(sorted(r.status_code for r in responses), [201, 409, 409, 409])
        self.assertEqual(await self.booking_count(), 1)

    async def test_randomised_mixed_races_preserve_invariant(self):
        rng = random.Random(20261031)
        for round_number in range(6):
            capacity = 8
            sizes = [rng.randint(1, 4) for _ in self.passengers]
            ride_id, ids = await self.seed_ride(capacity, [(p, n, B.pending)
                                                           for p, n in zip(self.passengers, sizes)])
            # Accept a first wave sequentially, then race everything at once.
            for booking_id in ids[:2]:
                await self.respond(booking_id)
            operations = []
            for index, booking_id in enumerate(ids):
                user = self.passengers[index]
                operations.append(self.respond(booking_id, accept=rng.random() < 0.8))
                if rng.random() < 0.5:
                    operations.append(self.withdraw(booking_id, user))
            if round_number % 3 == 2:
                operations.append(self.client.patch(f"/rides/{ride_id}/cancel",
                                                    headers=self.headers(self.driver)))
            rng.shuffle(operations)
            responses = await asyncio.gather(*operations)
            self.assertTrue(all(r.status_code in (200, 409) for r in responses),
                            [(r.status_code, r.text) for r in responses if r.status_code not in (200, 409)])
            status, available, rows = await self.assert_invariant(ride_id, capacity)
            if status == R.cancelled:
                self.assertEqual(available, capacity)

    async def test_concurrent_ride_cancel_vs_multi_seat_accepts(self):
        ride_id, ids = await self.seed_ride(6, [(p, 3, B.pending) for p in self.passengers[:4]])
        operations = [self.respond(i) for i in ids]
        operations.insert(2, self.client.patch(f"/rides/{ride_id}/cancel", headers=self.headers(self.driver)))
        await asyncio.gather(*operations)
        status, available, rows = await self.assert_invariant(ride_id, 6)
        self.assertEqual((status, available), (R.cancelled, 6))
        self.assertFalse([s for s, _ in rows if s in (B.accepted, B.pending)])


if __name__ == "__main__":
    unittest.main()
