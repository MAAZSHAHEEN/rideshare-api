"""Retries, failed runs and restart recovery for the maintenance jobs.

PostgreSQL tests (set TEST_DATABASE_URL). They prove, against a real server:

- a job that raises mid-batch releases its advisory lock, keeps the rides it
  already committed, rolls back the ride it was on, and a rerun finishes the
  rest idempotently;
- a job process killed with SIGKILL (no Python cleanup at all) holds the lock
  only as long as its server session lives: PostgreSQL ends the session when
  the socket closes and releases the session-level advisory lock and the row
  locks of the open transaction; the next run then proceeds;
- a second worker started while the first holds the lock skips (exit 0);
- a dropped database connection mid-run (pg_terminate_backend) turns into a
  non-zero exit with one ``status=error`` summary line, never a silent success;
- the CLI exit codes and summary line that alerting depends on.

Each test uses its own schema for rows. The advisory lock is database-wide,
so the job processes run with ``search_path`` set to that schema and an
``application_name`` unique to the test, which is how their server sessions
are found and terminated.
"""

import asyncio
import io
import os
import re
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_SECRET = os.environ.get("SECRET_KEY") or "test-only-signing-key-0123456789abcdef"
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": TEST_SECRET,
}):
    import booking_expiry
    import idempotency
    import lifecycle
    from jobs import expire_bookings, purge_idempotency_keys
    from jobs.locking import JOB_LOCK_IDS, JOB_LOCK_NAMESPACE
    from models import Base, Booking, BookingStatus, Ride, User, UserRole

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_OK = re.compile(
    r"^job=expire_bookings status=ok expired=(\d+) closed=(\d+) skipped_locked=(\d+) duration_ms=\d+$")
SUMMARY_ERROR = re.compile(r"^job=(\w+) status=error error=(\w+) duration_ms=\d+$")
SUMMARY_SKIPPED = re.compile(r"^job=(\w+) status=skipped duration_ms=\d+$")

