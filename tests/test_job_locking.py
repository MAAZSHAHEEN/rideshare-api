"""Single-runner advisory lock for the maintenance jobs (jobs/locking.py).

PostgreSQL tests (set TEST_DATABASE_URL). Each test uses its own schema for
rows; the advisory lock itself is database-wide, which is the point: two
runners against one database must serialise.
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

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": os.environ.get("SECRET_KEY") or "test-only-signing-key-0123456789abcdef",
}):
    import booking_expiry
    import idempotency
    from jobs import expire_bookings, purge_idempotency_keys
    from jobs.locking import JOB_LOCK_IDS, JOB_LOCK_NAMESPACE, job_lock
    from models import Base, Booking, BookingStatus, Ride, User, UserRole

ROOT = Path(__file__).resolve().parent.parent


class LockKeyTests(unittest.TestCase):
    def test_keys_are_distinct_int4_values(self):
        ids = list(JOB_LOCK_IDS.values())
        self.assertEqual(len(ids), len(set(ids)))
        for value in [JOB_LOCK_NAMESPACE, *ids]:
            self.assertTrue(-2**31 <= value < 2**31)
        self.assertEqual(set(JOB_LOCK_IDS), {"expire_bookings", "purge_idempotency_keys"})


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class JobLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_joblock_" + uuid4().hex
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=5,
                                          connect_args={"server_settings": {
                                              "search_path": self.schema,
                                              "statement_timeout": "20000"}})
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    async def seed_due_request(self):
        """One active future-dated ride with one pending request already past expiry."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            driver = User(name="fixture-driver", email=f"d-{uuid4().hex[:8]}@example.com",
                          cnic=f"fixture-cnic-{uuid4().hex[:8]}", password="unused",
                          phone_number="000", role=UserRole.driver)
            rider = User(name="fixture-rider", email=f"p-{uuid4().hex[:8]}@example.com",
                         cnic=f"fixture-cnic-{uuid4().hex[:8]}", password="unused",
                         phone_number="000", role=UserRole.passenger)
            db.add_all([driver, rider])
            await db.flush()
            ride = Ride(driver_id=driver.id, origin="Lahore", destination="Islamabad",
                        departure_time=now + timedelta(days=1), available_seats=3,
                        fare_per_seat=1500)
            db.add(ride)
            await db.flush()
            booking = Booking(ride_id=ride.id, passenger_id=rider.id,
                              status=BookingStatus.pending, expires_at=now - timedelta(minutes=1))
            db.add(booking)
            await db.commit()
            return booking.id

    async def booking_status(self, booking_id):
        async with AsyncSession(self.engine) as db:
            return await db.scalar(select(Booking.status).where(Booking.id == booking_id))

    async def lock_holders(self):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid::bigint = :ns AND granted"), {"ns": JOB_LOCK_NAMESPACE})

    async def test_lock_is_exclusive_and_released(self):
        async with job_lock(self.engine, "expire_bookings") as first:
            self.assertTrue(first)
            async with job_lock(self.engine, "expire_bookings") as second:
                self.assertFalse(second)
        self.assertEqual(await self.lock_holders(), 0)
        async with job_lock(self.engine, "expire_bookings") as again:
            self.assertTrue(again)

    async def test_different_jobs_do_not_block_each_other(self):
        async with job_lock(self.engine, "expire_bookings") as expire:
            async with job_lock(self.engine, "purge_idempotency_keys") as purge:
                self.assertTrue(expire)
                self.assertTrue(purge)

    async def test_run_skips_and_changes_nothing_while_another_runner_holds_the_lock(self):
        booking_id = await self.seed_due_request()
        async with job_lock(self.engine, "expire_bookings") as held:
            self.assertTrue(held)
            self.assertIsNone(await expire_bookings.run(100, engine=self.engine))
            self.assertEqual(await self.booking_status(booking_id), BookingStatus.pending)
        result = await expire_bookings.run(100, engine=self.engine)
        self.assertEqual(result.expired_requests, 1)
        self.assertEqual(await self.booking_status(booking_id), BookingStatus.expired)

    async def test_purge_skips_while_another_runner_holds_the_lock(self):
        async with job_lock(self.engine, "purge_idempotency_keys"):
            self.assertIsNone(await purge_idempotency_keys.run(100, engine=self.engine))
        self.assertEqual(await purge_idempotency_keys.run(100, engine=self.engine), 0)

    async def test_concurrent_double_run_has_exactly_one_effective_run(self):
        booking_id = await self.seed_due_request()
        real_sweep = booking_expiry.sweep
        entered = asyncio.Event()

        async def slow_sweep(db, *args, **kwargs):
            entered.set()
            await asyncio.sleep(0.5)  # keep the lock held while the second copy starts
            return await real_sweep(db, *args, **kwargs)

        async def second_after_first_entered():
            await entered.wait()
            return await expire_bookings.run(100, engine=self.engine)

        with patch.object(booking_expiry, "sweep", slow_sweep):
            first, second = await asyncio.gather(
                expire_bookings.run(100, engine=self.engine), second_after_first_entered())
        results = [first, second]
        effective = [r for r in results if r is not None]
        self.assertEqual(len(effective), 1, results)
        self.assertIsNone(second)
        self.assertEqual(effective[0].expired_requests, 1)
        self.assertEqual(await self.booking_status(booking_id), BookingStatus.expired)
        self.assertEqual(await self.lock_holders(), 0)

    async def test_concurrent_purges_have_exactly_one_effective_run(self):
        real_purge = idempotency.purge_expired
        entered = asyncio.Event()

        async def slow_purge(db, *args, **kwargs):
            entered.set()
            await asyncio.sleep(0.5)
            return await real_purge(db, *args, **kwargs)

        async def second_after_first_entered():
            await entered.wait()
            return await purge_idempotency_keys.run(100, engine=self.engine)

        with patch.object(idempotency, "purge_expired", slow_purge):
            first, second = await asyncio.gather(
                purge_idempotency_keys.run(100, engine=self.engine), second_after_first_entered())
        self.assertEqual(first, 0)
        self.assertIsNone(second)

    async def test_lock_is_released_when_the_job_raises(self):
        async def broken_sweep(db, *args, **kwargs):
            raise RuntimeError("simulated failure")

        with patch.object(booking_expiry, "sweep", broken_sweep):
            with self.assertRaises(RuntimeError):
                await expire_bookings.run(100, engine=self.engine)
        self.assertEqual(await self.lock_holders(), 0)
        self.assertIsNotNone(await expire_bookings.run(100, engine=self.engine))

    async def test_lock_dies_with_its_connection(self):
        """A crashed runner (connection gone, no unlock) never blocks the next run."""
        holder_engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(holder_engine.dispose)
        conn = await holder_engine.connect()
        self.assertTrue(await conn.scalar(text("SELECT pg_try_advisory_lock(:ns, :job)"), {
            "ns": JOB_LOCK_NAMESPACE, "job": JOB_LOCK_IDS["expire_bookings"]}))
        self.assertIsNone(await expire_bookings.run(100, engine=self.engine))
        await conn.invalidate()  # drop the server session without unlocking
        await conn.close()
        await holder_engine.dispose()
        for _ in range(50):  # the server notices the closed socket asynchronously
            if await self.lock_holders() == 0:
                break
            await asyncio.sleep(0.1)
        self.assertIsNotNone(await expire_bookings.run(100, engine=self.engine))

    async def test_cli_reports_skip_and_exits_zero(self):
        env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL,
               "SECRET_KEY": os.environ.get("SECRET_KEY") or "test-only-signing-key-0123456789abcdef"}
        async with job_lock(self.engine, "expire_bookings"), \
                job_lock(self.engine, "purge_idempotency_keys"):
            for module in ("jobs.expire_bookings", "jobs.purge_idempotency_keys"):
                result = await asyncio.to_thread(
                    subprocess.run, [sys.executable, "-B", "-m", module], cwd=ROOT, env=env,
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "job_lock=skipped")


if __name__ == "__main__":
    unittest.main()
