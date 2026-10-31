"""Concurrency stress tests against real PostgreSQL (RC-stress).

Every test runs in its own schema built from the models, plus test-only
tracking objects (never part of the app schema):

- ``stress_capacity``: each ride's seats at creation, captured by an AFTER
  INSERT trigger on ``rides`` (the app has no total-seats column);
- ``stress_history``: one row per committed insert/status change on rides,
  bookings and users.account_status, with ``txid_current()`` and
  ``clock_timestamp()``. Rows written by a transaction that rolls back vanish
  with it, so the table is the committed history;
- ``stress_events``: instants at which a test moved the frozen application
  clock past a ride's departure.

After EVERY test ``asyncTearDown`` runs ``assert_invariants``: pure SQL checks
of seat bounds and conservation, lifecycle terminal states, the partial unique
index, the history (no acceptance/creation after cancellation, completion or
departure; nothing created by a user after their suspension committed),
idempotency rows vs bookings, audit-row counts vs admin actions performed, and
token_version vs suspend/reinstate audit rows. HTTP status codes are recorded
but are never the invariant.

Interleavings are FORCED where possible: a transaction is paused immediately
before COMMIT (holding its row locks) by patching that session's ``commit``,
or a coordinator connection holds a row lock, and the competing operations are
started only once ``pg_stat_activity`` shows the expected number of backends
waiting on a lock. Tests whose docstring says "probabilistic" rely on
asyncio.gather over HTTP (httpx ASGI client, one pooled connection per
request) and are seeded: STRESS_SEED (default random, printed) and
STRESS_ROUNDS (default 6). STRESS_REPORT=<path> appends one JSON line per test.

Fixture identities are synthetic: no real CNIC, phone or personal data.
"""

import asyncio
import json
import os
import random
import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
ROUNDS = int(os.environ.get("STRESS_ROUNDS", "6"))
SEED = int(os.environ.get("STRESS_SEED") or random.SystemRandom().randrange(2**31))
REPORT = os.environ.get("STRESS_REPORT")

with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "stress-tests-only-not-a-production-key",
}):
    import booking_expiry
    import clock
    import idempotency
    import lifecycle
    from database import Base, get_db
    from main import app
    from models import Booking, BookingStatus, IdempotencyKey, Ride, RideStatus, User, UserRole
    from routers import admin as admin_routes
    from routers import bookings as booking_routes
    from routers.admin import AccountStatusChange
    from routers.auth import create_access_token
    from services import bookings as booking_service
    from services import rides as ride_service

B, R = BookingStatus, RideStatus
REASON = AccountStatusChange(reason="Stress test: repeated no-shows reported")
FAKE_HASH = "$2b$12$" + "x" * 53  # never used to log in
print(f"[stress] STRESS_SEED={SEED} STRESS_ROUNDS={ROUNDS}", file=sys.stderr)