# Child process: runs ``jobs.expire_bookings.main()`` on the test schema. In
# "pause" mode it stops inside the second ride's transaction (the first ride is
# already committed, the second ride's row lock is held and uncommitted), prints
# PAUSED and waits (blocking, up to 30 s) for the parent to create the release
# file, so the parent can kill it or cut its connection there first.
CHILD = r"""
import os, sys, time
from sqlalchemy.ext.asyncio import create_async_engine
import database, lifecycle
from jobs import expire_bookings

url, schema, app_name, mode, release = sys.argv[1:6]
database.engine = create_async_engine(url, connect_args={"server_settings": {
    "search_path": schema, "application_name": app_name}})
real = lifecycle.apply_booking_transition
calls = {"n": 0}

def pausing(ride, booking, action):
    calls["n"] += 1
    if mode == "pause" and calls["n"] == 2:
        print("PAUSED", flush=True)
        deadline = time.monotonic() + 30
        while not os.path.exists(release) and time.monotonic() < deadline:
            time.sleep(0.05)
    return real(ride, booking, action)

lifecycle.apply_booking_transition = pausing
sys.exit(expire_bookings.main([]))
"""


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class JobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_jobrec_" + uuid4().hex
        self.app_name = "rsjob-" + uuid4().hex[:12]
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=5,
                                          connect_args={"server_settings": {
                                              "search_path": self.schema,
                                              "statement_timeout": "20000"}})
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        self.children = []
        self.release = Path(f"/tmp/{self.app_name}.release")
        self.addCleanup(lambda: self.release.unlink(missing_ok=True))
        self.addAsyncCleanup(self.reap_children)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = :app"), {"app": self.app_name})
            await conn.execute(DropSchema(self.schema, cascade=True))

    async def reap_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            await asyncio.to_thread(child.wait, 30)
            for stream in (child.stdout, child.stderr):
                if stream:
                    stream.close()

    # --- fixtures and probes -------------------------------------------------

    async def seed_rides_with_due_requests(self, count=3):
        """``count`` future rides (3 seats each), each with one overdue pending request."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            driver = User(name="fixture-driver", email=f"d-{uuid4().hex[:8]}@example.com",
                          cnic=f"fixture-cnic-{uuid4().hex[:8]}", password="unused",
                          phone_number="000", role=UserRole.driver)
            riders = [User(name=f"fixture-rider-{i}", email=f"p{i}-{uuid4().hex[:8]}@example.com",
                           cnic=f"fixture-cnic-{uuid4().hex[:8]}", password="unused",
                           phone_number="000", role=UserRole.passenger) for i in range(count)]
            db.add_all([driver, *riders])
            await db.flush()
            rides = [Ride(driver_id=driver.id, origin="Lahore", destination="Islamabad",
                          departure_time=now + timedelta(days=1, minutes=i), available_seats=3,
                          fare_per_seat=1500) for i in range(count)]
            db.add_all(rides)
            await db.flush()
            bookings = [Booking(ride_id=ride.id, passenger_id=rider.id,
                                status=BookingStatus.pending,
                                expires_at=now - timedelta(minutes=1))
                        for ride, rider in zip(rides, riders)]
            db.add_all(bookings)
            await db.commit()
            # expire_due_requests walks rides in id order: index 0 is done first.
            return [b.id for b in sorted(bookings, key=lambda b: b.ride_id)]

    async def statuses(self, booking_ids):
        async with AsyncSession(self.engine) as db:
            rows = (await db.execute(select(Booking.id, Booking.status)
                                     .where(Booking.id.in_(booking_ids)))).all()
        by_id = dict(rows)
        return [by_id[i] for i in booking_ids]

    async def assert_rides_consistent(self):
        """Expiring a pending request never moves seats; every ride is still active with 3."""
        async with AsyncSession(self.engine) as db:
            rows = (await db.execute(select(Ride.available_seats, Ride.status))).all()
            live_per_ride = (await db.execute(text(
                "SELECT ride_id, count(*) FROM bookings WHERE status IN ('pending','accepted') "
                "GROUP BY ride_id HAVING count(*) > 1"))).all()
        self.assertTrue(rows)
        for seats, status in rows:
            self.assertEqual(seats, 3)
            self.assertEqual(status.value if hasattr(status, "value") else status, "active")
        self.assertEqual(live_per_ride, [])

    async def lock_holder_pids(self, job="expire_bookings"):
        async with self.engine.connect() as conn:
            return list((await conn.scalars(text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND classid::bigint = :ns AND objid::bigint = :job"),
                {"ns": JOB_LOCK_NAMESPACE, "job": JOB_LOCK_IDS[job]})).all())

    async def child_pids(self):
        async with self.engine.connect() as conn:
            return list((await conn.scalars(text(
                "SELECT pid FROM pg_stat_activity WHERE application_name = :app"),
                {"app": self.app_name})).all())

    async def wait_until(self, probe, timeout=10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            value = await probe()
            if value:
                return value
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"condition not reached within {timeout}s")
            await asyncio.sleep(0.05)

    def env(self):
        return {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "SECRET_KEY": TEST_SECRET,
                "PYTHONDONTWRITEBYTECODE": "1"}

    def spawn(self, mode):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", CHILD, TEST_DATABASE_URL, self.schema, self.app_name, mode,
             str(self.release)],
            cwd=ROOT, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    async def wait_paused(self, child):
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 30)
        if line.strip() != "PAUSED":
            child.kill()
            self.fail(f"child did not pause: {line!r} {child.stderr.read()}")

    async def finish(self, child):
        self.release.touch()
        out, err = await asyncio.to_thread(child.communicate, timeout=60)
        return child.returncode, out.strip().splitlines(), err

    def run_cli_in_test_schema(self, mode="plain"):
        child = subprocess.run(
            [sys.executable, "-B", "-c", CHILD, TEST_DATABASE_URL, self.schema, self.app_name, mode,
             str(self.release)],
            cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=60)
        return child.returncode, child.stdout.strip().splitlines(), child.stderr

    # --- exception mid-batch ---------------------------------------------------

    async def test_exception_mid_batch_keeps_committed_rides_and_rerun_finishes(self):
        ids = await self.seed_rides_with_due_requests(3)
        real = lifecycle.apply_booking_transition
        calls = {"n": 0}

        def fail_on_second(ride, booking, action):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash on the second ride")
            return real(ride, booking, action)

        with patch.object(lifecycle, "apply_booking_transition", fail_on_second):
            with self.assertRaises(RuntimeError):
                await expire_bookings.run(100, engine=self.engine)

        self.assertEqual(await self.lock_holder_pids(), [])  # released on the error path
        self.assertEqual(await self.statuses(ids),
                         [BookingStatus.expired, BookingStatus.pending, BookingStatus.pending])
        await self.assert_rides_consistent()

        retry = await expire_bookings.run(100, engine=self.engine)
        self.assertEqual(retry.expired_requests, 2)  # only what the failed run did not commit
        self.assertEqual(await self.statuses(ids), [BookingStatus.expired] * 3)
        again = await expire_bookings.run(100, engine=self.engine)
        self.assertEqual((again.expired_requests, again.rides_completed, again.rides_cancelled),
                         (0, 0, 0))
        await self.assert_rides_consistent()
        self.assertEqual(await self.lock_holder_pids(), [])

    # --- SIGKILL mid-batch --------------------------------------------------------

    async def test_sigkill_mid_batch_releases_lock_with_the_session_and_rerun_recovers(self):
        ids = await self.seed_rides_with_due_requests(3)
        child = self.spawn("pause")
        await self.wait_paused(child)

        holders = await self.lock_holder_pids()
        child_pids = await self.child_pids()
        self.assertEqual(len(holders), 1)
        self.assertIn(holders[0], child_pids)  # the lock belongs to the job's own session

        # A second worker (in-process and as a CLI) skips while the first runs.
        self.assertIsNone(await expire_bookings.run(100, engine=self.engine))
        code, lines, _ = await asyncio.to_thread(self.run_cli_in_test_schema)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "job_lock=skipped")
        self.assertRegex(lines[-1], SUMMARY_SKIPPED)
        self.assertEqual(await self.statuses(ids),
                         [BookingStatus.expired, BookingStatus.pending, BookingStatus.pending])

        os.kill(child.pid, signal.SIGKILL)  # no finally, no unlock, no rollback from Python
        await asyncio.to_thread(child.wait, 30)
        self.assertEqual(child.returncode, -signal.SIGKILL)

        # PostgreSQL ends the sessions when their sockets close: lock and row locks go with them.
        async def gone():
            return not await self.child_pids() and not await self.lock_holder_pids()
        await self.wait_until(gone)

        self.assertEqual(await self.statuses(ids),
                         [BookingStatus.expired, BookingStatus.pending, BookingStatus.pending])
        await self.assert_rides_consistent()
        async with self.engine.begin() as conn:  # the killed txn's row lock is gone too
            await conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            await conn.execute(text("SELECT id FROM rides FOR UPDATE NOWAIT"))

        code, lines, err = await asyncio.to_thread(self.run_cli_in_test_schema)
        self.assertEqual(code, 0, err)
        match = SUMMARY_OK.match(lines[-1])
        self.assertIsNotNone(match, lines)
        self.assertEqual(match.groups(), ("2", "0", "0"))
        self.assertEqual(await self.statuses(ids), [BookingStatus.expired] * 3)

        code, lines, err = await asyncio.to_thread(self.run_cli_in_test_schema)
        self.assertEqual(code, 0, err)
        self.assertEqual(SUMMARY_OK.match(lines[-1]).groups(), ("0", "0", "0"))
        await self.assert_rides_consistent()

    # --- connection dropped mid-run ----------------------------------------------

    async def test_work_connection_dropped_mid_run_exits_non_zero_and_releases_lock(self):
        ids = await self.seed_rides_with_due_requests(3)
        child = self.spawn("pause")
        await self.wait_paused(child)
        holders = await self.lock_holder_pids()
        workers = [pid for pid in await self.child_pids() if pid not in holders]
        self.assertEqual(len(holders), 1)
        self.assertEqual(len(workers), 1)
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": workers[0]})

        code, lines, err = await self.finish(child)
        self.assertEqual(code, 1, (lines, err))
        match = SUMMARY_ERROR.match(lines[-1])
        self.assertIsNotNone(match, lines)
        self.assertEqual(match.group(1), "expire_bookings")
        self.assertIn("Traceback", err)
        self.assertNotIn("job_lock=acquired", lines)  # no success counters on failure

        await self.wait_until(lambda: self._no_lock())
        self.assertEqual(await self.statuses(ids),
                         [BookingStatus.expired, BookingStatus.pending, BookingStatus.pending])
        await self.assert_rides_consistent()

        retry = await expire_bookings.run(100, engine=self.engine)
        self.assertEqual(retry.expired_requests, 2)
        self.assertEqual(await self.statuses(ids), [BookingStatus.expired] * 3)

    async def test_lock_connection_dropped_mid_run_is_reported_as_failure(self):
        """Losing the lock session means another runner may have started: never report ok."""
        ids = await self.seed_rides_with_due_requests(3)
        child = self.spawn("pause")
        await self.wait_paused(child)
        holders = await self.lock_holder_pids()
        self.assertEqual(len(holders), 1)
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": holders[0]})
        # The lock is free as soon as its session ends, even though the job still runs.
        await self.wait_until(lambda: self._no_lock())

        code, lines, err = await self.finish(child)
        self.assertEqual(code, 1, (lines, err))
        self.assertRegex(lines[-1], SUMMARY_ERROR)
        # The work itself was row-safe and finished; the next run has nothing to do.
        self.assertEqual(await self.statuses(ids), [BookingStatus.expired] * 3)
        again = await expire_bookings.run(100, engine=self.engine)
        self.assertEqual(again.expired_requests, 0)
        await self.assert_rides_consistent()

    async def _no_lock(self):
        return not await self.lock_holder_pids()

    # --- exit codes and summary line ---------------------------------------------------

    async def test_cli_success_prints_summary_and_exits_zero(self):
        await self.seed_rides_with_due_requests(2)
        code, lines, err = await asyncio.to_thread(self.run_cli_in_test_schema)
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[0], "job_lock=acquired")
        self.assertIn("expired_requests=2", lines)
        self.assertEqual(SUMMARY_OK.match(lines[-1]).groups(), ("2", "0", "0"))


class ExitCodeTests(unittest.TestCase):
    """main() turns any exception into exit 1 plus one status=error line (no message text)."""

    def run_main(self, module, target, name):
        async def broken(*args, **kwargs):
            raise RuntimeError("row value alice@example.invalid must never be logged")

        out, err = io.StringIO(), io.StringIO()
        with patch.object(target, name, broken), redirect_stdout(out), redirect_stderr(err):
            code = module.main([])
        return code, out.getvalue().strip().splitlines(), err.getvalue()

    @unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
    def test_expire_failure_exits_one_with_error_summary(self):
        code, lines, err = self.run_main(expire_bookings, booking_expiry, "sweep")
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(SUMMARY_ERROR.match(lines[0]).groups(), ("expire_bookings", "RuntimeError"))
        self.assertNotIn("alice@example.invalid", lines[0])
        self.assertIn("RuntimeError", err)

    @unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
    def test_purge_failure_exits_one_with_error_summary(self):
        code, lines, err = self.run_main(purge_idempotency_keys, idempotency, "purge_expired")
        self.assertEqual(code, 1)
        self.assertEqual(SUMMARY_ERROR.match(lines[-1]).groups(),
                         ("purge_idempotency_keys", "RuntimeError"))
        self.assertNotIn("alice@example.invalid", "\n".join(lines))

    def test_unreachable_database_exits_one_not_zero(self):
        env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://nobody:x@127.0.0.1:1/none",
               "SECRET_KEY": TEST_SECRET}
        for module, job in (("jobs.expire_bookings", "expire_bookings"),
                            ("jobs.purge_idempotency_keys", "purge_idempotency_keys")):
            result = subprocess.run([sys.executable, "-B", "-m", module], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, result.stderr)
            lines = result.stdout.strip().splitlines()
            self.assertEqual(SUMMARY_ERROR.match(lines[-1]).group(1), job)

    def test_bad_arguments_exit_two(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            expire_bookings.main(["--batch-size", "0"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
