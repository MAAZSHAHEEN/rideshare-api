"""Idempotency-Key support for booking create and driver responses (AST-022).

Unit tests cover header validation, fingerprints and TTL configuration. The
PostgreSQL tests (set TEST_DATABASE_URL) run in an isolated schema through the
HTTP API, including asyncio.gather duplicate storms against real row locks.
Fixture identities are synthetic: no real CNIC, phone or personal data.
"""
import asyncio
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "idempotency-tests-only-not-a-production-key",
}):
    import database
    import idempotency
    from main import app
    from models import Base, Booking, BookingStatus, IdempotencyKey, Ride, User, UserRole
    from routers.auth import create_access_token


class KeyValidationTests(unittest.TestCase):
    def test_accepts_bounded_visible_ascii(self):
        for key in ("a", "x" * 64, str(uuid4()), "retry:2026-10-31T06:00Z#1", "!~"):
            with self.subTest(key=key):
                self.assertEqual(idempotency.validate_key(key), key)
        self.assertIsNone(idempotency.validate_key(None))

    def test_rejects_empty_long_whitespace_control_and_non_ascii(self):
        for key in ("", "x" * 65, "a b", " a", "a\t", "a\x00", "caf\u00e9", "\u0661\u0662", "a\x7f"):
            with self.subTest(key=repr(key)):
                with self.assertRaises(HTTPException) as raised:
                    idempotency.validate_key(key)
                self.assertEqual((raised.exception.status_code, raised.exception.detail),
                                 (422, idempotency.INVALID_KEY))

    def test_fingerprint_is_canonical_and_discriminating(self):
        base = idempotency.fingerprint("booking.respond", {"booking_id": 7, "accept": True})
        self.assertEqual(base, idempotency.fingerprint(
            "booking.respond", {"accept": True, "booking_id": 7}))
        for other in (
            idempotency.fingerprint("booking.respond", {"booking_id": 7, "accept": False}),
            idempotency.fingerprint("booking.respond", {"booking_id": 8, "accept": True}),
            idempotency.fingerprint("booking.create", {"booking_id": 7, "accept": True}),
        ):
            self.assertNotEqual(base, other)
        self.assertRegex(base, r"^[0-9a-f]{64}$")

    def test_ttl_configuration_is_validated(self):
        with patch.dict(os.environ, {"IDEMPOTENCY_KEY_TTL_HOURS": "48"}):
            self.assertEqual(idempotency._ttl_from_environment(), timedelta(hours=48))
        for bad in ("0", "-1", "721", "1.5", "day"):
            with self.subTest(bad=bad), patch.dict(os.environ, {"IDEMPOTENCY_KEY_TTL_HOURS": bad}):
                with self.assertRaises(RuntimeError):
                    idempotency._ttl_from_environment()
        self.assertEqual(idempotency.KEY_TTL, timedelta(hours=24))


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class IdempotencyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_idempotency_" + uuid4().hex
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
            roles = [UserRole.driver] + [UserRole.passenger] * 4
            users = [User(name=f"fixture-{i}", email=f"idem{i}@example.com",
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

    async def seed_ride(self, seats=2, departure=None, pending_for=()):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.driver.id, origin="Lahore", destination="Islamabad",
                        departure_time=departure or datetime.now(timezone.utc) + timedelta(days=1),
                        available_seats=seats, fare_per_seat=1500)
            db.add(ride)
            await db.flush()
            bookings = [Booking(ride_id=ride.id, passenger_id=user.id) for user in pending_for]
            db.add_all(bookings)
            await db.commit()
            return ride.id, [booking.id for booking in bookings]

    async def book(self, ride_id, key=None, user=None):
        return await self.client.post(f"/bookings/{ride_id}", headers=self.headers(user or self.passenger, key))

    async def respond(self, booking_id, accept, key=None, accept_text=None):
        return await self.client.patch(
            f"/bookings/{booking_id}/respond",
            params={"accept": accept_text or str(accept).lower()},
            headers=self.headers(self.driver, key))

    async def scalar(self, statement):
        async with AsyncSession(self.engine) as db:
            return await db.scalar(statement)

    async def booking_count(self, ride_id=None):
        query = select(func.count()).select_from(Booking)
        if ride_id is not None:
            query = query.where(Booking.ride_id == ride_id)
        return await self.scalar(query)

    async def key_count(self):
        return await self.scalar(select(func.count()).select_from(IdempotencyKey))

    # --- Create -------------------------------------------------------------

    async def test_without_header_behaviour_is_unchanged(self):
        ride_id, _ = await self.seed_ride()
        first, second = await self.book(ride_id), await self.book(ride_id)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json(), {"detail": "You already booked this ride"})
        self.assertNotIn("Idempotent-Replayed", first.headers)
        self.assertEqual(await self.key_count(), 0)

    async def test_retry_with_same_key_replays_original_201(self):
        ride_id, _ = await self.seed_ride()
        first = await self.book(ride_id, key="create-1")
        retry = await self.book(ride_id, key="create-1")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertNotIn("idempotent-replayed", first.headers)
        self.assertEqual((retry.status_code, retry.json()), (201, first.json()))
        self.assertEqual(retry.headers["idempotent-replayed"], "true")
        self.assertEqual(await self.booking_count(ride_id), 1)
        row = await self.scalar(select(IdempotencyKey))
        self.assertEqual((row.user_id, row.key, row.operation, row.status_code, row.response_body),
                         (self.passenger.id, "create-1", "booking.create", 201, first.json()))
        self.assertAlmostEqual((row.expires_at - row.created_at).total_seconds(), 24 * 3600, delta=1)

    async def test_replay_returns_original_result_after_state_changes(self):
        ride_id, _ = await self.seed_ride()
        first = await self.book(ride_id, key="create-then-accept")
        accepted = await self.respond(first.json()["id"], True)
        self.assertEqual(accepted.json()["status"], "accepted")
        retry = await self.book(ride_id, key="create-then-accept")
        self.assertEqual((retry.status_code, retry.json()["status"]), (201, "pending"))
        self.assertEqual(retry.json(), first.json())

    async def test_same_key_different_payload_is_422_without_writes(self):
        ride_a, _ = await self.seed_ride()
        ride_b, _ = await self.seed_ride()
        await self.book(ride_a, key="k")
        reused = await self.book(ride_b, key="k")
        self.assertEqual(reused.status_code, 422)
        self.assertEqual(reused.json(), {"detail": idempotency.KEY_REUSED})
        self.assertEqual(await self.booking_count(ride_b), 0)
        # Same key on another operation is also a different payload.
        booking_id = await self.scalar(select(Booking.id).where(Booking.ride_id == ride_a))
        other_user_key = await self.client.patch(
            f"/bookings/{booking_id}/respond", params={"accept": "true"},
            headers=self.headers(self.passenger, "k"))
        self.assertEqual(other_user_key.status_code, 422)
        self.assertEqual(await self.scalar(select(Booking.status).where(Booking.id == booking_id)),
                         BookingStatus.pending)

    async def test_keys_are_scoped_per_user(self):
        ride_id, _ = await self.seed_ride(seats=3)
        responses = [await self.book(ride_id, key="shared", user=user) for user in self.passengers[:3]]
        self.assertEqual([r.status_code for r in responses], [201, 201, 201])
        self.assertEqual(len({r.json()["id"] for r in responses}), 3)
        self.assertEqual({r.json()["passenger_id"] for r in responses},
                         {user.id for user in self.passengers[:3]})
        self.assertTrue(all("idempotent-replayed" not in r.headers for r in responses))

    async def test_errors_are_not_stored_and_key_is_reusable(self):
        ride_id, _ = await self.seed_ride(seats=0)
        full = await self.book(ride_id, key="retry-when-free")
        self.assertEqual((full.status_code, full.json()), (409, {"detail": "No seats available"}))
        self.assertEqual(await self.key_count(), 0)
        async with self.engine.begin() as conn:
            await conn.execute(update(Ride).where(Ride.id == ride_id).values(available_seats=1))
        retry = await self.book(ride_id, key="retry-when-free")
        self.assertEqual(retry.status_code, 201)
        self.assertNotIn("idempotent-replayed", retry.headers)
        # Authorisation failures do not leave a key behind either.
        own = await self.client.post(f"/bookings/{ride_id}", headers=self.headers(self.driver, "own"))
        self.assertEqual(own.status_code, 403)
        missing = await self.book(999999, key="missing-ride")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(await self.key_count(), 1)

    async def test_invalid_header_is_422_without_writes(self):
        ride_id, _ = await self.seed_ride()
        for key in ("", "x" * 65, "has space"):
            with self.subTest(key=key):
                response = await self.book(ride_id, key=key)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": idempotency.INVALID_KEY})
        self.assertEqual((await self.booking_count(ride_id), await self.key_count()), (0, 0))

    async def test_concurrent_duplicate_requests_create_exactly_one_booking(self):
        ride_id, _ = await self.seed_ride()
        responses = await asyncio.wait_for(asyncio.gather(*(
            self.book(ride_id, key="storm") for _ in range(12))), timeout=60)
        self.assertEqual([r.status_code for r in responses], [201] * 12,
                         [r.text for r in responses])
        self.assertEqual([r.json() for r in responses], [responses[0].json()] * 12)  # jsonb may reorder keys
        self.assertEqual(sum("idempotent-replayed" not in r.headers for r in responses), 1)
        self.assertEqual(await self.booking_count(ride_id), 1)
        self.assertEqual(await self.key_count(), 1)

    async def test_concurrent_distinct_keys_still_hit_duplicate_booking_guard(self):
        ride_id, _ = await self.seed_ride()
        responses = await asyncio.wait_for(asyncio.gather(*(
            self.book(ride_id, key=f"distinct-{i}") for i in range(6))), timeout=60)
        self.assertEqual(sorted(r.status_code for r in responses), [201] + [409] * 5)
        self.assertEqual(await self.booking_count(ride_id), 1)
        self.assertEqual(await self.key_count(), 1)

    async def test_concurrent_same_key_different_payloads_one_wins_others_422(self):
        rides = [(await self.seed_ride())[0] for _ in range(4)]
        responses = await asyncio.wait_for(asyncio.gather(*(
            self.book(ride_id, key="contested") for ride_id in rides)), timeout=60)
        self.assertEqual(sorted(r.status_code for r in responses), [201, 422, 422, 422])
        self.assertEqual(await self.booking_count(), 1)

    # --- Respond -------------------------------------------------------------

    async def test_accept_retry_replays_and_debits_one_seat(self):
        ride_id, (booking_id,) = await self.seed_ride(seats=2, pending_for=[self.passenger])
        first = await self.respond(booking_id, True, key="accept-1")
        retry = await self.respond(booking_id, True, key="accept-1", accept_text="1")
        self.assertEqual((first.status_code, retry.status_code), (200, 200))
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(retry.headers["idempotent-replayed"], "true")
        self.assertEqual(await self.scalar(select(Ride.available_seats).where(Ride.id == ride_id)), 1)
        # Control: the same retry without a key is the historical 409.
        control = await self.respond(booking_id, True)
        self.assertEqual(control.json(), {"detail": "Booking already responded to"})

    async def test_accept_then_reject_with_same_key_is_422(self):
        ride_id, (booking_id,) = await self.seed_ride(pending_for=[self.passenger])
        self.assertEqual((await self.respond(booking_id, True, key="decide")).status_code, 200)
        flipped = await self.respond(booking_id, False, key="decide")
        self.assertEqual((flipped.status_code, flipped.json()), (422, {"detail": idempotency.KEY_REUSED}))
        self.assertEqual(await self.scalar(select(Booking.status).where(Booking.id == booking_id)),
                         BookingStatus.accepted)

    async def test_concurrent_accepts_with_same_key_debit_once(self):
        ride_id, (booking_id,) = await self.seed_ride(seats=1, pending_for=[self.passenger])
        responses = await asyncio.wait_for(asyncio.gather(*(
            self.respond(booking_id, True, key="tap-tap") for _ in range(8))), timeout=60)
        self.assertEqual([r.status_code for r in responses], [200] * 8, [r.text for r in responses])
        self.assertEqual(await self.scalar(select(Ride.available_seats).where(Ride.id == ride_id)), 0)
        self.assertEqual(sum("idempotent-replayed" not in r.headers for r in responses), 1)

    # --- TTL ----------------------------------------------------------------

    async def test_expired_key_is_reclaimed_for_a_new_request(self):
        ride_a, _ = await self.seed_ride()
        ride_b, _ = await self.seed_ride()
        await self.book(ride_a, key="old")
        async with self.engine.begin() as conn:
            await conn.execute(update(IdempotencyKey).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        fresh = await self.book(ride_b, key="old")
        self.assertEqual(fresh.status_code, 201, fresh.text)
        self.assertNotIn("idempotent-replayed", fresh.headers)
        row = await self.scalar(select(IdempotencyKey))
        self.assertEqual(row.response_body["ride_id"], ride_b)
        self.assertGreater(row.expires_at, datetime.now(timezone.utc))

    async def test_purge_deletes_only_expired_keys_and_is_idempotent(self):
        ride_id, _ = await self.seed_ride(seats=4)
        for user in self.passengers[:3]:
            await self.book(ride_id, key="purge-me", user=user)
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            await conn.execute(update(IdempotencyKey).where(
                IdempotencyKey.user_id != self.passengers[2].id).values(expires_at=now))
        async with AsyncSession(self.engine) as db:
            self.assertEqual(await idempotency.purge_expired(db, now=now, batch_size=1), 2)
            self.assertEqual(await idempotency.purge_expired(db, now=now), 0)
        self.assertEqual(await self.key_count(), 1)

    async def test_deleting_a_user_cascades_to_their_keys(self):
        ride_id, _ = await self.seed_ride()
        await self.book(ride_id, key="gone", user=self.passengers[3])
        async with self.engine.begin() as conn:
            await conn.execute(text("DELETE FROM bookings"))
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": self.passengers[3].id})
        self.assertEqual(await self.key_count(), 0)


class PurgeCliTests(unittest.TestCase):
    def test_cli_rejects_bad_batch_size_before_touching_the_database(self):
        env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://nobody@127.0.0.1:1/none",
               "SECRET_KEY": "idempotency-tests-only-not-a-production-key"}
        result = subprocess.run([sys.executable, "-B", "-m", "jobs.purge_idempotency_keys",
                                 "--batch-size", "0"], cwd=PROJECT_ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--batch-size must be between 1 and 10000", result.stderr)


if __name__ == "__main__":
    unittest.main()
