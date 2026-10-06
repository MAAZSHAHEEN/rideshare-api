"""PostgreSQL booking-response tests: set TEST_DATABASE_URL, then run
python -B -m unittest discover -s tests -p test_booking_responses.py -v

Use a dedicated postgresql+asyncpg test database with CREATE SCHEMA permission.
Each test commits fixtures in a unique schema so independent connections can see
them. Teardown drops only that schema. App lifespan and migrations are not run.
"""

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "booking-tests-only-not-a-production-key",
}):
    from database import Base, get_db
    from main import app
    from models import Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class BookingResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = f"test_booking_responses_{uuid4().hex}"
        self.engine = create_async_engine(
            TEST_DATABASE_URL,
            isolation_level="READ COMMITTED",
            connect_args={"server_settings": {
                "search_path": self.schema,
                "lock_timeout": "15000",
                "statement_timeout": "20000",
            }},
        )
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.execute(CreateSchema(self.schema))
            await connection.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            users = [User(
                name=f"User {index}", email=f"user{index}@example.com",
                password="unused-test-hash", cnic=str(index), phone_number="03001234567",
                role=role,
            ) for index, role in enumerate((
                UserRole.driver, UserRole.driver, UserRole.passenger,
                UserRole.passenger, UserRole.admin,
            ))]
            session.add_all(users)
            await session.flush()
            self.driver, self.other_driver, self.passenger, _, self.admin = users
            ride = Ride(
                driver_id=self.driver.id, origin="Peshawar", destination="Islamabad",
                departure_time=datetime.now(timezone.utc) + timedelta(days=1),
                available_seats=2, fare_per_seat=500,
            )
            session.add(ride)
            await session.flush()
            bookings = [Booking(ride_id=ride.id, passenger_id=user.id) for user in users[2:4]]
            session.add_all(bookings)
            await session.commit()
            self.ride_id = ride.id
            self.booking_ids = [booking.id for booking in bookings]

        self.request_pids = []
        self.preload_state = False

        async def override_get_db():
            # Each HTTP request owns a session/connection and commits for real.
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                pid = await session.scalar(select(func.pg_backend_pid()))
                if self.preload_state:
                    # Strong references retain ORM identity-map state across waits.
                    session.info["preloaded"] = [
                        await session.get(Ride, self.ride_id),
                        await session.get(Booking, self.booking_ids[0]),
                    ]
                self.request_pids.append(pid)
                yield session

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True))

    async def respond(self, booking_id, accept, user=None):
        user = user or self.driver
        token = create_access_token(user.id, user.role)
        return await self.client.patch(
            f"/bookings/{booking_id}/respond",
            params={"accept": str(accept).lower()},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def state(self):
        async with AsyncSession(self.engine) as session:
            ride = await session.get(Ride, self.ride_id)
            bookings = (await session.scalars(select(Booking).order_by(Booking.id))).all()
            return ride.available_seats, [booking.status for booking in bookings]

    async def update_ride(self, **values):
        async with self.engine.begin() as connection:
            await connection.execute(update(Ride).where(Ride.id == self.ride_id).values(**values))

    async def contended_responses(self, decisions, while_locked=None):
        """Hold the ride until every independent request is waiting in PostgreSQL.

        This forces the original implementation to read pending before waiting;
        merely scheduling two requests together would not reliably expose it.
        """
        self.request_pids.clear()
        tasks = []
        try:
            async with self.engine.begin() as coordinator:
                coordinator_pid = await coordinator.scalar(select(func.pg_backend_pid()))
                await coordinator.execute(
                    select(Ride.id).where(Ride.id == self.ride_id).with_for_update()
                )
                tasks = [asyncio.create_task(self.respond(booking_id, accept))
                         for booking_id, accept in decisions]
                async with asyncio.timeout(10):
                    while True:
                        for task in tasks:
                            if task.done():
                                response = task.result()
                                self.fail(f"Request did not wait for the ride lock: {response.status_code}")
                        if len(self.request_pids) == len(tasks):
                            self.assertEqual(len(set(self.request_pids)), len(tasks))
                            self.assertNotIn(coordinator_pid, self.request_pids)
                            blocked = [await coordinator.scalar(
                                select(func.cardinality(func.pg_blocking_pids(pid)))
                            ) for pid in self.request_pids]
                            if all(blocked):
                                break
                        await asyncio.sleep(0.01)
                if while_locked is not None:
                    await while_locked(coordinator)
            # Releasing the coordinator's lock lets PostgreSQL serialize requests.
            async with asyncio.timeout(10):
                return await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def assert_one_winner(self, responses):
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])

    async def test_same_booking_concurrent_accepts_consume_one_seat(self):
        booking_id = self.booking_ids[0]
        responses = await self.contended_responses([(booking_id, True), (booking_id, True)])
        self.assert_one_winner(responses)
        self.assertEqual(await self.state(), (1, [BookingStatus.accepted, BookingStatus.pending]))

    async def test_same_booking_concurrent_accept_reject_preserves_winner(self):
        booking_id = self.booking_ids[0]
        responses = await self.contended_responses([(booking_id, True), (booking_id, False)])
        self.assert_one_winner(responses)
        winner = next(response for response in responses if response.status_code == 200)
        final_status = BookingStatus(winner.json()["status"])
        expected_seats = 1 if final_status == BookingStatus.accepted else 2
        self.assertEqual(await self.state(), (expected_seats, [final_status, BookingStatus.pending]))

    async def test_different_bookings_compete_for_final_seat(self):
        await self.update_ride(available_seats=1)
        responses = await self.contended_responses([(booking_id, True) for booking_id in self.booking_ids])
        self.assert_one_winner(responses)
        seats, statuses = await self.state()
        self.assertEqual(seats, 0)
        self.assertEqual(statuses.count(BookingStatus.accepted), 1)
        self.assertEqual(statuses.count(BookingStatus.pending), 1)

    async def test_already_responded_bookings_cannot_change(self):
        for status in (BookingStatus.accepted, BookingStatus.rejected, BookingStatus.cancelled):
            for accept in (True, False):
                with self.subTest(status=status, accept=accept):
                    async with self.engine.begin() as connection:
                        await connection.execute(update(Booking).where(
                            Booking.id == self.booking_ids[0]
                        ).values(status=status))
                    before = await self.state()
                    response = await self.respond(self.booking_ids[0], accept)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(await self.state(), before)

    async def test_other_driver_cannot_respond(self):
        before = await self.state()
        response = await self.respond(self.booking_ids[0], True, self.other_driver)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(await self.state(), before)

    async def test_only_drivers_can_respond(self):
        before = await self.state()
        for user in (self.passenger, self.admin):
            with self.subTest(role=user.role):
                response = await self.respond(self.booking_ids[0], True, user)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(await self.state(), before)

    async def test_missing_booking_returns_404(self):
        response = await self.respond(max(self.booking_ids) + 1, True)
        self.assertEqual(response.status_code, 404, response.text)

    async def test_preloaded_booking_is_refreshed_after_lock_wait(self):
        self.preload_state = True

        async def decide_booking(connection):
            await connection.execute(update(Booking).where(
                Booking.id == self.booking_ids[0]
            ).values(status=BookingStatus.rejected))

        responses = await self.contended_responses([(self.booking_ids[0], True)], decide_booking)
        self.assertEqual(responses[0].status_code, 409, responses[0].text)
        self.assertEqual(await self.state(), (2, [BookingStatus.rejected, BookingStatus.pending]))

    async def test_ride_status_is_refreshed_after_lock_wait(self):
        self.preload_state = True

        async def deactivate_ride(connection):
            await connection.execute(update(Ride).where(
                Ride.id == self.ride_id
            ).values(status=RideStatus.cancelled))

        responses = await self.contended_responses([(self.booking_ids[0], True)], deactivate_ride)
        self.assertEqual(responses[0].status_code, 409, responses[0].text)
        self.assertEqual(responses[0].json()["detail"], "Ride is not active")
        self.assertEqual(await self.state(), (2, [BookingStatus.pending, BookingStatus.pending]))

    async def test_capacity_is_refreshed_after_lock_wait(self):
        self.preload_state = True

        async def consume_capacity(connection):
            await connection.execute(update(Ride).where(
                Ride.id == self.ride_id
            ).values(available_seats=0))

        responses = await self.contended_responses([(self.booking_ids[0], True)], consume_capacity)
        self.assertEqual(responses[0].status_code, 409, responses[0].text)
        self.assertEqual(responses[0].json()["detail"], "No seats available")
        self.assertEqual(await self.state(), (0, [BookingStatus.pending, BookingStatus.pending]))

    async def test_failed_commit_rolls_back_booking_and_seats(self):
        before = await self.state()

        async def fail_after_flush(session):
            await session.flush()
            raise RuntimeError("simulated commit failure")

        with patch.object(AsyncSession, "commit", fail_after_flush):
            with self.assertRaisesRegex(RuntimeError, "simulated commit failure"):
                await self.respond(self.booking_ids[0], True)
        self.assertEqual(await self.state(), before)

    async def test_active_booking_precheck_returns_conflict(self):
        token = create_access_token(self.passenger.id, self.passenger.role)
        for status in (BookingStatus.pending, BookingStatus.accepted):
            with self.subTest(status=status):
                async with self.engine.begin() as connection:
                    await connection.execute(update(Booking).where(
                        Booking.id == self.booking_ids[0]
                    ).values(status=status))
                before = await self.state()
                response = await self.client.post(
                    f"/bookings/{self.ride_id}", headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(await self.state(), before)

    async def test_historical_booking_allows_a_new_request(self):
        token = create_access_token(self.passenger.id, self.passenger.role)
        for status in (BookingStatus.rejected, BookingStatus.cancelled):
            with self.subTest(status=status):
                # Preserve history and retire the previous iteration's pending request.
                async with self.engine.begin() as connection:
                    await connection.execute(update(Booking).where(
                        Booking.passenger_id == self.passenger.id
                    ).values(status=status))
                response = await self.client.post(
                    f"/bookings/{self.ride_id}", headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()["status"], "pending")

    async def test_competing_writer_uniqueness_violation_returns_conflict_and_rolls_back(self):
        # An external status update can bypass the route's ride-lock protocol.
        # Reactivate a historical row after the precheck but before API insertion.
        async with self.engine.begin() as connection:
            await connection.execute(update(Booking).where(
                Booking.id == self.booking_ids[0]
            ).values(status=BookingStatus.rejected))
        original_commit = AsyncSession.commit
        original_rollback = AsyncSession.rollback
        rollbacks = []

        async def competing_commit(session):
            with session.no_autoflush:
                api_pid = await session.scalar(select(func.pg_backend_pid()))
            async with self.engine.begin() as writer:
                writer_pid = await writer.scalar(select(func.pg_backend_pid()))
                self.assertNotEqual(api_pid, writer_pid)
                await writer.execute(update(Booking).where(
                    Booking.id == self.booking_ids[0]
                ).values(status=BookingStatus.pending))
            await original_commit(session)

        async def record_rollback(session):
            await original_rollback(session)
            rollbacks.append(session)

        token = create_access_token(self.passenger.id, self.passenger.role)
        with patch.object(AsyncSession, "commit", competing_commit):
            with patch.object(AsyncSession, "rollback", record_rollback):
                response = await self.client.post(
                    f"/bookings/{self.ride_id}", headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "You already booked this ride"})
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(await self.state(), (2, [BookingStatus.pending, BookingStatus.pending]))

    async def test_unrelated_integrity_error_is_not_mislabeled_as_duplicate(self):
        async with self.engine.begin() as connection:
            await connection.execute(update(Booking).where(
                Booking.id == self.booking_ids[0]
            ).values(status=BookingStatus.rejected))
        before = await self.state()

        async def invalid_commit(session):
            await session.flush()
            await session.execute(update(Ride).where(
                Ride.id == self.ride_id
            ).values(available_seats=-1))

        token = create_access_token(self.passenger.id, self.passenger.role)
        with patch.object(AsyncSession, "commit", invalid_commit):
            with self.assertRaises(IntegrityError) as raised:
                await self.client.post(
                    f"/bookings/{self.ride_id}", headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(raised.exception.orig.sqlstate, "23514")
        self.assertEqual(await self.state(), before)

    async def test_negative_ride_inputs_return_validation_errors(self):
        token = create_access_token(self.driver.id, self.driver.role)
        payload = {
            "origin": "Peshawar", "destination": "Islamabad",
            "departure_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "available_seats": 2, "fare_per_seat": 500,
        }
        for field in ("available_seats", "fare_per_seat"):
            with self.subTest(field=field):
                response = await self.client.post(
                    "/rides/", json={**payload, field: -1},
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertTrue(any(error["loc"] == ["body", field]
                                    for error in response.json()["detail"]))

    async def test_zero_ride_seats_and_fare_remain_valid(self):
        token = create_access_token(self.driver.id, self.driver.role)
        response = await self.client.post(
            "/rides/", json={
                "origin": "Peshawar", "destination": "Islamabad",
                "departure_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                "available_seats": 0, "fare_per_seat": 0,
            }, headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 201, response.text)


if __name__ == "__main__":
    unittest.main()