TRACKING_DDL = [
    "CREATE TABLE stress_capacity (ride_id int PRIMARY KEY, seats int NOT NULL)",
    """CREATE TABLE stress_history (
        seq bigserial PRIMARY KEY, tbl text NOT NULL, row_id int NOT NULL, op text NOT NULL,
        old_status text, new_status text, ride_id int, user_id int, seats int,
        txid bigint NOT NULL DEFAULT txid_current(),
        at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    """CREATE TABLE stress_events (
        id serial PRIMARY KEY, kind text NOT NULL, ride_id int NOT NULL,
        at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    """CREATE FUNCTION stress_track() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'rides' THEN
        IF TG_OP = 'INSERT' THEN
          INSERT INTO stress_capacity VALUES (NEW.id, NEW.available_seats);
          INSERT INTO stress_history (tbl, row_id, op, new_status, ride_id, user_id)
            VALUES ('rides', NEW.id, 'INSERT', NEW.status::text, NEW.id, NEW.driver_id);
        ELSIF OLD.status IS DISTINCT FROM NEW.status THEN
          INSERT INTO stress_history (tbl, row_id, op, old_status, new_status, ride_id, user_id)
            VALUES ('rides', NEW.id, 'UPDATE', OLD.status::text, NEW.status::text, NEW.id, NEW.driver_id);
        END IF;
      ELSIF TG_TABLE_NAME = 'bookings' THEN
        IF TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status THEN
          INSERT INTO stress_history (tbl, row_id, op, old_status, new_status, ride_id, user_id, seats)
            VALUES ('bookings', NEW.id, TG_OP,
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD.status::text END,
                    NEW.status::text, NEW.ride_id, NEW.passenger_id, NEW.seats_requested);
        END IF;
      ELSIF TG_TABLE_NAME = 'users' THEN
        IF TG_OP = 'UPDATE' AND OLD.account_status IS DISTINCT FROM NEW.account_status THEN
          INSERT INTO stress_history (tbl, row_id, op, old_status, new_status, user_id)
            VALUES ('users', NEW.id, 'UPDATE', OLD.account_status, NEW.account_status, NEW.id);
        END IF;
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER stress_rides AFTER INSERT OR UPDATE ON rides FOR EACH ROW EXECUTE FUNCTION stress_track()",
    "CREATE TRIGGER stress_bookings AFTER INSERT OR UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION stress_track()",
    "CREATE TRIGGER stress_users AFTER UPDATE ON users FOR EACH ROW EXECUTE FUNCTION stress_track()",
]

# Each query returns offending rows; every one must return none.
INVARIANT_QUERIES = {
    "ride without captured capacity":
        "SELECT r.id FROM rides r LEFT JOIN stress_capacity c ON c.ride_id = r.id WHERE c.ride_id IS NULL",
    "0 <= available_seats <= seats":
        "SELECT r.id, r.available_seats, c.seats FROM rides r JOIN stress_capacity c ON c.ride_id = r.id "
        "WHERE r.available_seats < 0 OR r.available_seats > c.seats",
    "sum(accepted seats) + available = seats (all statuses)":
        "SELECT r.id, r.status, r.available_seats, c.seats, "
        "COALESCE(SUM(b.seats_requested) FILTER (WHERE b.status = 'accepted'), 0) AS accepted "
        "FROM rides r JOIN stress_capacity c ON c.ride_id = r.id LEFT JOIN bookings b ON b.ride_id = r.id "
        "GROUP BY r.id, c.seats HAVING COALESCE(SUM(b.seats_requested) FILTER "
        "(WHERE b.status = 'accepted'), 0) + r.available_seats <> c.seats",
    "cancelled ride has every seat back and no live booking":
        "SELECT r.id FROM rides r JOIN stress_capacity c ON c.ride_id = r.id WHERE r.status = 'cancelled' "
        "AND (r.available_seats <> c.seats OR EXISTS (SELECT 1 FROM bookings b WHERE b.ride_id = r.id "
        "AND b.status IN ('pending', 'accepted')))",
    "completed ride has no pending booking":
        "SELECT r.id FROM rides r WHERE r.status = 'completed' AND EXISTS (SELECT 1 FROM bookings b "
        "WHERE b.ride_id = r.id AND b.status = 'pending')",
    "duplicate live booking per (ride, passenger)":
        "SELECT ride_id, passenger_id, count(*) FROM bookings WHERE status IN ('pending', 'accepted') "
        "GROUP BY ride_id, passenger_id HAVING count(*) > 1",
    "partial unique index missing or invalid":
        "SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'uq_bookings_active_passenger_ride' AND c.relnamespace = current_schema()::regnamespace "
        "AND i.indisunique AND i.indisvalid AND pg_get_expr(i.indpred, i.indrelid) LIKE '%pending%accepted%')",
    "seats_requested out of CHECK range":
        "SELECT id FROM bookings WHERE seats_requested < 1 OR seats_requested > 8",
    "booking accepted or created after its ride was cancelled/completed":
        "SELECT h.* FROM stress_history h JOIN stress_history rc ON rc.tbl = 'rides' AND rc.row_id = h.ride_id "
        "AND rc.op = 'UPDATE' AND rc.new_status IN ('cancelled', 'completed') "
        "WHERE h.tbl = 'bookings' AND (h.new_status = 'accepted' OR h.op = 'INSERT') AND h.at > rc.at",
    "booking accepted or created after the ride departed":
        "SELECT h.* FROM stress_history h JOIN stress_events e ON e.kind = 'departed' AND e.ride_id = h.ride_id "
        "WHERE h.tbl = 'bookings' AND (h.new_status = 'accepted' OR h.op = 'INSERT') AND h.at > e.at",
    "accepted seat handed back after departure by a passenger cancel or a suspension":
        "SELECT h.* FROM stress_history h JOIN stress_events e ON e.kind = 'departed' AND e.ride_id = h.ride_id "
        "WHERE h.tbl = 'bookings' AND h.old_status = 'accepted' AND h.new_status = 'cancelled' AND h.at > e.at "
        "AND (NOT EXISTS (SELECT 1 FROM stress_history rc WHERE rc.tbl = 'rides' AND rc.txid = h.txid "
        "AND rc.row_id = h.ride_id AND rc.new_status = 'cancelled') "
        " OR EXISTS (SELECT 1 FROM stress_history s WHERE s.tbl = 'users' AND s.txid = h.txid))",
    "ride cancelled by a suspension after it departed":
        "SELECT h.* FROM stress_history h JOIN stress_events e ON e.kind = 'departed' AND e.ride_id = h.row_id "
        "WHERE h.tbl = 'rides' AND h.new_status = 'cancelled' AND h.at > e.at "
        "AND EXISTS (SELECT 1 FROM stress_history s WHERE s.tbl = 'users' AND s.txid = h.txid)",
    "ride or booking created by a user after their suspension committed":
        "WITH s AS (SELECT user_id, new_status, at, LEAD(at) OVER (PARTITION BY user_id ORDER BY seq) AS until "
        "FROM stress_history WHERE tbl = 'users') "
        "SELECT h.* FROM stress_history h JOIN s ON s.user_id = h.user_id AND s.new_status = 'suspended' "
        "WHERE h.op = 'INSERT' AND h.tbl IN ('rides', 'bookings') AND h.at > s.at "
        "AND (s.until IS NULL OR h.at < s.until)",
    "suspended user still holds a live booking on an active future ride":
        "SELECT b.id FROM bookings b JOIN users u ON u.id = b.passenger_id JOIN rides r ON r.id = b.ride_id "
        "WHERE u.account_status = 'suspended' AND b.status IN ('pending', 'accepted') "
        "AND r.status = 'active' AND r.departure_time > :now",
    "suspended driver still has an active future ride":
        "SELECT r.id FROM rides r JOIN users u ON u.id = r.driver_id WHERE u.account_status = 'suspended' "
        "AND r.status = 'active' AND r.departure_time > :now",
    "token_version != number of suspend/reinstate audit rows":
        "SELECT u.id, u.token_version, count(a.id) FROM users u LEFT JOIN admin_audit_log a "
        "ON a.target_id = u.id AND a.target_type = 'user' AND a.action IN ('user.suspend', 'user.reinstate') "
        "GROUP BY u.id HAVING u.token_version <> count(a.id)",
    "account_status disagrees with the latest audit action":
        "SELECT u.id FROM users u JOIN LATERAL (SELECT action FROM admin_audit_log a WHERE a.target_id = u.id "
        "AND a.action IN ('user.suspend', 'user.reinstate') ORDER BY a.id DESC LIMIT 1) last ON true "
        "WHERE (last.action = 'user.suspend') <> (u.account_status = 'suspended')",
    "idempotency row committed without a stored response":
        "SELECT user_id, key FROM idempotency_keys WHERE status_code IS NULL OR response_body IS NULL",
    "booking.create key without its booking (same passenger, ride and seats)":
        "SELECT k.user_id, k.key FROM idempotency_keys k WHERE k.operation = 'booking.create' "
        "AND k.expires_at > :now AND NOT EXISTS (SELECT 1 FROM bookings b "
        "WHERE b.id = (k.response_body->>'id')::int AND b.passenger_id = k.user_id "
        "AND b.ride_id = (k.response_body->>'ride_id')::int "
        "AND b.seats_requested = (k.response_body->>'seats_requested')::int "
        "AND k.status_code = 201)",
    "booking created by two different keys":
        "SELECT response_body->>'id' FROM idempotency_keys WHERE operation = 'booking.create' "
        "GROUP BY response_body->>'id' HAVING count(*) > 1",
    "booking.respond key whose transition never committed (or not the driver)":
        "SELECT k.user_id, k.key FROM idempotency_keys k WHERE k.operation = 'booking.respond' "
        "AND k.expires_at > :now AND NOT EXISTS (SELECT 1 FROM bookings b JOIN rides r ON r.id = b.ride_id "
        "JOIN stress_history h ON h.tbl = 'bookings' AND h.row_id = b.id AND h.old_status = 'pending' "
        "AND h.new_status = k.response_body->>'status' "
        "WHERE b.id = (k.response_body->>'id')::int AND r.driver_id = k.user_id AND k.status_code = 200)",
    "audit-log immutability triggers missing":
        "SELECT 1 WHERE (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relname = 'admin_audit_log' AND c.relnamespace = current_schema()::regnamespace "
        "AND NOT t.tgisinternal AND t.tgname LIKE 'admin_audit_log_no_%') <> 2",
}


class FrozenClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def outcome(result):
    """Normalise a service/router result or HTTPException to a comparable tuple."""
    if isinstance(result, HTTPException):
        return (result.status_code, result.detail)
    if isinstance(result, JSONResponse):
        return (result.status_code, "replay")
    if isinstance(result, BaseException):
        raise result
    return (200, "ok")


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class StressBase(unittest.IsolatedAsyncioTestCase):
    MODE = "forced"
    PASSENGERS = 12
    DRIVERS = 4

    async def asyncSetUp(self):
        self.schema = f"test_stress_{uuid4().hex}"
        self.engine = self.make_engine()
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
            for statement in TRACKING_DDL:
                await conn.execute(text(statement))
        self.addAsyncCleanup(self.drop_schema)
        self.uid, self.roles = {}, {}
        people = [("admin", UserRole.admin), ("admin2", UserRole.admin)]
        people += [(f"d{i}", UserRole.driver) for i in range(self.DRIVERS)]
        people += [(f"p{i}", UserRole.passenger) for i in range(self.PASSENGERS)]
        async with self.session() as db:
            users = []
            for index, (name, role) in enumerate(people):
                user = User(name=f"Stress {name}", email=f"stress.{name}@example.test",
                            cnic=f"00000-{index:07d}-0", phone_number=f"+920000{index:06d}",
                            password=FAKE_HASH, role=role)
                db.add(user)
                users.append((name, user))
            await db.commit()
            for name, user in users:
                self.uid[name], self.roles[name] = user.id, user.role
        self.expected_audit = Counter()
        self.check_now = None
        self.outcomes = Counter()

        async def override_get_db():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def asyncTearDown(self):
        await self.assert_invariants()
        if REPORT:
            with open(REPORT, "a") as handle:
                handle.write(json.dumps({
                    "test": self.id().rsplit(".", 1)[-1], "mode": self.MODE, "seed": SEED,
                    "rounds": ROUNDS, "outcomes": {str(k): v for k, v in sorted(
                        self.outcomes.items(), key=lambda item: str(item[0]))},
                }) + "\n")

    def make_engine(self):
        return create_async_engine(TEST_DATABASE_URL, pool_size=30, max_overflow=20, connect_args={
            "server_settings": {"search_path": self.schema, "lock_timeout": "20000"},
        })

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def session(self, engine=None):
        return AsyncSession(engine or self.engine, expire_on_commit=False)

    # ----- invariants -------------------------------------------------------------------------

    async def assert_invariants(self):
        now = self.check_now or datetime.now(timezone.utc)
        failures = {}
        async with self.engine.connect() as conn:
            for name, sql in INVARIANT_QUERIES.items():
                params = {"now": now} if ":now" in sql else {}
                rows = (await conn.execute(text(sql), params)).all()
                if rows:
                    failures[name] = [tuple(row) for row in rows[:5]]
            audit = dict((await conn.execute(text(
                "SELECT action, count(*) FROM admin_audit_log GROUP BY action"))).all())
            stuck = await conn.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'"))
        if audit != {k: v for k, v in self.expected_audit.items() if v}:
            failures["audit rows != admin actions performed"] = [(audit, dict(self.expected_audit))]
        if stuck:
            failures["backends still waiting on locks"] = [stuck]
        self.assertEqual(failures, {}, f"invariant violations (STRESS_SEED={SEED})")

    # ----- fixtures ---------------------------------------------------------------------------

    async def add_ride(self, driver="d0", seats=4, departure=None, status=R.active):
        async with self.session() as db:
            ride = Ride(driver_id=self.uid[driver], origin="Lahore", destination="Islamabad",
                        departure_time=departure or datetime.now(timezone.utc) + timedelta(days=2),
                        available_seats=seats, fare_per_seat=1500, status=status)
            db.add(ride)
            await db.commit()
            return ride.id

    async def add_booking(self, ride_id, passenger, seats=1, status=B.pending, expires_at=None):
        """Insert a booking; an accepted one debits its seats in the same transaction."""
        async with self.session() as db:
            booking = Booking(ride_id=ride_id, passenger_id=self.uid[passenger], status=status,
                              seats_requested=seats, expires_at=expires_at)
            db.add(booking)
            if status == B.accepted:
                ride = await db.get(Ride, ride_id)
                ride.available_seats = ride.available_seats - seats  # fixture, not app code
            await db.commit()
            return booking.id

    async def ride_state(self, ride_id):
        async with self.session() as db:
            return tuple((await db.execute(select(Ride.status, Ride.available_seats)
                                           .where(Ride.id == ride_id))).one())

    async def booking_status(self, booking_id):
        async with self.session() as db:
            return await db.scalar(select(Booking.status).where(Booking.id == booking_id))

    async def mark_departed(self, ride_id):
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO stress_events (kind, ride_id) VALUES ('departed', :r)"),
                               {"r": ride_id})

    def freeze(self, now):
        frozen = FrozenClock(now)
        patcher = patch.object(clock, "utc_now", frozen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return frozen

    # ----- forcing helpers --------------------------------------------------------------------

    def hold_commit(self, db, fail=False):
        """Pause ``db``'s next commit (locks held) until ``release`` is set.

        With ``fail=True`` the commit raises instead, so the transaction rolls back."""
        gate = SimpleNamespace(reached=asyncio.Event(), release=asyncio.Event())
        real_commit = db.commit
        state = {"armed": True}

        async def held_commit():
            if not state["armed"]:
                return await real_commit()
            state["armed"] = False
            gate.reached.set()
            await gate.release.wait()
            if fail:
                raise RuntimeError("forced failure before commit")
            return await real_commit()

        patcher = patch.object(db, "commit", held_commit)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gate

    async def waiting_on_locks(self):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'"))

    async def wait_blocked(self, count, *tasks, timeout=10.0):
        """Return once at least ``count`` backends wait on a lock (asserting it happened)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self.waiting_on_locks() >= count:
                return
            for task in tasks:
                if task.done():
                    self.fail(f"task finished instead of blocking: {task.result()!r}")
            await asyncio.sleep(0.01)
        self.fail(f"fewer than {count} backends ended up waiting on a lock")

    async def held_lock(self, sql, params):
        """Coordinator connection holding a row lock until ``release()``."""
        conn = await self.engine.connect()
        trans = await conn.begin()
        await conn.execute(text(sql), params)

        async def release():
            await trans.commit()
            await conn.close()
        return release

    # ----- operations (each in its own session = its own connection) --------------------------

    async def op(self, fn, actor, *args, gate=None, fail=False, **kwargs):
        async with self.session() as db:
            user = await db.get(User, self.uid[actor])
            if gate is not None:
                gate.update(vars(self.hold_commit(db, fail=fail)))
            try:
                return await fn(*args, db=db, current_user=user, **kwargs)
            except HTTPException as exc:
                return exc

    async def admin(self, fn, target, actor="admin", gate=None):
        async with self.session() as db:
            admin = await db.get(User, self.uid[actor])
            if gate is not None:
                gate.update(vars(self.hold_commit(db)))
            try:
                result = await fn(change=REASON, user_id=self.uid[target], db=db, admin=admin)
            except HTTPException as exc:
                return exc
            self.expected_audit["user.suspend" if fn is admin_routes.suspend_user
                                else "user.reinstate"] += 1
            return result

    async def gated(self, make):
        """Start ``make(gate)`` and wait until it is paused before COMMIT."""
        gate = {}
        task = asyncio.create_task(make(gate))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while "reached" not in gate or not gate["reached"].is_set():
            if task.done():
                self.fail(f"gated operation finished without committing: {task.result()!r}")
            if loop.time() > deadline:
                self.fail("gated operation never reached COMMIT")
            await asyncio.sleep(0.005)
        return task, gate["release"]

    def headers(self, name, token_version=0):
        return {"Authorization": f"Bearer {create_access_token(self.uid[name], self.roles[name], token_version)}"}

    async def http(self, method, url, actor, **kwargs):
        response = await self.client.request(method, url, headers={
            **self.headers(actor), **kwargs.pop("headers", {})}, **kwargs)
        self.assertLess(response.status_code, 500, response.text)
        return response


# =================================================================================================
class SeatCapacityStressTests(StressBase):
    MODE = "probabilistic"

    async def test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity(self):
        """Probabilistic, seeded: per round a ride with 1-5 seats; 3-9 concurrent 1/2/3-seat
        requests (some duplicated); then concurrent accepts, duplicate accepts, rejects and
        passenger cancels; then concurrent cancels of accepted bookings racing new accepts."""
        drivers = [f"d{i}" for i in range(self.DRIVERS)]
        passengers = [f"p{i}" for i in range(self.PASSENGERS)]
        for round_number in range(ROUNDS * 2):
            rng = random.Random(SEED * 1000 + round_number)
            driver, capacity = rng.choice(drivers), rng.randint(1, 5)
            response = await self.http("POST", "/rides/", driver, json={
                "origin": "Lahore", "destination": "Multan", "available_seats": capacity,
                "fare_per_seat": 900,
                "departure_time": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()})
            self.assertEqual(response.status_code, 201)
            ride_id = response.json()["id"]
            riders = rng.sample(passengers, rng.randint(3, 9))
            requests = [(p, rng.choice([1, 1, 2, 2, 3])) for p in riders]
            requests += [(p, s) for p, s in rng.sample(requests, 2)]  # duplicates
            rng.shuffle(requests)
            results = await asyncio.gather(*[
                self.http("POST", f"/bookings/{ride_id}", p, json={"seats": s}) for p, s in requests])
            for r in results:
                self.outcomes[("request", r.status_code, r.json().get("detail", "ok"))] += 1
            async with self.session() as db:
                rows = (await db.execute(select(Booking.id, Booking.passenger_id).where(
                    Booking.ride_id == ride_id))).all()
            by_user = {v: k for k, v in self.uid.items()}
            actions = []
            for booking_id, passenger_id in rows:
                actions.append(("PATCH", f"/bookings/{booking_id}/respond?accept=true", driver))
                roll = rng.random()
                if roll < 0.3:
                    actions.append(("PATCH", f"/bookings/{booking_id}/respond?accept=true", driver))
                elif roll < 0.45:
                    actions.append(("PATCH", f"/bookings/{booking_id}/respond?accept=false", driver))
                elif roll < 0.65:
                    actions.append(("PATCH", f"/bookings/{booking_id}/cancel", by_user[passenger_id]))
            rng.shuffle(actions)
            for r in await asyncio.gather(*[self.http(m, u, a) for m, u, a in actions]):
                self.outcomes[("respond/cancel", r.status_code, r.json().get("detail", "ok"))] += 1
            await self.assert_invariants()
            # Phase 3: cancel accepted bookings while new requests + accepts race for the seats.
            async with self.session() as db:
                accepted = (await db.execute(select(Booking.id, Booking.passenger_id).where(
                    Booking.ride_id == ride_id, Booking.status == B.accepted))).all()
            late = [p for p in passengers if p not in riders][:2]
            phase3 = [self.http("PATCH", f"/bookings/{b}/cancel", by_user[p]) for b, p in accepted]
            phase3 += [self.http("POST", f"/bookings/{ride_id}", p, json={"seats": 1}) for p in late]
            for r in await asyncio.gather(*phase3):
                self.outcomes[("phase3", r.status_code, r.json().get("detail", "ok"))] += 1
            async with self.session() as db:
                pending = (await db.scalars(select(Booking.id).where(
                    Booking.ride_id == ride_id, Booking.status == B.pending))).all()
            for r in await asyncio.gather(*[self.http(
                    "PATCH", f"/bookings/{b}/respond?accept=true", driver) for b in pending]):
                self.outcomes[("phase3-accept", r.status_code, r.json().get("detail", "ok"))] += 1
            await self.assert_invariants()
        # The run must have exercised real contention, not only happy paths.
        self.assertTrue(any(k[1] == 409 for k in self.outcomes), self.outcomes)


# =================================================================================================
def serial_model(seats_a, start_a, order):
    """Serial reference: apply ``order`` (ops 'accept', 'pcancel', 'dcancel') one at a time
    through the real lifecycle tables. Returns (outcomes by op, final ride/booking state)."""
    ride = SimpleNamespace(status=R.active, available_seats=4)
    a = SimpleNamespace(status=B.pending, seats_requested=seats_a)
    other = SimpleNamespace(status=B.pending, seats_requested=2)
    if start_a == B.accepted:
        lifecycle.apply_booking_transition(ride, a, "accept")
    results = {}
    for name in order:
        try:
            if name == "accept":
                target = a if start_a == B.pending else other
                # Same guard as booking_service.respond_to_booking: with #17 (D7, R-1 wording) a
                # request cancelled on a still-active ride answers BOOKING_UNAVAILABLE.
                if hasattr(lifecycle, "respond_target"):
                    lifecycle.respond_target(target, ride, "accept")
                else:
                    lifecycle.booking_target(target.status, "accept")
                lifecycle.require_ride_active(ride)
                lifecycle.apply_booking_transition(ride, target, "accept")
            elif name == "pcancel":
                lifecycle.require_ride_active(ride)
                lifecycle.apply_booking_transition(ride, a, "cancel")
            else:
                lifecycle.ride_target(ride.status, "cancel")
                for booking in (a, other):
                    if booking.status in (B.pending, B.accepted):
                        lifecycle.apply_booking_transition(ride, booking, "cancel")
                ride.status = R.cancelled
            results[name] = (200, "ok")
        except HTTPException as exc:
            results[name] = (exc.status_code, exc.detail)
    return results, (ride.status, ride.available_seats, a.status, other.status)


class LifecycleRaceTests(StressBase):
    async def test_forced_accept_vs_passenger_cancel_vs_driver_cancel(self):
        """Forced: for each start state (pending/accepted), seat count (1/2) and first holder,
        the first op is paused before COMMIT holding the ride lock, the other two are started
        one by one and confirmed lock-waiting. The first holder is forced; the order between the
        two waiters follows PostgreSQL's tuple-lock queue, so the result must equal the serial
        model for one of the two orders (which one is reported)."""
        ops = ("accept", "pcancel", "dcancel")
        for start in (B.pending, B.accepted):
            for seats in (1, 2):
                for first in ops:
                    ride = await self.add_ride("d0", seats=4)
                    a = await self.add_booking(ride, "p0", seats=seats, status=start)
                    other = await self.add_booking(ride, "p1", seats=2)
                    target = a if start == B.pending else other
                    makers = {
                        "accept": lambda g, t=target: self.op(
                            booking_service.respond_to_booking, "d0", t, True, gate=g),
                        "pcancel": lambda g: self.op(booking_service.cancel_booking, "p0", a, gate=g),
                        "dcancel": lambda g: self.op(ride_service.cancel_ride, "d0", ride, gate=g),
                    }
                    rest = [o for o in ops if o != first]
                    first_task, release = await self.gated(makers[first])
                    tasks = {first: first_task}
                    for count, name in enumerate(rest, 1):
                        tasks[name] = asyncio.create_task(makers[name](None))
                        await self.wait_blocked(count, tasks[name])
                    release.set()
                    actual = {n: outcome(r) for n, r in zip(tasks, await asyncio.gather(*tasks.values()))}
                    status, available = await self.ride_state(ride)
                    final = (status, available, await self.booking_status(a), await self.booking_status(other))
                    matches = [order for order in ([first, *rest], [first, *reversed(rest)])
                               if serial_model(seats, start, order) == (actual, final)]
                    self.assertTrue(matches, (start, seats, first, actual, final))
                    self.outcomes[(start.value, seats, "->".join(matches[0]))] += 1
                    await self.assert_invariants()

    async def test_multi_seat_cancellation_restoration(self):
        """Forced + probabilistic. Forced: a passenger's 3-seat cancel paused before COMMIT while
        the driver's ride cancel waits (and the reverse); the ride ends with all 8 seats.
        Probabilistic: ROUNDS rounds of concurrent cancels (incl. duplicates) racing an accept."""
        for cancel_first in (True, False):
            ride = await self.add_ride("d1", seats=8)
            three = await self.add_booking(ride, "p0", seats=3, status=B.accepted)
            await self.add_booking(ride, "p1", seats=2, status=B.accepted)
            await self.add_booking(ride, "p2", seats=2, status=B.pending)
            if cancel_first:
                first, release = await self.gated(lambda g: self.op(
                    booking_service.cancel_booking, "p0", three, gate=g))
                second = asyncio.create_task(self.op(ride_service.cancel_ride, "d1", ride))
            else:
                first, release = await self.gated(lambda g: self.op(
                    ride_service.cancel_ride, "d1", ride, gate=g))
                second = asyncio.create_task(self.op(booking_service.cancel_booking, "p0", three))
            await self.wait_blocked(1, second)
            release.set()
            results = [outcome(r) for r in await asyncio.gather(first, second)]
            self.assertEqual(results[0], (200, "ok"))
            self.assertEqual(results[1], (200, "ok") if cancel_first else (409, lifecycle.RIDE_NOT_ACTIVE))
            self.assertEqual(await self.ride_state(ride), (R.cancelled, 8))
            self.outcomes[("forced", "cancel_first" if cancel_first else "ride_cancel_first")] += 1
        for round_number in range(ROUNDS):
            rng = random.Random(SEED * 7 + round_number)
            ride = await self.add_ride("d2", seats=8)
            accepted = [(await self.add_booking(ride, f"p{i}", seats=s, status=B.accepted), f"p{i}")
                        for i, s in enumerate((3, 2, 2))]
            pending = await self.add_booking(ride, "p3", seats=1)
            calls = [self.op(booking_service.cancel_booking, p, b) for b, p in accepted]
            calls += [self.op(booking_service.cancel_booking, accepted[0][1], accepted[0][0])]
            calls += [self.op(booking_service.respond_to_booking, "d2", pending, True)]
            rng.shuffle(calls)
            for r in await asyncio.gather(*calls):
                self.outcomes[("round",) + outcome(r)] += 1
            self.assertEqual(await self.ride_state(ride), (R.active, 7))
            await self.assert_invariants()

    async def test_driver_cancel_with_accepted_holders_racing_new_accepts(self):
        """Forced (both orders) + probabilistic ROUNDS of cancel_ride vs 3 accepts vs a
        passenger cancel over HTTP. A cancelled ride must end with every seat and no live row."""
        for accept_first in (True, False):
            ride = await self.add_ride("d0", seats=6)
            await self.add_booking(ride, "p0", seats=2, status=B.accepted)
            new = await self.add_booking(ride, "p1", seats=3)
            accept = lambda g: self.op(booking_service.respond_to_booking, "d0", new, True, gate=g)
            cancel = lambda g: self.op(ride_service.cancel_ride, "d0", ride, gate=g)
            first, release = await self.gated(accept if accept_first else cancel)
            second = asyncio.create_task((cancel if accept_first else accept)(None))
            await self.wait_blocked(1, second)
            release.set()
            results = [outcome(r) for r in await asyncio.gather(first, second)]
            self.assertEqual(results[0], (200, "ok"))
            self.assertEqual(results[1], (200, "ok") if accept_first else (409, lifecycle.ALREADY_RESPONDED))
            self.assertEqual(await self.ride_state(ride), (R.cancelled, 6))
        for round_number in range(ROUNDS):
            rng = random.Random(SEED * 13 + round_number)
            ride = await self.add_ride("d1", seats=6)
            holder = await self.add_booking(ride, "p0", seats=2, status=B.accepted)
            await self.add_booking(ride, "p1", seats=1, status=B.accepted)
            pend = [await self.add_booking(ride, f"p{i}", seats=s) for i, s in ((2, 2), (3, 1), (4, 1))]
            calls = [self.http("PATCH", f"/rides/{ride}/cancel", "d1"),
                     self.http("PATCH", f"/bookings/{holder}/cancel", "p0")]
            calls += [self.http("PATCH", f"/bookings/{b}/respond?accept=true", "d1") for b in pend]
            rng.shuffle(calls)
            for r in await asyncio.gather(*calls):
                self.outcomes[("round", r.status_code, r.json().get("detail", "ok"))] += 1
            self.assertEqual(await self.ride_state(ride), (R.cancelled, 6))
            await self.assert_invariants()


# =================================================================================================
class SuspensionRaceStressTests(StressBase):
    """Forced: each case runs twice, suspension paused before COMMIT (holding the user and ride
    locks) while the competing operation is confirmed lock-waiting, and the reverse. Between
    cases the target is reinstated (audit + token_version checked by the invariants)."""

    async def run_case(self, target, make_op, suspend_first):
        if suspend_first:
            first, release = await self.gated(
                lambda g: self.admin(admin_routes.suspend_user, target, gate=g))
            second = asyncio.create_task(make_op(None))
        else:
            first, release = await self.gated(make_op)
            second = asyncio.create_task(self.admin(admin_routes.suspend_user, target))
        await self.wait_blocked(1, second)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        suspension, operation = (first_result, second_result) if suspend_first else (second_result, first_result)
        self.assertEqual(outcome(suspension), (200, "ok"))
        await self.assert_invariants()
        reinstated = await self.admin(admin_routes.reinstate_user, target)
        self.assertEqual(outcome(reinstated), (200, "ok"))
        return outcome(operation), suspension

    async def test_driver_and_passenger_suspension_racing_book_accept_cancel(self):
        for suspend_first in (True, False):
            tag = "suspend_first" if suspend_first else "op_first"
            # 1. driver suspended vs a passenger booking 2 seats on the driver's ride
            ride = await self.add_ride("d0", seats=4)
            result, effects = await self.run_case("d0", lambda g: self.op(
                booking_service.book_ride, "p0", ride, seats=2, gate=g), suspend_first)
            self.assertEqual(result, (409, lifecycle.RIDE_NOT_ACTIVE) if suspend_first else (200, "ok"))
            self.assertEqual(await self.ride_state(ride), (R.cancelled, 4))
            self.outcomes[(tag, "driver_vs_book") + result] += 1
            # 2. driver suspended vs the driver accepting a 2-seat request
            ride = await self.add_ride("d0", seats=4)
            pending = await self.add_booking(ride, "p1", seats=2)
            result, _ = await self.run_case("d0", lambda g: self.op(
                booking_service.respond_to_booking, "d0", pending, True, gate=g), suspend_first)
            self.assertEqual(result, (409, lifecycle.ALREADY_RESPONDED) if suspend_first else (200, "ok"))
            self.assertEqual(await self.ride_state(ride), (R.cancelled, 4))
            self.outcomes[(tag, "driver_vs_accept") + result] += 1
            # 3. driver suspended vs a passenger cancelling an accepted 3-seat booking
            ride = await self.add_ride("d0", seats=4)
            held = await self.add_booking(ride, "p2", seats=3, status=B.accepted)
            result, _ = await self.run_case("d0", lambda g: self.op(
                booking_service.cancel_booking, "p2", held, gate=g), suspend_first)
            self.assertEqual(result, (409, lifecycle.RIDE_NOT_ACTIVE) if suspend_first else (200, "ok"))
            self.assertEqual(await self.ride_state(ride), (R.cancelled, 4))
            self.outcomes[(tag, "driver_vs_pcancel") + result] += 1
            # 4. passenger suspended vs the driver accepting the passenger's 2-seat request
            ride = await self.add_ride("d1", seats=4)
            pending = await self.add_booking(ride, "p3", seats=2)
            result, _ = await self.run_case("p3", lambda g: self.op(
                booking_service.respond_to_booking, "d1", pending, True, gate=g), suspend_first)
            # The suspension cancels the request while the ride stays active. Version-aware:
            # before #17 the accept reports ALREADY_RESPONDED; with #17 after R-1 (Maaz,
            # 31 Oct 2026) it reports the neutral BOOKING_UNAVAILABLE, exact text, which must not
            # reveal the suspension. (#17's first version, "Booking was cancelled by the
            # passenger", is superseded; PASSENGER_WITHDREW is now an alias of the new string.)
            withdrawn = getattr(lifecycle, "BOOKING_UNAVAILABLE", lifecycle.ALREADY_RESPONDED)
            if hasattr(lifecycle, "BOOKING_UNAVAILABLE"):
                self.assertEqual(withdrawn, "This booking is no longer available.")
            self.assertEqual(result, (409, withdrawn) if suspend_first else (200, "ok"))
            for revealing in ("suspend", "passenger", "withdr"):
                self.assertNotIn(revealing, result[1].lower())
            self.assertEqual((await self.ride_state(ride), await self.booking_status(pending)),
                             ((R.active, 4), B.cancelled))
            self.outcomes[(tag, "passenger_vs_accept") + result] += 1
            # 5. passenger suspended vs the passenger's own accepted-booking cancel
            ride = await self.add_ride("d1", seats=4)
            held = await self.add_booking(ride, "p4", seats=3, status=B.accepted)
            result, _ = await self.run_case("p4", lambda g: self.op(
                booking_service.cancel_booking, "p4", held, gate=g), suspend_first)
            self.assertEqual(result, (409, lifecycle.NOT_CANCELLABLE) if suspend_first else (200, "ok"))
            self.assertEqual(await self.ride_state(ride), (R.active, 4))
            self.outcomes[(tag, "passenger_vs_own_cancel") + result] += 1
            # 6. passenger suspended vs the passenger's keyed booking request
            ride = await self.add_ride("d1", seats=4)
            key = f"stress-suspend-{tag}"
            result, _ = await self.run_case("p5", lambda g: self.op(
                booking_routes.book_ride, "p5", ride_id=ride, body=None,
                idempotency_key=key, gate=g), suspend_first)
            self.assertEqual(result, (401, "Invalid token") if suspend_first else (200, "ok"))
            async with self.session() as db:
                keys = await db.scalar(select(IdempotencyKey.key).where(IdempotencyKey.key == key))
            # The key row survives a booking that the suspension then cancelled: it records
            # the 201 actually returned. On 401 nothing was claimed.
            self.assertEqual(keys, None if suspend_first else key)
            self.outcomes[(tag, "passenger_vs_keyed_book") + result] += 1

    async def test_concurrent_suspensions_of_one_user_apply_once(self):
        """Forced: admin's suspension paused before COMMIT, admin2's waits; one audit row."""
        ride = await self.add_ride("d2", seats=3)
        await self.add_booking(ride, "p6", seats=2, status=B.accepted)
        first, release = await self.gated(lambda g: self.admin(admin_routes.suspend_user, "d2", gate=g))
        second = asyncio.create_task(self.admin(admin_routes.suspend_user, "d2", actor="admin2"))
        await self.wait_blocked(1, second)
        release.set()
        results = [outcome(r) for r in await asyncio.gather(first, second)]
        self.assertEqual(results, [(200, "ok"), (409, "User is already suspended")])
        self.assertEqual(await self.ride_state(ride), (R.cancelled, 3))


# =================================================================================================
class IdempotencyStressTests(StressBase):
    async def test_concurrent_identical_and_conflicting_keys(self):
        """Probabilistic (seeded): per round 12 identical keyed creates, 6 same-key creates with
        different payloads, 8 identical keyed accepts and 4 same-key accept/reject mixes."""
        self.MODE = "probabilistic"
        for round_number in range(ROUNDS):
            rng = random.Random(SEED * 31 + round_number)
            ride = await self.add_ride("d0", seats=8)
            other_ride = await self.add_ride("d0", seats=8)
            passenger = f"p{round_number % self.PASSENGERS}"
            key = f"same-{round_number}-{rng.randrange(10**6)}"
            seats = rng.choice([1, 2])
            responses = await asyncio.gather(*[self.http(
                "POST", f"/bookings/{ride}", passenger, json={"seats": seats},
                headers={"Idempotency-Key": key}) for _ in range(12)])
            ids = {r.json()["id"] for r in responses}
            self.assertEqual([r.status_code for r in responses], [201] * 12)
            self.assertEqual(len(ids), 1)
            self.assertEqual(sum(r.headers.get("Idempotent-Replayed") == "true" for r in responses), 11)
            # Same key, different payloads (seat count / ride), all at once.
            mixed_key = f"mixed-{round_number}"
            other = f"p{(round_number + 1) % self.PASSENGERS}"
            variants = [(ride, 1), (ride, 2), (other_ride, 1)] * 2
            rng.shuffle(variants)
            responses = await asyncio.gather(*[self.http(
                "POST", f"/bookings/{r}", other, json={"seats": s},
                headers={"Idempotency-Key": mixed_key}) for r, s in variants])
            winners = {(r.json()["ride_id"], r.json()["seats_requested"])
                       for r in responses if r.status_code == 201}
            self.assertEqual(len(winners), 1, [r.json() for r in responses])
            winner = winners.pop()
            for (r, s), response in zip(variants, responses):
                expected = 201 if (r, s) == winner else 422
                self.assertEqual(response.status_code, expected, response.text)
                self.outcomes[("create-mixed", response.status_code)] += 1
            # Identical keyed accepts debit once; same-key accept/reject mixes: one decision.
            booking_id = ids.pop()
            responses = await asyncio.gather(*[self.http(
                "PATCH", f"/bookings/{booking_id}/respond?accept=true", "d0",
                headers={"Idempotency-Key": f"acc-{round_number}"}) for _ in range(8)])
            self.assertEqual([r.status_code for r in responses], [200] * 8)
            mix = [True, False, True, False]
            rng.shuffle(mix)
            async with self.session() as db:
                mixed_booking = await db.scalar(select(Booking.id).where(
                    Booking.ride_id == winner[0], Booking.passenger_id == self.uid[other]))
            responses = await asyncio.gather(*[self.http(
                "PATCH", f"/bookings/{mixed_booking}/respond?accept={str(a).lower()}", "d0",
                headers={"Idempotency-Key": f"mix-{round_number}"}) for a in mix])
            codes = Counter(r.status_code for r in responses)
            self.assertEqual(codes, Counter({200: 2, 422: 2}), [r.text for r in responses])
            self.outcomes[("respond-mixed", tuple(sorted(codes.items())))] += 1
            await self.assert_invariants()

    async def test_forced_waiters_replay_after_commit_and_proceed_after_rollback(self):
        """Forced: the first keyed request is paused before COMMIT; 5 duplicates (+1 with a
        different payload) are confirmed waiting on the key's primary key, then the first
        commits (all replay / 422) or fails (one waiter creates, the rest replay)."""
        for fail in (False, True):
            ride = await self.add_ride("d1", seats=8)
            key = f"forced-{fail}"
            first, release = await self.gated(lambda g: self.op(
                booking_routes.book_ride, "p0", ride_id=ride, body=None, idempotency_key=key,
                gate=g, fail=fail))
            waiters = [asyncio.create_task(self.op(
                booking_routes.book_ride, "p0", ride_id=ride, body=None, idempotency_key=key))
                for _ in range(5)]
            for count in range(1, 6):
                await self.wait_blocked(count, *waiters)
            release.set()
            first_result = await asyncio.gather(first, return_exceptions=True)
            results = [outcome(r) for r in await asyncio.gather(*waiters)]
            async with self.session() as db:
                bookings = (await db.scalars(select(Booking.id).where(Booking.ride_id == ride))).all()
            self.assertEqual(len(bookings), 1)
            if not fail:
                self.assertEqual(outcome(first_result[0]), (200, "ok"))
                self.assertEqual(results, [(201, "replay")] * 5)
            else:
                self.assertIsInstance(first_result[0], RuntimeError)
                self.assertEqual(sorted(results), sorted([(200, "ok")] + [(201, "replay")] * 4))
            self.outcomes[("first_fails" if fail else "first_commits",) + tuple(sorted(Counter(results).items()))] += 1


# =================================================================================================
class ClockRaceTests(StressBase):
    async def test_expiry_boundary_and_sweep_racing_accept(self):
        """Boundary (exact): accept at expires_at-1us succeeds; at expires_at and +1us it is 409.
        Forced A: sweep holds the ride lock waiting on a booking lock held by a coordinator,
        accept waits on the ride lock -> sweep expires first, accept 409. Forced B: accept paused
        before COMMIT, sweep skips the locked ride (SKIP LOCKED), accept wins; re-sweep no-op.
        Probabilistic: ROUNDS rounds of accept || sweep at expires_at +/- 1us."""
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        frozen = self.freeze(t0)
        ride = await self.add_ride("d0", seats=8, departure=t0 + timedelta(hours=12))
        expires = t0 + booking_expiry.PENDING_TTL
        eps = timedelta(microseconds=1)
        ride_b = await self.add_ride("d0", seats=8, departure=t0 + timedelta(hours=12))
        booking = {}
        for name in ("minus", "exact", "plus", "forcedA"):
            created = await self.op(booking_service.book_ride, f"p{len(booking)}", ride)
            self.assertEqual(created.expires_at, expires)
            booking[name] = created.id
        for name, at, expected in (("minus", expires - eps, (200, "ok")),
                                   ("exact", expires, (409, lifecycle.BOOKING_EXPIRED)),
                                   ("plus", expires + eps, (409, lifecycle.BOOKING_EXPIRED))):
            frozen.now = at
            self.assertEqual(outcome(await self.op(
                booking_service.respond_to_booking, "d0", booking[name], True)), expected, name)
        jobs = self.make_engine()
        self.addAsyncCleanup(jobs.dispose)

        async def sweep(now):
            async with self.session(jobs) as db:
                return await booking_expiry.sweep(db, now)

        # Forced A
        frozen.now = expires + eps
        release_booking = await self.held_lock(
            "SELECT 1 FROM bookings WHERE id = :b FOR UPDATE", {"b": booking["forcedA"]})
        sweeper = asyncio.create_task(sweep(expires + eps))
        await self.wait_blocked(1, sweeper)
        accept = asyncio.create_task(self.op(booking_service.respond_to_booking, "d0", booking["forcedA"], True))
        await self.wait_blocked(2, sweeper, accept)
        await release_booking()
        result = await sweeper
        self.assertEqual(outcome(await accept), (409, lifecycle.ALREADY_RESPONDED))
        self.assertEqual(await self.booking_status(booking["forcedA"]), B.expired)
        # exact, plus and forcedA: the only due requests at this point.
        self.assertEqual(result.expired_requests, 3)
        # Forced B: accept (clock just before expiry) holds the ride; the sweep skips it.
        frozen.now = t0  # created after Forced A so that sweep could not touch it
        booking["forcedB"] = (await self.op(booking_service.book_ride, "p4", ride_b)).id
        frozen.now = expires - eps
        accept, release = await self.gated(lambda g: self.op(
            booking_service.respond_to_booking, "d0", booking["forcedB"], True, gate=g))
        skipped = await sweep(expires + eps)
        self.assertEqual((skipped.expired_requests, skipped.rides_skipped_locked), (0, 1))
        release.set()
        self.assertEqual(outcome(await accept), (200, "ok"))
        self.assertEqual((await sweep(expires + eps)).expired_requests, 0)
        self.assertEqual(await self.booking_status(booking["forcedB"]), B.accepted)
        self.outcomes[("boundary+forced", "ok")] += 1
        # Probabilistic rounds
        for round_number in range(ROUNDS):
            side = random.Random(SEED * 17 + round_number).choice((-1, 1))
            frozen.now = t0
            fresh = await self.add_ride("d1", seats=4, departure=t0 + timedelta(hours=12))
            b = (await self.op(booking_service.book_ride, "p9", fresh)).id
            frozen.now = expires + side * eps
            accept_result, sweep_result = await asyncio.gather(
                self.op(booking_service.respond_to_booking, "d1", b, True), sweep(expires + side * eps))
            status = await self.booking_status(b)
            if side < 0:
                self.assertEqual((outcome(accept_result), status), ((200, "ok"), B.accepted))
                self.assertEqual(sweep_result.expired_requests, 0)
            else:
                self.assertIn(outcome(accept_result), [(409, lifecycle.BOOKING_EXPIRED),
                                                       (409, lifecycle.ALREADY_RESPONDED)])
                if status == B.pending:
                    # The sweep met the ride locked by the (failing) accept and skipped it
                    # (SKIP LOCKED, by design); the request stays lazily expired until the
                    # next run, which must persist it.
                    self.assertEqual(sweep_result.rides_skipped_locked, 1)
                    self.assertEqual((await sweep(expires + eps)).expired_requests, 1)
                    status = await self.booking_status(b)
                    self.outcomes[("round", side, "sweep_skipped_locked")] += 1
                self.assertEqual(status, B.expired)
            self.outcomes[("round", side) + outcome(accept_result)] += 1
        self.check_now = frozen.now
        await self.assert_invariants()

    async def test_departure_crossed_while_waiting_on_ride_lock(self):
        """Forced: a coordinator holds the ride lock; request, accept, accepted-cancel and
        pending-withdraw are all confirmed waiting; the frozen clock is moved to the departure
        instant (inclusive) or to 1us before it; the lock is released."""
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        frozen = self.freeze(t0)
        eps = timedelta(microseconds=1)
        for crossed in (True, False):
            departure = frozen.now + timedelta(hours=1)
            frozen.now = departure - timedelta(seconds=1)
            ride = await self.add_ride("d2", seats=6, departure=departure)
            pending = await self.add_booking(ride, "p0", seats=2, expires_at=departure)
            accepted = await self.add_booking(ride, "p1", seats=2, status=B.accepted)
            withdraw = await self.add_booking(ride, "p2", seats=1, expires_at=departure)
            release = await self.held_lock("SELECT 1 FROM rides WHERE id = :r FOR UPDATE", {"r": ride})
            tasks = {
                "request": asyncio.create_task(self.op(booking_service.book_ride, "p3", ride, seats=2)),
                "accept": asyncio.create_task(self.op(booking_service.respond_to_booking, "d2", pending, True)),
                "accepted_cancel": asyncio.create_task(self.op(booking_service.cancel_booking, "p1", accepted)),
                "withdraw": asyncio.create_task(self.op(booking_service.cancel_booking, "p2", withdraw)),
            }
            await self.wait_blocked(4, *tasks.values())
            frozen.now = departure if crossed else departure - eps
            if crossed:
                await self.mark_departed(ride)
            await release()
            results = {n: outcome(r) for n, r in zip(tasks, await asyncio.gather(*tasks.values()))}
            departed = (409, lifecycle.RIDE_DEPARTED)
            # A request/accept that waited past departure is rejected; the pending request
            # (expires_at = departure) is also expired, but the departure message wins.
            self.assertEqual(results, {
                "request": departed if crossed else (200, "ok"),
                "accept": departed if crossed else (200, "ok"),
                "accepted_cancel": departed if crossed else (200, "ok"),
                "withdraw": (200, "ok"),
            })
            self.outcomes[("crossed" if crossed else "not_crossed",) + tuple(sorted(results.items()))] += 1
            await self.assert_invariants()
            frozen.now = departure + timedelta(hours=2)
        self.check_now = frozen.now


class SuspensionClockRaceTests(StressBase):
    async def test_suspension_waiting_on_ride_lock_rechecks_departure(self):
        """Forced: a coordinator holds the ride lock; a suspension (driver, then passenger) is
        confirmed waiting on it; the frozen clock moves to the departure instant; the lock is
        released. A ride that departed while the suspension waited is a departed ride: it is
        not cancelled and an accepted seat on it is not handed back (BOOKING_LIFECYCLE §3:
        time guards are evaluated after the locks).

        Regression test for the RC-stress product fix in services/accounts.py: suspend_user
        captured ``now`` before waiting for the ride locks, so it cancelled a ride (and handed
        back accepted seats) that had departed while it waited."""
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        frozen = self.freeze(t0)
        for target, driver, passenger in (("d0", "d0", "p0"), ("p1", "d1", "p1")):
            departure = frozen.now + timedelta(hours=1)
            frozen.now = departure - timedelta(seconds=1)
            ride = await self.add_ride(driver, seats=4, departure=departure)
            held = await self.add_booking(ride, passenger, seats=2, status=B.accepted)
            release = await self.held_lock("SELECT 1 FROM rides WHERE id = :r FOR UPDATE", {"r": ride})
            suspension = asyncio.create_task(self.admin(admin_routes.suspend_user, target))
            await self.wait_blocked(1, suspension)
            frozen.now = departure
            await self.mark_departed(ride)
            await release()
            response = await suspension
            self.assertEqual(outcome(response), (200, "ok"))
            self.assertEqual((await self.ride_state(ride), await self.booking_status(held)),
                             ((R.active, 2), B.accepted), target)
            if target == driver:
                self.assertEqual((response.cancelled_ride_ids, response.departed_active_ride_ids),
                                 ([], [ride]))
            self.assertEqual(response.cancelled_booking_ids, [])
            self.check_now = frozen.now
            await self.assert_invariants()
            frozen.now = departure + timedelta(hours=2)


# =================================================================================================
class JobConcurrencyTests(StressBase):
    async def test_expire_sweep_and_purge_twice_concurrently_forced(self):
        """Forced: sweep A paused before its first COMMIT while sweep B runs to the end; purge A
        paused before its first batch COMMIT while purge B runs (see run_jobs_twice)."""
        await self.run_jobs_twice(forced=True)

    async def test_expire_sweep_and_purge_twice_concurrently_free_running(self):
        """Probabilistic: two sweeps and two purges started together with asyncio.gather."""
        self.MODE = "probabilistic"
        await self.run_jobs_twice(forced=False)

    async def run_jobs_twice(self, forced):
        """Two sweeps and two purges run at once, each on its own engine (own connection pool),
        twice: forced (sweep A paused before its first COMMIT while B runs; purge A paused
        before its first batch COMMIT while B runs) and free-running. Totals must equal the
        single-run expectation exactly; a third run changes nothing."""
        for _ in (1,):
            now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)
            expect = Counter()
            # 6 future rides with 2 due pending + 1 live pending each (ride lock contention).
            for i in range(6):
                ride = await self.add_ride(f"d{i % self.DRIVERS}", seats=6, departure=now + timedelta(hours=5))
                for j in range(2):
                    await self.add_booking(ride, f"p{j}", expires_at=now - timedelta(minutes=1 + j))
                    expect["expired_requests"] += 1
                await self.add_booking(ride, "p2", expires_at=now + timedelta(minutes=5))
            # 4 stale rides: two with an accepted booking (completed), two without (cancelled).
            for i in range(4):
                ride = await self.add_ride("d3", seats=4, departure=now - timedelta(hours=48))
                await self.add_booking(ride, "p3")
                expect["expired_requests"] += 1
                if i % 2 == 0:
                    await self.add_booking(ride, "p4", seats=2, status=B.accepted)
                    expect["rides_completed"] += 1
                else:
                    expect["rides_cancelled"] += 1
            # Idempotency keys: 23 expired (no booking needed: purged) and 3 live ones.
            async with self.session() as db:
                for k in range(23):
                    db.add(IdempotencyKey(
                        user_id=self.uid[f"p{k % 5}"], key=f"old-{forced}-{k}", operation="booking.create",
                        request_hash="0" * 64, status_code=201, response_body={"id": -k},
                        created_at=now - timedelta(days=2), expires_at=now - timedelta(seconds=k + 1)))
                live = await self.add_ride("d0", seats=3, departure=now + timedelta(days=1))
                for k in range(3):
                    booking = Booking(ride_id=live, passenger_id=self.uid[f"p{6 + k}"], status=B.pending,
                                      expires_at=now + timedelta(hours=1))
                    db.add(booking)
                    await db.flush()
                    db.add(IdempotencyKey(
                        user_id=booking.passenger_id, key=f"live-{forced}-{k}", operation="booking.create",
                        request_hash="1" * 64, status_code=201,
                        response_body={"id": booking.id, "ride_id": live, "passenger_id": booking.passenger_id,
                                       "status": "pending", "seats_requested": 1},
                        created_at=now, expires_at=now + timedelta(hours=1)))
                await db.commit()
            engines = [self.make_engine() for _ in range(4)]
            for engine in engines:
                self.addAsyncCleanup(engine.dispose)

            async def sweep(engine, gate=None):
                async with self.session(engine) as db:
                    if gate is not None:
                        gate.update(vars(self.hold_commit(db)))
                    return await booking_expiry.sweep(db, now, batch_size=3)

            async def purge(engine, gate=None):
                async with self.session(engine) as db:
                    if gate is not None:
                        gate.update(vars(self.hold_commit(db)))
                    return await idempotency.purge_expired(db, now=now, batch_size=7)

            if forced:
                sweep_a, release_sweep = await self.gated(lambda g: sweep(engines[0], g))
                sweep_b = await sweep(engines[1])
                purge_a, release_purge = await self.gated(lambda g: purge(engines[2], g))
                purge_b = await purge(engines[3])
                release_sweep.set()
                release_purge.set()
                sweep_a, purge_a = await sweep_a, await purge_a
                self.assertGreaterEqual(sweep_b.rides_skipped_locked, 1)
            else:
                sweep_a, sweep_b, purge_a, purge_b = await asyncio.gather(
                    sweep(engines[0]), sweep(engines[1]), purge(engines[2]), purge(engines[3]))
            total = Counter()
            for result in (sweep_a, sweep_b):
                for name in ("expired_requests", "rides_completed", "rides_cancelled"):
                    total[name] += getattr(result, name)
            self.assertEqual(total, expect)
            self.assertEqual(purge_a + purge_b, 23)
            third = await sweep(engines[0])
            self.assertEqual((third.expired_requests, third.rides_completed, third.rides_cancelled), (0, 0, 0))
            self.assertEqual(await purge(engines[2]), 0)
            async with self.session() as db:
                remaining = (await db.scalars(select(IdempotencyKey.key).where(
                    IdempotencyKey.key.like(f"%-{forced}-%")))).all()
            self.assertEqual(sorted(remaining), [f"live-{forced}-{k}" for k in range(3)])
            self.outcomes[("forced" if forced else "free",
                           "skipped", sweep_a.rides_skipped_locked + sweep_b.rides_skipped_locked,
                           "purge_split", purge_a, purge_b)] += 1
            self.check_now = now
            await self.assert_invariants()


if __name__ == "__main__":
    unittest.main()
