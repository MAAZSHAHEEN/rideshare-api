"""Ride/booking state machine, cancellation cut-off and seat invariants (M4).

Unit tests cover every (state, action) pair of the transition tables against an
independently written expectation. PostgreSQL tests (set TEST_DATABASE_URL)
drive the same matrix through the HTTP API in an isolated schema, plus
departure-boundary and asyncio.gather concurrency cases. Fixture identities are
synthetic: no real CNIC, phone or personal data.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from itertools import product
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "lifecycle-tests-only-not-a-production-key",
}):
    import database
    import lifecycle
    from main import app
    from models import Base, Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token

B, R = BookingStatus, RideStatus
DEPARTED = "Ride has already departed"

# Written out by hand, independent of lifecycle.py, so a changed table fails.
EXPECTED_BOOKING = {
    (B.pending, "accept"): (B.accepted, -1),
    (B.pending, "reject"): (B.rejected, 0),
    (B.pending, "cancel"): (B.cancelled, 0),
    (B.accepted, "cancel"): (B.cancelled, +1),
}
# D7: a request the passenger withdrew (cancelled booking, ride still active)
# is reported distinctly to the driver; every other conflict keeps its string.
WITHDREW = "This booking is no longer available."  # R-1 neutral wording, exact
BOOKING_CONFLICT = {"accept": "Booking already responded to",
                    "reject": "Booking already responded to",
                    "cancel": "Booking cannot be cancelled"}
EXPECTED_RIDE = {(R.active, "complete"): R.completed, (R.active, "cancel"): R.cancelled}


class _Ride:
    def __init__(self, seats=2, status=R.active, departure=None):
        self.available_seats, self.status = seats, status
        self.departure_time = departure or datetime.now(timezone.utc) + timedelta(days=1)


class _Booking:
    def __init__(self, status):
        self.status = status


class TransitionTableTests(unittest.TestCase):
    def test_every_booking_state_action_pair(self):
        for status, action in product(BookingStatus, ("accept", "reject", "cancel")):
            with self.subTest(status=status.value, action=action):
                ride, booking = _Ride(seats=2), _Booking(status)
                expected = EXPECTED_BOOKING.get((status, action))
                if expected is None:
                    with self.assertRaises(HTTPException) as raised:
                        lifecycle.apply_booking_transition(ride, booking, action)
                    self.assertEqual((raised.exception.status_code, raised.exception.detail),
                                     (409, BOOKING_CONFLICT[action]))
                    self.assertEqual((ride.available_seats, booking.status), (2, status))
                else:
                    lifecycle.apply_booking_transition(ride, booking, action)
                    self.assertEqual((booking.status, ride.available_seats),
                                     (expected[0], 2 + expected[1]))

    def test_every_ride_state_action_pair(self):
        for status, action in product(RideStatus, ("complete", "cancel")):
            with self.subTest(status=status.value, action=action):
                expected = EXPECTED_RIDE.get((status, action))
                if expected is None:
                    with self.assertRaises(HTTPException) as raised:
                        lifecycle.ride_target(status, action)
                    self.assertEqual((raised.exception.status_code, raised.exception.detail),
                                     (409, "Ride is not active"))
                else:
                    self.assertEqual(lifecycle.ride_target(status, action), expected)

    def test_terminal_states_have_no_exits(self):
        for status in (B.rejected, B.cancelled):
            self.assertFalse([key for key in lifecycle.BOOKING_TRANSITIONS if key[0] == status])
        for status in (R.completed, R.cancelled):
            self.assertFalse([key for key in lifecycle.RIDE_TRANSITIONS if key[0] == status])

    def test_acceptance_never_drives_seats_negative(self):
        ride, booking = _Ride(seats=0), _Booking(B.pending)
        with self.assertRaises(HTTPException) as raised:
            lifecycle.apply_booking_transition(ride, booking, "accept")
        self.assertEqual(raised.exception.detail, "No seats available")
        self.assertEqual((ride.available_seats, booking.status), (0, B.pending))

    def test_accepted_cancellation_cutoff_boundary_is_inclusive(self):
        departure = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
        ride = _Ride(departure=departure)
        self.assertTrue(lifecycle.accepted_cancellation_closed(ride, now=departure))
        self.assertTrue(lifecycle.accepted_cancellation_closed(
            ride, now=departure + timedelta(microseconds=1)))
        self.assertFalse(lifecycle.accepted_cancellation_closed(
            ride, now=departure - timedelta(microseconds=1)))
        # 08:00 PKT is 03:00 UTC: instants compare, not wall clocks.
        pkt = timezone(timedelta(hours=5))
        self.assertTrue(lifecycle.accepted_cancellation_closed(
            ride, now=datetime(2026, 11, 3, 8, 0, tzinfo=pkt)))

    def test_completion_requires_departure_boundary_is_inclusive(self):
        departure = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
        ride = _Ride(departure=departure)
        lifecycle.require_departed_for_completion(ride, now=departure)  # allowed
        lifecycle.require_departed_for_completion(ride, now=departure + timedelta(hours=5))
        for early in (departure - timedelta(microseconds=1), departure - timedelta(days=1),
                      datetime(2026, 11, 3, 7, 59, 59, 999999, tzinfo=timezone(timedelta(hours=5)))):
            with self.subTest(now=early.isoformat()):
                with self.assertRaises(HTTPException) as raised:
                    lifecycle.require_departed_for_completion(ride, now=early)
                self.assertEqual((raised.exception.status_code, raised.exception.detail),
                                 (409, "Ride cannot be completed before its departure time"))


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class LifecycleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_lifecycle_" + uuid4().hex
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=10,
                                          connect_args={"server_settings": {
                                              "search_path": self.schema, "lock_timeout": "10000",
                                              "statement_timeout": "20000"}})
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            roles = [UserRole.driver] + [UserRole.passenger] * 9
            users = [User(name=f"fixture-{i}", email=f"lifecycle{i}@example.com",
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

    def headers(self, user):
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    async def seed(self, *, seats=2, ride_status=R.active, booking_status=None, departure=None):
        departure = departure or datetime.now(timezone.utc) + timedelta(days=1)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.driver.id, origin="Lahore", destination="Islamabad",
                        departure_time=departure, available_seats=seats, fare_per_seat=1500,
                        status=ride_status)
            db.add(ride)
            await db.flush()
            booking_id = None
            if booking_status is not None:
                booking = Booking(ride_id=ride.id, passenger_id=self.passenger.id,
                                  status=booking_status)
                db.add(booking)
                await db.flush()
                booking_id = booking.id
            await db.commit()
            return ride.id, booking_id

    async def state(self, ride_id):
        async with AsyncSession(self.engine) as db:
            ride = await db.get(Ride, ride_id)
            statuses = (await db.scalars(select(Booking.status).where(
                Booking.ride_id == ride_id).order_by(Booking.id))).all()
            return ride.status, ride.available_seats, list(statuses)

    async def act(self, action, ride_id=None, booking_id=None, user=None):
        if action in ("accept", "reject"):
            return await self.client.patch(
                f"/bookings/{booking_id}/respond", params={"accept": str(action == "accept").lower()},
                headers=self.headers(self.driver))
        if action == "cancel":
            return await self.client.patch(f"/bookings/{booking_id}/cancel",
                                           headers=self.headers(user or self.passenger))
        if action == "book":
            return await self.client.post(f"/bookings/{ride_id}",
                                          headers=self.headers(user or self.passengers[1]))
        return await self.client.patch(f"/rides/{ride_id}/{action.removeprefix('ride_')}",
                                       headers=self.headers(self.driver))

    # --- State-transition matrix through the API -------------------------------

    def expected_booking_response(self, ride_status, booking_status, action):
        """Contract, including guard order: respond checks the booking first,
        passenger cancellation checks the ride first."""
        legal = EXPECTED_BOOKING.get((booking_status, action))
        if action in ("accept", "reject"):
            if legal is None:
                if booking_status == B.cancelled and ride_status == R.active:
                    return 409, WITHDREW, None
                return 409, BOOKING_CONFLICT[action], None
            if ride_status != R.active:
                return 409, "Ride is not active", None
            return 200, None, legal
        if ride_status != R.active:
            return 409, "Ride is not active", None
        if legal is None:
            return 409, BOOKING_CONFLICT[action], None
        return 200, None, legal

    async def test_booking_matrix_every_ride_state_booking_state_and_action(self):
        for ride_status, booking_status, action in product(
                RideStatus, BookingStatus, ("accept", "reject", "cancel")):
            with self.subTest(ride=ride_status.value, booking=booking_status.value, action=action):
                ride_id, booking_id = await self.seed(ride_status=ride_status,
                                                      booking_status=booking_status)
                before = await self.state(ride_id)
                response = await self.act(action, booking_id=booking_id)
                code, detail, legal = self.expected_booking_response(ride_status, booking_status, action)
                self.assertEqual(response.status_code, code, response.text)
                if code == 409:
                    self.assertEqual(response.json(), {"detail": detail})
                    self.assertEqual(await self.state(ride_id), before)
                else:
                    target, delta = legal
                    self.assertEqual(response.json()["status"], target.value)
                    self.assertEqual(set(response.json()), {"id", "ride_id", "passenger_id", "status"})
                    self.assertEqual(await self.state(ride_id), (ride_status, 2 + delta, [target]))

    async def test_ride_matrix_every_ride_state_and_action(self):
        for ride_status, action in product(RideStatus, ("ride_complete", "ride_cancel", "book")):
            with self.subTest(ride=ride_status.value, action=action):
                departure = datetime.now(timezone.utc) + timedelta(days=1)
                ride_id, _ = await self.seed(ride_status=ride_status, departure=departure)
                before = await self.state(ride_id)
                # Completion is judged after departure (AST-016); booking before it.
                at = departure if action == "ride_complete" else departure - timedelta(hours=1)
                with patch("clock.utc_now", return_value=at):
                    response = await self.act(action, ride_id=ride_id)
                if ride_status != R.active:
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json(), {"detail": "Ride is not active"})
                    self.assertEqual(await self.state(ride_id), before)
                elif action == "book":
                    self.assertEqual(response.status_code, 201, response.text)
                    self.assertEqual(await self.state(ride_id), (R.active, 2, [B.pending]))
                else:
                    target = EXPECTED_RIDE[(ride_status, action.removeprefix("ride_"))]
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["status"], target.value)
                    self.assertEqual(await self.state(ride_id), (target, 2, []))

    # --- Cancellation cut-off ----------------------------------------------------

    async def test_accepted_booking_cannot_be_cancelled_after_departure(self):
        ride_id, booking_id = await self.seed(
            seats=1, booking_status=B.accepted,
            departure=datetime.now(timezone.utc) - timedelta(minutes=10))
        response = await self.act("cancel", booking_id=booking_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": DEPARTED})
        # The seat is not handed back to a ride that has left.
        self.assertEqual(await self.state(ride_id), (R.active, 1, [B.accepted]))

    async def test_pending_request_can_still_be_withdrawn_after_departure(self):
        ride_id, booking_id = await self.seed(
            booking_status=B.pending, departure=datetime.now(timezone.utc) - timedelta(minutes=10))
        response = await self.act("cancel", booking_id=booking_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.state(ride_id), (R.active, 2, [B.cancelled]))

    async def test_cancellation_cutoff_exact_departure_instant(self):
        departure = datetime.now(timezone.utc) + timedelta(hours=3)
        ride_id, booking_id = await self.seed(seats=1, booking_status=B.accepted, departure=departure)
        with patch("clock.utc_now", return_value=departure):
            at = await self.act("cancel", booking_id=booking_id)
        self.assertEqual((at.status_code, at.json()), (409, {"detail": DEPARTED}))
        self.assertEqual(await self.state(ride_id), (R.active, 1, [B.accepted]))
        with patch("clock.utc_now", return_value=departure - timedelta(microseconds=1)):
            before = await self.act("cancel", booking_id=booking_id)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(await self.state(ride_id), (R.active, 2, [B.cancelled]))

    async def test_cutoff_reports_terminal_booking_before_departure(self):
        # Guard order: an already-cancelled booking says so, even after departure.
        ride_id, booking_id = await self.seed(
            booking_status=B.cancelled, departure=datetime.now(timezone.utc) - timedelta(hours=1))
        response = await self.act("cancel", booking_id=booking_id)
        self.assertEqual(response.json(), {"detail": "Booking cannot be cancelled"})

    async def test_driver_may_still_cancel_a_departed_ride(self):
        # Documented policy: ride cancellation has no cut-off (driver no-show).
        ride_id, _ = await self.seed(seats=1, booking_status=B.accepted,
                                     departure=datetime.now(timezone.utc) - timedelta(hours=1))
        response = await self.act("ride_cancel", ride_id=ride_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.state(ride_id), (R.cancelled, 2, [B.cancelled]))

    async def lock_waiters(self):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(
                "SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = current_database() LIMIT 1"))

    async def test_cancellation_waiting_on_ride_lock_rechecks_departure(self):
        departure = datetime.now(timezone.utc) + timedelta(hours=2)
        ride_id, booking_id = await self.seed(seats=1, booking_status=B.accepted, departure=departure)
        current = {"now": departure - timedelta(minutes=1)}
        with patch("clock.utc_now", side_effect=lambda: current["now"]):
            async with self.engine.connect() as holder:
                transaction = await holder.begin()
                await holder.execute(select(Ride.id).where(Ride.id == ride_id).with_for_update())
                task = asyncio.create_task(self.act("cancel", booking_id=booking_id))
                waiting = None
                for _ in range(200):
                    waiting = await self.lock_waiters()
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(waiting, "cancellation never waited on the ride lock")
                current["now"] = departure  # departure passes while it waits
                await transaction.rollback()
            response = await asyncio.wait_for(task, timeout=10)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": DEPARTED}))
        self.assertEqual(await self.state(ride_id), (R.active, 1, [B.accepted]))

    # --- Concurrency and seat invariants (asyncio.gather, real PostgreSQL) -----

    async def accepted_count(self, ride_id):
        async with AsyncSession(self.engine) as db:
            return await db.scalar(select(func.count()).select_from(Booking).where(
                Booking.ride_id == ride_id, Booking.status == B.accepted))

    async def test_concurrent_requests_and_acceptances_never_oversell(self):
        capacity = 3
        ride_id, _ = await self.seed(seats=capacity)
        passengers = self.passengers[1:9]
        books = await asyncio.wait_for(asyncio.gather(*(
            self.act("book", ride_id=ride_id, user=p) for p in passengers)), timeout=20)
        self.assertEqual([r.status_code for r in books], [201] * len(passengers))
        accepts = await asyncio.wait_for(asyncio.gather(*(
            self.act("accept", booking_id=r.json()["id"]) for r in books)), timeout=20)
        codes = sorted(r.status_code for r in accepts)
        self.assertEqual(codes, [200] * capacity + [409] * (len(passengers) - capacity))
        self.assertTrue(all(r.json() == {"detail": "No seats available"}
                            for r in accepts if r.status_code == 409))
        status, seats, _ = await self.state(ride_id)
        self.assertEqual((status, seats, await self.accepted_count(ride_id)), (R.active, 0, capacity))

    async def test_mixed_concurrent_operations_conserve_seats(self):
        """available_seats + accepted bookings == capacity under any interleaving."""
        capacity = 2
        ride_id, _ = await self.seed(seats=capacity)
        books = [await self.act("book", ride_id=ride_id, user=p) for p in self.passengers[1:7]]
        ids = [r.json()["id"] for r in books]
        for booking_id in ids[:2]:
            self.assertEqual((await self.act("accept", booking_id=booking_id)).status_code, 200)
        users = self.passengers[1:7]
        operations = [
            self.act("cancel", booking_id=ids[0], user=users[0]),   # frees a seat
            self.act("cancel", booking_id=ids[0], user=users[0]),   # duplicate cancel
            self.act("cancel", booking_id=ids[1], user=users[1]),   # frees a seat
            self.act("accept", booking_id=ids[2]),
            self.act("accept", booking_id=ids[3]),
            self.act("accept", booking_id=ids[4]),
            self.act("reject", booking_id=ids[4]),                  # competes with accept
            self.act("cancel", booking_id=ids[5], user=users[5]),
            self.act("accept", booking_id=ids[5]),                  # competes with cancel
        ]
        responses = await asyncio.wait_for(asyncio.gather(*operations), timeout=30)
        self.assertTrue(all(r.status_code in (200, 409) for r in responses),
                        [r.status_code for r in responses])
        self.assertEqual(sorted(r.status_code for r in responses[:2]), [200, 409])
        status, seats, _ = await self.state(ride_id)
        accepted = await self.accepted_count(ride_id)
        self.assertEqual(status, R.active)
        self.assertGreaterEqual(seats, 0)
        self.assertEqual(seats + accepted, capacity)

    async def test_concurrent_cancel_accepted_and_ride_cancel_restore_once(self):
        ride_id, booking_id = await self.seed(seats=0, booking_status=B.accepted)
        responses = await asyncio.wait_for(asyncio.gather(
            self.act("cancel", booking_id=booking_id), self.act("ride_cancel", ride_id=ride_id),
        ), timeout=20)
        # Whichever wins the ride lock, the single debit is reversed exactly once.
        self.assertIn(sorted(r.status_code for r in responses), ([200, 200], [200, 409]))
        self.assertEqual(await self.state(ride_id), (R.cancelled, 1, [B.cancelled]))

    async def test_ride_completion_vs_cancellation_has_one_terminal_winner(self):
        # After departure (completion is only possible then, AST-016): complete,
        # cancel and a late acceptance race; exactly one terminal state wins and
        # the departed ride never gains an accepted passenger.
        ride_id, booking_id = await self.seed(
            seats=1, booking_status=B.pending,
            departure=datetime.now(timezone.utc) - timedelta(minutes=1))
        responses = await asyncio.wait_for(asyncio.gather(
            self.act("ride_complete", ride_id=ride_id), self.act("ride_cancel", ride_id=ride_id),
            self.act("accept", booking_id=booking_id),
        ), timeout=20)
        status, seats, bookings = await self.state(ride_id)
        self.assertIn(status, (R.completed, R.cancelled))
        self.assertEqual(sorted(r.status_code for r in responses[:2]), [200, 409])
        self.assertEqual(responses[2].status_code, 409, responses[2].text)
        self.assertEqual((seats, bookings), (1, [B.cancelled]))

    async def test_completion_vs_cancellation_before_departure_only_cancel_can_win(self):
        ride_id, booking_id = await self.seed(seats=1, booking_status=B.accepted)
        responses = await asyncio.wait_for(asyncio.gather(
            self.act("ride_complete", ride_id=ride_id), self.act("ride_cancel", ride_id=ride_id),
        ), timeout=20)
        self.assertEqual([r.status_code for r in responses], [409, 200])
        self.assertIn(responses[0].json()["detail"],
                      ("Ride cannot be completed before its departure time", "Ride is not active"))
        self.assertEqual(await self.state(ride_id), (R.cancelled, 2, [B.cancelled]))


if __name__ == "__main__":
    unittest.main()
