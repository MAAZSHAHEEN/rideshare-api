"""Transaction boundaries on real PostgreSQL; set TEST_DATABASE_URL.

Use the production get_db generator with its existing factory bound to an
isolated schema. No dependency override or production database is used.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "transaction-tests-only-not-a-production-key",
}):
    import database
    from main import app
    from models import Base, Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token
    from schemas import RideCreate
    from services import bookings, rides


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class TransactionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_transactions_" + uuid4().hex
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
            users = [User(name=str(i), email=f"fixture{i}@example.com", cnic=str(i),
                          password="unused", phone_number="000", role=role)
                     for i, role in enumerate((UserRole.driver, UserRole.driver,
                                               UserRole.passenger, UserRole.passenger))]
            db.add_all(users)
            await db.commit()
            self.driver, self.other_driver, self.passenger, self.other_passenger = users
        binding = patch.dict(database.SessionLocal.kw, {"bind": self.engine})
        binding.start()
        self.addCleanup(binding.stop)
        self.assertNotIn(database.get_db, app.dependency_overrides)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def payload(self):
        return dict(origin="A", destination="B", available_seats=2, fare_per_seat=100,
                    departure_time=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    def registration(self):
        key = uuid4().hex
        return dict(name="Test", email=f"{key}@example.com", cnic=key,
                    phone_number="000", password="Valid-password-123!", role="passenger")

    async def seed(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.driver.id, **RideCreate(**self.payload()).model_dump())
            db.add(ride)
            await db.flush()
            booking = Booking(ride_id=ride.id, passenger_id=self.passenger.id)
            db.add(booking)
            await db.commit()
            return ride.id, booking.id

    async def snapshot(self):
        async with AsyncSession(self.engine) as db:
            return (
                (await db.execute(select(Ride.id, Ride.status, Ride.available_seats).order_by(Ride.id))).all(),
                (await db.execute(select(Booking.id, Booking.status).order_by(Booking.id))).all(),
                await db.scalar(select(func.count()).select_from(User)),
            )

    async def assert_unlocked(self, ride_id, booking_id):
        async with self.engine.begin() as conn:
            await conn.execute(select(Ride.id).where(Ride.id == ride_id).with_for_update(nowait=True))
            await conn.execute(select(Booking.id).where(Booking.id == booking_id).with_for_update(nowait=True))

    async def operate(self, operation, db, ride_id, booking_id):
        if operation == "book":
            return await bookings.book_ride(ride_id, db, self.other_passenger)
        if operation == "respond":
            return await bookings.respond_to_booking(booking_id, True, db, self.driver)
        if operation == "cancel_booking":
            return await bookings.cancel_booking(booking_id, db, self.passenger)
        if operation == "create":
            return await rides.create_ride(RideCreate(**self.payload()), db, self.driver)
        if operation == "cancel_ride":
            return await rides.cancel_ride(ride_id, db, self.driver)
        return await rides.complete_ride(ride_id, db, self.driver)

    async def test_direct_conflicts_release_locks_before_session_closes(self):
        for operation in ("book", "respond", "cancel_booking", "cancel_ride", "complete"):
            with self.subTest(operation=operation):
                ride_id, booking_id = await self.seed()
                async with AsyncSession(self.engine) as db:
                    with self.assertRaises(HTTPException):
                        if operation == "book":
                            await bookings.book_ride(ride_id, db, self.passenger)
                        elif operation == "respond":
                            await bookings.respond_to_booking(booking_id, True, db, self.other_driver)
                        elif operation == "cancel_booking":
                            await bookings.cancel_booking(booking_id, db, self.other_passenger)
                        elif operation == "cancel_ride":
                            await rides.cancel_ride(ride_id, db, self.other_driver)
                        else:
                            await rides.complete_ride(ride_id, db, self.other_driver)
                    self.assertFalse(db.in_transaction())
                    await self.assert_unlocked(ride_id, booking_id)

    async def check_failed_writes(self, database_error):
        for operation in ("book", "respond", "create", "cancel_booking", "cancel_ride", "complete"):
            with self.subTest(operation=operation):
                ride_id, booking_id = await self.seed()
                before = await self.snapshot()
                async with AsyncSession(self.engine, expire_on_commit=False) as db:
                    async def fail_commit():
                        await db.flush()
                        self.assertEqual(await self.snapshot(), before)
                        if database_error:
                            await db.execute(update(Ride).where(Ride.id == ride_id).values(fare_per_seat=-1))
                        raise RuntimeError("failure after flush")
                    with patch.object(db, "commit", fail_commit):
                        with self.assertRaises(IntegrityError if database_error else RuntimeError):
                            await self.operate(operation, db, ride_id, booking_id)
                    self.assertFalse(db.in_transaction())
                    self.assertFalse(db.new)
                    self.assertFalse(db.dirty)
                    await self.assert_unlocked(ride_id, booking_id)
                    self.assertEqual(await self.snapshot(), before)
                    # Reusing the caller session must not persist failed work.
                    await db.commit()
                    self.assertEqual(await self.snapshot(), before)

    async def test_direct_failed_commit_rolls_back_all_writes(self):
        await self.check_failed_writes(False)

    async def test_direct_postgresql_failure_rolls_back_and_propagates(self):
        await self.check_failed_writes(True)

    async def test_real_get_db_closes_after_http_failure(self):
        ride_id, booking_id = await self.seed()
        before = await self.snapshot()
        sessions = []
        async def fail_commit(db):
            sessions.append(db)
            await db.flush()
            raise RuntimeError("HTTP failure after flush")
        token = create_access_token(self.driver.id, self.driver.role)
        with patch.object(AsyncSession, "commit", fail_commit):
            with self.assertRaisesRegex(RuntimeError, "HTTP failure"):
                await self.client.post("/rides/", json=self.payload(),
                                       headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0].in_transaction())
        self.assertEqual(self.engine.pool.checkedout(), 0)
        self.assertEqual(await self.snapshot(), before)
        await self.assert_unlocked(ride_id, booking_id)
        # Authentication failure occurs outside any service rollback handler.
        bad_role_token = create_access_token(self.driver.id, UserRole.passenger)
        response = await self.client.post("/rides/", json=self.payload(),
                                          headers={"Authorization": f"Bearer {bad_role_token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    async def registration_race(self, field):
        first, second = self.registration(), self.registration()
        second[field] = first[field]
        ready = asyncio.Event()
        pids, failed_sessions = [], []
        original_commit = AsyncSession.commit
        async def coordinated_commit(db):
            with db.no_autoflush:
                pids.append(await db.scalar(select(func.pg_backend_pid())))
            if len(pids) == 2:
                ready.set()
            async with asyncio.timeout(10):
                await ready.wait()
            try:
                await original_commit(db)
            except IntegrityError:
                failed_sessions.append(db)
                raise
        with patch.object(AsyncSession, "commit", coordinated_commit):
            async with asyncio.timeout(20):
                responses = await asyncio.gather(
                    self.client.post("/auth/register", json=first),
                    self.client.post("/auth/register", json=second),
                    return_exceptions=True,
                )
        self.assertEqual(len(set(pids)), 2)
        self.assertTrue(all(not isinstance(r, BaseException) for r in responses),
                        "Registration race leaked a database exception")
        self.assertEqual(sorted(r.status_code for r in responses), [201, 409])
        conflict = next(r for r in responses if r.status_code == 409)
        self.assertEqual(conflict.json(), {"detail": "Registration details already in use"})
        self.assertEqual(len(failed_sessions), 1)
        self.assertFalse(failed_sessions[0].in_transaction())
        async with AsyncSession(self.engine) as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(User).where(
                getattr(User, field) == first[field])), 1)

    async def test_concurrent_registration_email_conflict(self):
        await self.registration_race("email")

    async def test_concurrent_registration_cnic_conflict(self):
        await self.registration_race("cnic")

    async def test_registration_unrelated_unique_error_propagates(self):
        original_commit = AsyncSession.commit
        original_rollback = AsyncSession.rollback
        rollbacks = []
        async def invalid_commit(db):
            next(user for user in db.new if isinstance(user, User)).id = self.driver.id
            await original_commit(db)
        async def record_rollback(db):
            await original_rollback(db)
            rollbacks.append(db)
        with patch.object(AsyncSession, "commit", invalid_commit), patch.object(AsyncSession, "rollback", record_rollback):
            with self.assertRaises(IntegrityError) as raised:
                await self.client.post("/auth/register", json=self.registration())
        self.assertEqual(raised.exception.orig.sqlstate, "23505")
        self.assertEqual(raised.exception.orig.__cause__.constraint_name, "users_pkey")
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual((await self.snapshot())[2], 4)

    async def test_refresh_failure_after_commit_preserves_every_operation(self):
        for operation in ("book", "respond", "cancel_booking", "create", "cancel_ride", "complete", "register"):
            with self.subTest(operation=operation):
                ride_id, booking_id = await self.seed()
                before = await self.snapshot()
                observed, persisted, rollbacks = [], [], []
                original_rollback = AsyncSession.rollback
                async def record_rollback(db):
                    await original_rollback(db)
                    rollbacks.append(db)
                original_refresh = AsyncSession.refresh
                async def fail_refresh(db, instance, *args, **kwargs):
                    # Actual refresh starts a second transaction after commit.
                    await original_refresh(db, instance, *args, **kwargs)
                    persisted.append(await self.snapshot())
                    observed.append(db)
                    await db.rollback()
                    self.assertEqual(await self.snapshot(), persisted[-1])
                    raise RuntimeError("refresh failed after successful commit")
                actor = self.driver
                method, path, kwargs = "patch", f"/rides/{ride_id}/complete", {}
                if operation == "book":
                    actor = self.other_passenger
                    method, path = "post", f"/bookings/{ride_id}"
                elif operation == "respond":
                    path, kwargs = f"/bookings/{booking_id}/respond", {"params": {"accept": "true"}}
                elif operation == "cancel_booking":
                    actor, path = self.passenger, f"/bookings/{booking_id}/cancel"
                elif operation == "create":
                    method, path, kwargs = "post", "/rides/", {"json": self.payload()}
                elif operation == "cancel_ride":
                    path = f"/rides/{ride_id}/cancel"
                elif operation == "register":
                    method, path, kwargs = "post", "/auth/register", {"json": self.registration()}
                kwargs["headers"] = {"Authorization": f"Bearer {create_access_token(actor.id, actor.role)}"}
                with patch.object(AsyncSession, "refresh", fail_refresh), patch.object(AsyncSession, "rollback", record_rollback):
                    with self.assertRaisesRegex(RuntimeError, "refresh failed"):
                        await getattr(self.client, method)(path, **kwargs)
                self.assertEqual(len(observed), 1)
                self.assertEqual(len(rollbacks), 1)  # Only the deliberately rolled-back refresh transaction.
                after_rides, after_bookings, after_users = persisted[0]
                if operation == "book":
                    self.assertEqual(after_rides, before[0])
                    self.assertEqual(len(after_bookings), len(before[1]) + 1)
                    self.assertEqual(after_bookings[-1].status, BookingStatus.pending)
                elif operation == "create":
                    self.assertEqual(len(after_rides), len(before[0]) + 1)
                    self.assertEqual(after_rides[-1].available_seats, 2)
                    self.assertEqual(after_bookings, before[1])
                elif operation == "register":
                    self.assertEqual(after_users, before[2] + 1)
                else:
                    ride = next(row for row in after_rides if row.id == ride_id)
                    booking = next(row for row in after_bookings if row.id == booking_id)
                    self.assertEqual(ride.available_seats, 1 if operation == "respond" else 2)
                    self.assertEqual(booking.status, BookingStatus.accepted if operation == "respond" else BookingStatus.cancelled)
                    self.assertEqual(ride.status, {"cancel_ride": RideStatus.cancelled,
                                                  "complete": RideStatus.completed}.get(operation, RideStatus.active))
                self.assertNotEqual(persisted[0], before)
                self.assertEqual(await self.snapshot(), persisted[0])
                self.assertFalse(observed[0].in_transaction())
                self.assertEqual(self.engine.pool.checkedout(), 0)
