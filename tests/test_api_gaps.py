"""Milestone B API gaps (G2, G3, G6, G7, D1, D2, D7) against real PostgreSQL.

Set TEST_DATABASE_URL to a postgresql+asyncpg test database. Each test builds
its tables in a unique schema and drops it afterwards. The lifecycle clock is
frozen at NOW, so "departed" boundaries are exact. Fixture identities are
synthetic: no real CNIC, phone or personal data.
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
    "SECRET_KEY": "api-gaps-tests-only-not-a-production-key",
}):
    import database
    from main import app
    from models import Base, Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token

B, R = BookingStatus, RideStatus
NOW = datetime(2030, 3, 10, 6, 0, tzinfo=timezone.utc)
PKT = timezone(timedelta(hours=5))
WITHDREW = "This booking is no longer available."  # R-1 neutral wording, exact
ANSWERED = "Booking already responded to"
RIDE_KEYS = {"id", "driver_id", "origin", "destination", "departure_time",
             "available_seats", "fare_per_seat", "status"}


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class ApiGapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.schema = "test_api_gaps_" + uuid4().hex
        self.engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=5,
                                          connect_args={"server_settings": {
                                              "search_path": self.schema, "lock_timeout": "10000",
                                              "statement_timeout": "20000"}})
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            spec = [("driver", UserRole.driver), ("other_driver", UserRole.driver),
                    ("passenger", UserRole.passenger), ("other_passenger", UserRole.passenger),
                    ("admin", UserRole.admin)]
            self.users = {name: User(name=f"Fixture {name}", email=f"{name}@example.com",
                                     cnic=f"fixture-{name}", password="unused",
                                     phone_number="00000000000", role=role)
                          for name, role in spec}
            db.add_all(self.users.values())
            await db.commit()
        binding = patch.dict(database.SessionLocal.kw, {"bind": self.engine})
        binding.start()
        self.addCleanup(binding.stop)
        frozen = patch("clock.utc_now", return_value=NOW)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def headers(self, actor):
        user = self.users[actor]
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    async def ride(self, driver="driver", **values):
        fields = dict(origin="Lahore", destination="Islamabad", departure_time=NOW + timedelta(hours=2),
                      available_seats=3, fare_per_seat=1800, status=R.active)
        fields.update(values)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ride = Ride(driver_id=self.users[driver].id, **fields)
            db.add(ride)
            await db.commit()
            return ride.id

    async def booking(self, ride_id, passenger="passenger", status=B.pending):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            booking = Booking(ride_id=ride_id, passenger_id=self.users[passenger].id, status=status)
            db.add(booking)
            await db.commit()
            return booking.id

    async def get(self, path, actor, expected=200, **params):
        response = await self.client.get(path, params=params, headers=self.headers(actor))
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def ids(self, response):
        return [item["id"] for item in response.json()]

    def total(self, response):
        return int(response.headers["X-Total-Count"])

    # --- G2: passenger booking lookup / history --------------------------------

    async def test_g2_my_booking_for_a_ride_with_history_and_status(self):
        ride_id = await self.ride()
        other_ride = await self.ride()
        old = await self.booking(ride_id, status=B.cancelled)
        current = await self.booking(ride_id, status=B.pending)
        elsewhere = await self.booking(other_ride, status=B.accepted)
        response = await self.get("/bookings/me", "passenger", ride_id=ride_id)
        self.assertEqual(self.ids(response), [current, old])  # newest first
        self.assertEqual(self.total(response), 2)
        self.assertEqual(set(response.json()[0]), {"id", "ride_id", "passenger_id", "status", "ride"})
        self.assertEqual(set(response.json()[0]["ride"]), RIDE_KEYS)
        live = await self.get("/bookings/me", "passenger", ride_id=ride_id, status="pending")
        self.assertEqual((self.ids(live), self.total(live)), ([current], 1))
        unfiltered = await self.get("/bookings/me", "passenger")
        self.assertEqual((self.ids(unfiltered), self.total(unfiltered)), ([elsewhere, current, old], 3))
        page = await self.get("/bookings/me", "passenger", ride_id=ride_id, limit=1, offset=1)
        self.assertEqual((self.ids(page), self.total(page)), ([old], 2))

    async def test_g2_ride_filter_never_reveals_other_passengers_bookings(self):
        ride_id = await self.ride()
        await self.booking(ride_id, passenger="other_passenger")
        for actor in ("passenger", "driver"):  # driver accounts also have passenger mode
            with self.subTest(actor=actor):
                response = await self.get("/bookings/me", actor, ride_id=ride_id)
                self.assertEqual((response.json(), self.total(response)), ([], 0))
        missing = await self.get("/bookings/me", "passenger", ride_id=ride_id + 999)
        self.assertEqual((missing.json(), self.total(missing)), ([], 0))

    async def test_g2_validation_auth_and_role(self):
        for value in ("0", "-1", "x", "1.5", str(2**31)):
            with self.subTest(ride_id=value):
                response = await self.client.get("/bookings/me", params={"ride_id": value},
                                                 headers=self.headers("passenger"))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], list)
        self.assertEqual((await self.client.get("/bookings/me", params={"ride_id": 1})).status_code, 401)
        await self.get("/bookings/me", "admin", 403, ride_id=1)

    # --- G3: case-insensitive, trimmed place search ---------------------------

    async def test_g3_search_trims_and_case_folds_both_sides(self):
        plain = await self.ride()
        padded = await self.ride(origin="  lahore ", destination="ISLAMABAD")
        await self.ride(origin="Lahore Cantt")
        await self.ride(origin="Rawalpindi")
        for origin in ("Lahore", "lahore", "  LAHORE  ", "LaHoRe\t"):
            with self.subTest(origin=origin):
                response = await self.get("/rides/", "passenger", origin=origin, destination="islamabad ")
                self.assertEqual((self.ids(response), self.total(response)), ([plain, padded], 2))
        partial = await self.get("/rides/", "passenger", origin="Laho")
        self.assertEqual((partial.json(), self.total(partial)), ([], 0))

    async def test_g3_blank_place_after_trimming_is_422(self):
        for params in ({"origin": "   "}, {"destination": "\t"}, {"origin": ""}):
            with self.subTest(params=params):
                response = await self.client.get("/rides/", params=params, headers=self.headers("passenger"))
                self.assertEqual(response.status_code, 422, response.text)

    async def test_g3_wildcards_are_literal(self):
        await self.ride(origin="Lahore")
        for origin in ("%", "_ahore", "Lah%"):
            with self.subTest(origin=origin):
                self.assertEqual((await self.get("/rides/", "passenger", origin=origin)).json(), [])

    # --- Discovery excludes departed and non-active rides; totals agree -------

    async def test_discovery_excludes_departed_inactive_and_full_rides_from_rows_and_total(self):
        await self.ride(departure_time=NOW)                              # departs exactly now
        await self.ride(departure_time=NOW - timedelta(days=1))
        future = await self.ride(departure_time=NOW + timedelta(microseconds=1))
        await self.ride(status=R.cancelled)
        await self.ride(status=R.completed, departure_time=NOW - timedelta(hours=1))
        await self.ride(available_seats=0)
        response = await self.get("/rides/", "passenger")
        self.assertEqual((self.ids(response), self.total(response)), ([future], 1))
        wide = await self.get("/rides/", "passenger",
                              departure_from=(NOW - timedelta(days=2)).isoformat())
        self.assertEqual((self.ids(wide), self.total(wide)), ([future], 1))

    # --- G7: X-Total-Count on every list ---------------------------------------

    async def test_g7_search_total_ignores_limit_and_offset(self):
        ids = [await self.ride(departure_time=NOW + timedelta(hours=i + 1)) for i in range(5)]
        await self.ride(origin="Multan")
        first = await self.get("/rides/", "passenger", origin="lahore", limit=2)
        self.assertEqual((self.ids(first), self.total(first)), (ids[:2], 5))
        last = await self.get("/rides/", "passenger", origin="lahore", limit=2, offset=4)
        self.assertEqual((self.ids(last), self.total(last)), (ids[4:], 5))
        beyond = await self.get("/rides/", "passenger", origin="lahore", offset=50)
        self.assertEqual((beyond.json(), self.total(beyond)), ([], 5))
        seats = await self.get("/rides/", "passenger", min_seats=4)
        self.assertEqual((seats.json(), seats.headers["X-Total-Count"]), ([], "0"))

    async def test_g7_driver_lists_and_ride_bookings_totals(self):
        rides = [await self.ride(departure_time=NOW + timedelta(hours=i + 1)) for i in range(3)]
        await self.ride(driver="other_driver")
        mine = await self.get("/rides/me", "driver", limit=1)
        self.assertEqual((self.ids(mine), self.total(mine)), ([rides[2]], 3))
        for passenger, status in (("passenger", B.cancelled), ("passenger", B.pending),
                                  ("other_passenger", B.accepted)):
            await self.booking(rides[0], passenger=passenger, status=status)
        requests = await self.get(f"/rides/{rides[0]}/bookings", "driver", limit=1)
        self.assertEqual((len(requests.json()), self.total(requests)), (1, 3))
        pending = await self.get(f"/rides/{rides[0]}/bookings", "driver", status="pending")
        self.assertEqual((len(pending.json()), self.total(pending)), (1, 1))
        empty = await self.get(f"/rides/{rides[1]}/bookings", "driver")
        self.assertEqual((empty.json(), self.total(empty)), ([], 0))

    async def test_g7_errors_carry_no_total(self):
        ride_id = await self.ride()
        for path, actor, code in ((f"/rides/{ride_id}/bookings", "other_driver", 403),
                                  ("/bookings/me", "admin", 403), ("/rides/me", "admin", 403),
                                  ("/rides/", "admin", 422),
                                  (f"/rides/{ride_id + 99}/bookings", "driver", 404)):
            with self.subTest(path=path, actor=actor):
                params = {"limit": 0} if path == "/rides/" else {}
                response = await self.client.get(path, params=params, headers=self.headers(actor))
                self.assertEqual(response.status_code, code, response.text)
                self.assertNotIn("X-Total-Count", response.headers)

    # --- D1: /rides/me filtering ---------------------------------------------

    async def test_d1_upcoming_and_departed_split_at_the_lifecycle_boundary(self):
        at_now = await self.ride(departure_time=NOW)
        just_after = await self.ride(departure_time=NOW + timedelta(microseconds=1))
        past = await self.ride(departure_time=NOW - timedelta(days=1))
        later = await self.ride(departure_time=NOW + timedelta(days=1))
        upcoming = await self.get("/rides/me", "driver", when="upcoming", order="asc")
        self.assertEqual((self.ids(upcoming), self.total(upcoming)), ([just_after, later], 2))
        departed = await self.get("/rides/me", "driver", when="departed")
        self.assertEqual((self.ids(departed), self.total(departed)), ([at_now, past], 2))
        everything = await self.get("/rides/me", "driver")  # default order unchanged: DESC
        self.assertEqual(self.ids(everything), [later, just_after, at_now, past])

    async def test_d1_status_window_order_and_pagination_combine(self):
        a = await self.ride(departure_time=NOW + timedelta(hours=1))
        b = await self.ride(departure_time=NOW + timedelta(hours=1))   # id tie-break
        c = await self.ride(departure_time=NOW + timedelta(hours=3))
        await self.ride(departure_time=NOW + timedelta(hours=2), status=R.cancelled)
        await self.ride(departure_time=NOW + timedelta(hours=1), driver="other_driver")
        window = dict(departure_from=(NOW + timedelta(hours=1)).astimezone(PKT).isoformat(),
                      departure_before=(NOW + timedelta(hours=3)).isoformat())
        asc = await self.get("/rides/me", "driver", status="active", order="asc", **window)
        self.assertEqual((self.ids(asc), self.total(asc)), ([a, b], 2))  # before is exclusive
        desc = await self.get("/rides/me", "driver", status="active", order="desc", **window)
        self.assertEqual(self.ids(desc), [b, a])
        page = await self.get("/rides/me", "driver", status="active", when="upcoming",
                              order="asc", limit=1, offset=2)
        self.assertEqual((self.ids(page), self.total(page)), ([c], 3))
        cancelled = await self.get("/rides/me", "driver", status="cancelled", **window)
        self.assertEqual(self.total(cancelled), 1)

    async def test_d1_validation_and_role(self):
        for params in ({"when": "soon"}, {"order": "random"}, {"departure_from": "2030-03-10T06:00:00"},
                       {"departure_from": NOW.isoformat(), "departure_before": NOW.isoformat()},
                       {"departure_from": (NOW + timedelta(hours=1)).isoformat(),
                        "departure_before": NOW.isoformat()}):
            with self.subTest(params=params):
                response = await self.client.get("/rides/me", params=params, headers=self.headers("driver"))
                self.assertEqual(response.status_code, 422, response.text)
        await self.get("/rides/me", "admin", 403, when="upcoming")
        self.assertEqual((await self.client.get("/rides/me", params={"when": "upcoming"})).status_code, 401)

    # --- D2: per-ride booking counts for the owner ----------------------------

    async def test_d2_counts_by_status_for_own_rides_only(self):
        busy, quiet = await self.ride(), await self.ride()
        foreign = await self.ride(driver="other_driver")
        for passenger, status in (("passenger", B.pending), ("other_passenger", B.pending),
                                  ("passenger", B.cancelled), ("admin", B.accepted)):
            await self.booking(busy, passenger=passenger, status=status)
        await self.booking(foreign, passenger="passenger", status=B.pending)
        response = await self.client.get("/rides/me/booking-counts", headers=self.headers("driver"),
                                         params=[("ride_id", quiet), ("ride_id", foreign),
                                                 ("ride_id", busy), ("ride_id", quiet),
                                                 ("ride_id", busy + 1000)])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [
            {"ride_id": quiet, "pending": 0, "accepted": 0, "rejected": 0, "cancelled": 0},
            {"ride_id": busy, "pending": 2, "accepted": 1, "rejected": 0, "cancelled": 1},
        ])
        # The other driver sees only their own ride; ours are silently omitted.
        theirs = await self.client.get("/rides/me/booking-counts", headers=self.headers("other_driver"),
                                       params=[("ride_id", busy), ("ride_id", foreign)])
        self.assertEqual(theirs.json(), [
            {"ride_id": foreign, "pending": 1, "accepted": 0, "rejected": 0, "cancelled": 0}])

    async def test_d2_validation_auth_and_role(self):
        ride_id = await self.ride()
        for params in ([], [("ride_id", 0)], [("ride_id", "x")],
                       [("ride_id", i) for i in range(1, 102)]):
            with self.subTest(n=len(params)):
                response = await self.client.get("/rides/me/booking-counts", params=params,
                                                 headers=self.headers("driver"))
                self.assertEqual(response.status_code, 422, response.text)
        hundred = await self.client.get("/rides/me/booking-counts", headers=self.headers("driver"),
                                        params=[("ride_id", ride_id)] + [("ride_id", i + 5000) for i in range(99)])
        self.assertEqual(hundred.status_code, 200, hundred.text)
        self.assertEqual([row["ride_id"] for row in hundred.json()], [ride_id])
        response = await self.client.get("/rides/me/booking-counts", params={"ride_id": ride_id},
                                         headers=self.headers("admin"))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual((await self.client.get("/rides/me/booking-counts",
                                                params={"ride_id": ride_id})).status_code, 401)

    async def test_d2_route_does_not_shadow_ride_paths(self):
        ride_id = await self.ride()
        self.assertEqual((await self.get(f"/rides/{ride_id}", "passenger")).json()["id"], ride_id)
        await self.get(f"/rides/{ride_id}/bookings", "driver")
        await self.get("/rides/me", "driver")

    # --- D7: distinct 409s when answering a request ---------------------------

    async def respond(self, booking_id, accept):
        return await self.client.patch(f"/bookings/{booking_id}/respond",
                                       params={"accept": str(accept).lower()},
                                       headers=self.headers("driver"))

    async def test_d7_withdrawn_vs_already_answered(self):
        cases = (
            (R.active, B.cancelled, WITHDREW),        # passenger withdrew (pending or accepted)
            (R.active, B.accepted, ANSWERED),
            (R.active, B.rejected, ANSWERED),
            (R.cancelled, B.cancelled, ANSWERED),     # ride cancellation cascade: unchanged
            (R.completed, B.cancelled, ANSWERED),     # completion cascade: unchanged
        )
        for ride_status, booking_status, detail in cases:
            for accept in (True, False):
                with self.subTest(ride=ride_status.value, booking=booking_status.value, accept=accept):
                    ride_id = await self.ride(status=ride_status)
                    booking_id = await self.booking(ride_id, status=booking_status)
                    response = await self.respond(booking_id, accept)
                    self.assertEqual((response.status_code, response.json()), (409, {"detail": detail}))

    async def test_d7_passenger_withdraws_then_driver_answers_through_the_api(self):
        ride_id = await self.ride(available_seats=1)
        booking_id = (await self.client.post(f"/bookings/{ride_id}", headers=self.headers("passenger"))).json()["id"]
        self.assertEqual((await self.respond(booking_id, True)).status_code, 200)
        cancelled = await self.client.patch(f"/bookings/{booking_id}/cancel", headers=self.headers("passenger"))
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        for accept in (True, False):
            response = await self.respond(booking_id, accept)
            self.assertEqual(response.json(), {"detail": WITHDREW})
        async with AsyncSession(self.engine) as db:
            ride = await db.get(Ride, ride_id)
            booking = await db.get(Booking, booking_id)
            self.assertEqual((ride.available_seats, booking.status), (1, B.cancelled))

    async def test_r1_cancelled_booking_on_active_ride_has_neutral_exact_wording(self):
        # R-1 (Maaz, 31 Oct 2026). A booking on a still-active ride can be
        # cancelled by its passenger or by the passenger's account suspension
        # (#19 cancels pending and accepted bookings server-side, without the
        # passenger). The driver gets one neutral, exact string for both, which
        # never says why. Here the server-side cancellation is written directly,
        # as the suspension service does, for a pending and an accepted request.
        import lifecycle
        self.assertEqual(lifecycle.BOOKING_UNAVAILABLE, "This booking is no longer available.")
        self.assertEqual(lifecycle.PASSENGER_WITHDREW, lifecycle.BOOKING_UNAVAILABLE)
        for answered_first in (False, True):
            ride_id = await self.ride(available_seats=2)
            booking_id = (await self.client.post(f"/bookings/{ride_id}",
                                                 headers=self.headers("passenger"))).json()["id"]
            if answered_first:
                self.assertEqual((await self.respond(booking_id, True)).status_code, 200)
            async with AsyncSession(self.engine) as db:  # suspension-style cancellation
                booking = await db.get(Booking, booking_id)
                ride = await db.get(Ride, ride_id)
                booking.status = B.cancelled
                ride.available_seats = 2
                await db.commit()
            for accept in (True, False):
                with self.subTest(accepted_before=answered_first, accept=accept):
                    response = await self.respond(booking_id, accept)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json(), {"detail": "This booking is no longer available."})
                    detail = response.json()["detail"].lower()
                    for revealing in ("passenger", "suspend", "withdr", "cancelled by"):
                        self.assertNotIn(revealing, detail)
            async with AsyncSession(self.engine) as db:
                self.assertEqual(((await db.get(Ride, ride_id)).available_seats,
                                  (await db.get(Booking, booking_id)).status), (2, B.cancelled))

    async def lock_waiters(self):
        async with self.engine.connect() as conn:
            return await conn.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = current_database()"))

    async def test_d7_withdrawal_racing_an_accept_reports_the_withdrawal(self):
        # Cancel queues first on the Ride lock, accept second; after release the
        # accept sees the committed withdrawal (lock order Ride -> Booking).
        ride_id = await self.ride(available_seats=1)
        booking_id = await self.booking(ride_id)
        async with self.engine.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(select(Ride.id).where(Ride.id == ride_id).with_for_update())
            tasks = []
            for factory in (lambda: self.client.patch(f"/bookings/{booking_id}/cancel",
                                                      headers=self.headers("passenger")),
                            lambda: self.respond(booking_id, True)):
                tasks.append(asyncio.create_task(factory()))
                for _ in range(250):
                    if await self.lock_waiters() >= len(tasks):
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("request never waited on the ride lock")
            await transaction.rollback()
        cancel, accept = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual((accept.status_code, accept.json()), (409, {"detail": WITHDREW}))
        async with AsyncSession(self.engine) as db:
            self.assertEqual((await db.get(Ride, ride_id)).available_seats, 1)


if __name__ == "__main__":
    unittest.main()
