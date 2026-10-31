"""Account suspension, token revocation and admin user endpoints (Milestone D, M5-A).

Every test runs against an isolated PostgreSQL schema built from the models.
All identities are obviously fake (CNIC 12345-..., phone +92300000....).
"""

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "suspension-tests-only-not-a-production-key",
}):
    import jwt

    import config
    from database import Base, get_db
    from main import app
    from models import (AccountStatus, Booking, BookingStatus, IdempotencyKey, Ride, RideStatus,
                        User, UserRole)
    from routers import auth as auth_routes
    from routers import bookings as booking_routes
    from routers.admin import REASON_MAX_LENGTH
    from routers.auth import create_access_token, hash_password
    from routers.dependencies import ADMIN_FORBIDDEN_DETAIL, decode_access_claims, InvalidAccessToken
    from schemas import RideCreate
    from services import accounts as account_service
    from services import bookings as booking_service
    from services import rides as ride_service

PASSWORD = "Fake-test-password-123!"
FUTURE = lambda hours=24: datetime.now(timezone.utc) + timedelta(hours=hours)  # noqa: E731
PAST = lambda hours=2: datetime.now(timezone.utc) - timedelta(hours=hours)  # noqa: E731
REASON = {"reason": "Repeated no-shows reported by passengers"}
_PASSWORD_HASH = None


def password_hash():
    global _PASSWORD_HASH
    if _PASSWORD_HASH is None:
        _PASSWORD_HASH = hash_password(PASSWORD)
    return _PASSWORD_HASH


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class SuspensionTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_suspension_{uuid4().hex}"
        self.engine = create_async_engine(TEST_DATABASE_URL, connect_args={
            "server_settings": {"search_path": self.schema, "lock_timeout": "10000"},
        })
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        self.users = {}
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            for index, (name, role) in enumerate((
                ("admin", UserRole.admin), ("other_admin", UserRole.admin),
                ("driver", UserRole.driver), ("other_driver", UserRole.driver),
                ("passenger", UserRole.passenger), ("other_passenger", UserRole.passenger),
            )):
                user = User(
                    name=f"Test {name}", email=f"Test.{name}@Example.com",
                    cnic=f"12345-00000{index:02d}-{index % 10}",
                    phone_number=f"+92300000{index:04d}",
                    password=password_hash(), role=role,
                )
                db.add(user)
                self.users[name] = user
            await db.commit()

        async def override_get_db():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def session(self):
        return AsyncSession(self.engine, expire_on_commit=False)

    def token(self, name, token_version=0):
        user = self.users[name]
        return create_access_token(user.id, user.role, token_version)

    def auth(self, name_or_token):
        token = self.token(name_or_token) if name_or_token in self.users else name_or_token
        return {"Authorization": f"Bearer {token}"}

    async def suspend(self, target, actor="admin", body=REASON):
        return await self.client.post(
            f"/admin/users/{self.users[target].id}/suspend", json=body, headers=self.auth(actor))

    async def reinstate(self, target, actor="admin", body=REASON):
        return await self.client.post(
            f"/admin/users/{self.users[target].id}/reinstate", json=body, headers=self.auth(actor))

    async def login(self, name, password=PASSWORD):
        return await self.client.post("/auth/login", json={
            "email": self.users[name].email, "password": password,
        })

    async def account(self, name):
        async with self.session() as db:
            return (await db.execute(select(User.account_status, User.token_version)
                                     .where(User.id == self.users[name].id))).one()

    async def add_ride(self, driver="driver", departure=None, seats=3, status=RideStatus.active):
        async with self.session() as db:
            ride = Ride(driver_id=self.users[driver].id, origin="Lahore", destination="Islamabad",
                        departure_time=departure or FUTURE(), available_seats=seats,
                        fare_per_seat=2500, status=status)
            db.add(ride)
            await db.commit()
            return ride.id

    async def add_booking(self, ride_id, passenger="passenger", status=BookingStatus.pending,
                          seats=1, expires_at=None):
        async with self.session() as db:
            booking = Booking(ride_id=ride_id, passenger_id=self.users[passenger].id, status=status,
                              seats_requested=seats, expires_at=expires_at)
            db.add(booking)
            await db.commit()
            return booking.id

    async def ride_state(self, ride_id):
        async with self.session() as db:
            return (await db.execute(select(Ride.status, Ride.available_seats)
                                     .where(Ride.id == ride_id))).one()

    async def booking_status(self, booking_id):
        async with self.session() as db:
            return await db.scalar(select(Booking.status).where(Booking.id == booking_id))


class AdminUserEndpointTests(SuspensionTestBase):
    async def test_lookup_returns_masked_pii_only(self):
        target = self.users["passenger"]
        response = await self.client.get(f"/admin/users/{target.id}", headers=self.auth("admin"))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"id", "name", "email_masked", "cnic_masked", "phone_masked",
                                     "role", "account_status", "created_at"})
        self.assertEqual(body["cnic_masked"], "*****-****004-*")
        self.assertEqual(body["phone_masked"], "+92 3** *** **04")
        self.assertEqual(body["email_masked"], "T***@Example.com")
        self.assertEqual(body["account_status"], "active")
        for raw in (target.cnic, target.phone_number, target.email, target.password, "00000",
                    "token_version"):
            self.assertNotIn(raw, response.text)

    async def test_lookup_unknown_and_out_of_range_ids(self):
        response = await self.client.get("/admin/users/999999", headers=self.auth("admin"))
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "User not found"}))
        for bad in ("0", "-1", str(2**31), "abc"):
            with self.subTest(user_id=bad):
                response = await self.client.get(f"/admin/users/{bad}", headers=self.auth("admin"))
                self.assertEqual(response.status_code, 422, response.text)

    async def test_non_admins_get_403_and_nothing_changes(self):
        target = self.users["other_passenger"].id
        for actor in ("passenger", "driver", "other_passenger"):
            for method, path, body in (
                ("GET", f"/admin/users/{target}", None),
                ("POST", f"/admin/users/{target}/suspend", REASON),
                ("POST", f"/admin/users/{target}/reinstate", REASON),
                # IDOR: acting on their own id is still admin-only.
                ("POST", f"/admin/users/{self.users[actor].id}/suspend", REASON),
            ):
                with self.subTest(actor=actor, path=path):
                    response = await self.client.request(method, path, json=body, headers=self.auth(actor))
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json(), {"detail": ADMIN_FORBIDDEN_DETAIL})
        for name in ("passenger", "driver", "other_passenger"):
            self.assertEqual(await self.account(name), ("active", 0))

    async def test_unauthenticated_gets_401(self):
        target = self.users["passenger"].id
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic YTpi"}):
            for method, path in (("GET", f"/admin/users/{target}"),
                                 ("POST", f"/admin/users/{target}/suspend")):
                with self.subTest(headers=headers, path=path):
                    response = await self.client.request(method, path, json=REASON, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(await self.account("passenger"), ("active", 0))

    async def test_admin_cannot_suspend_or_reinstate_self(self):
        for call in (self.suspend, self.reinstate):
            response = await call("admin", actor="admin")
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(await self.account("admin"), ("active", 0))
        # Another admin can (break-glass for a compromised admin account).
        self.assertEqual((await self.suspend("admin", actor="other_admin")).status_code, 200)
        self.assertEqual(await self.account("admin"), ("suspended", 1))

    async def test_reason_validation_and_boundaries(self):
        bad_bodies = {
            "missing": {},
            "null": {"reason": None},
            "empty": {"reason": ""},
            "whitespace": {"reason": "    \n  "},
            "too short after strip": {"reason": "  ab  "},
            "too long": {"reason": "x" * (REASON_MAX_LENGTH + 1)},
            "cnic": {"reason": "Fake ID 12345-1234567-1 used"},
            "bare cnic digits": {"reason": "ID 1234512345671"},
            "phone": {"reason": "Call +92 300 1234567"},
            "control char": {"reason": "spam\x00here"},
            "not a string": {"reason": 12345},
        }
        for label, body in bad_bodies.items():
            with self.subTest(case=label):
                response = await self.suspend("passenger", body=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("1234567", response.text)
        self.assertEqual(await self.account("passenger"), ("active", 0))
        for ok in ("abc", "y" * REASON_MAX_LENGTH, "Ride 123456 cancelled late"):
            with self.subTest(ok=ok[:20]):
                self.assertEqual((await self.suspend("passenger", body={"reason": ok})).status_code, 200)
                self.assertEqual((await self.reinstate("passenger", body={"reason": ok})).status_code, 200)

    async def test_unknown_target_and_double_actions(self):
        response = await self.client.post("/admin/users/999999/suspend", json=REASON,
                                          headers=self.auth("admin"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.reinstate("passenger")).status_code, 409)
        self.assertEqual((await self.suspend("passenger")).status_code, 200)
        response = await self.suspend("passenger")
        self.assertEqual((response.status_code, response.json()), (409, {"detail": "User is already suspended"}))
        self.assertEqual(await self.account("passenger"), ("suspended", 1))


class TokenRevocationTests(SuspensionTestBase):
    async def test_suspension_revokes_issued_tokens_and_blocks_login(self):
        login = await self.login("passenger")
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["access_token"]
        self.assertEqual(jwt.decode(token, options={"verify_signature": False})["tv"], 0)
        self.assertEqual((await self.client.get("/me", headers=self.auth(token))).status_code, 200)

        response = await self.suspend("passenger")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["account_status"], "suspended")

        me = await self.client.get("/me", headers=self.auth(token))
        self.assertEqual((me.status_code, me.json()), (401, {"detail": "Invalid token"}))
        self.assertEqual(me.headers.get("WWW-Authenticate"), "Bearer")
        # A token minted with the NEW version still fails while suspended.
        current = self.token("passenger", token_version=1)
        self.assertEqual((await self.client.get("/me", headers=self.auth(current))).status_code, 401)

        right = await self.login("passenger")
        wrong = await self.login("passenger", password="Wrong-password-123!")
        self.assertEqual(right.status_code, auth_routes.SUSPENDED_LOGIN_RESPONSE[0])
        self.assertEqual(right.json(), {"detail": auth_routes.SUSPENDED_LOGIN_RESPONSE[1]})
        # D6 default: indistinguishable from a wrong password.
        self.assertEqual((right.status_code, right.json()), (wrong.status_code, wrong.json()))
        self.assertNotIn("access_token", right.text)

    async def test_suspended_login_message_is_a_single_switchable_constant(self):
        await self.suspend("passenger")
        with patch.object(auth_routes, "SUSPENDED_LOGIN_RESPONSE", (403, "Account suspended")):
            right = await self.login("passenger")
            wrong = await self.login("passenger", password="Wrong-password-123!")
        self.assertEqual((right.status_code, right.json()), (403, {"detail": "Account suspended"}))
        self.assertEqual(wrong.status_code, 401)  # password still checked first

    async def test_reinstated_user_must_log_in_again(self):
        old = (await self.login("driver")).json()["access_token"]
        await self.suspend("driver")
        response = await self.reinstate("driver")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["account_status"], "active")
        self.assertEqual(await self.account("driver"), ("active", 2))
        for version in (0, 1):
            stale = self.token("driver", token_version=version)
            self.assertEqual((await self.client.get("/me", headers=self.auth(stale))).status_code, 401)
        self.assertEqual((await self.client.get("/me", headers=self.auth(old))).status_code, 401)
        fresh = await self.login("driver")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        token = fresh.json()["access_token"]
        self.assertEqual(jwt.decode(token, options={"verify_signature": False})["tv"], 2)
        self.assertEqual((await self.client.get("/me", headers=self.auth(token))).status_code, 200)

    def legacy_token(self, name, **extra):
        user = self.users[name]
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": str(user.id), "role": user.role.value, "iat": now - 5, "exp": now + 600, **extra}
        return jwt.encode(claims, config.SECRET_KEY, algorithm=config.ALGORITHM)

    async def test_pre_milestone_tokens_without_tv_keep_working_until_revoked(self):
        legacy = self.legacy_token("passenger")
        self.assertNotIn("tv", jwt.decode(legacy, options={"verify_signature": False}))
        self.assertEqual((await self.client.get("/me", headers=self.auth(legacy))).status_code, 200)
        await self.suspend("passenger")
        await self.reinstate("passenger")
        self.assertEqual((await self.client.get("/me", headers=self.auth(legacy))).status_code, 401)

    async def test_malformed_or_future_tv_claims_are_rejected(self):
        for label, tv in (("string", "0"), ("bool", False), ("float", 0.0), ("negative", -1),
                          ("too large", 2**31), ("null", None), ("future version", 1)):
            with self.subTest(tv=label):
                token = self.legacy_token("passenger", tv=tv)
                response = await self.client.get("/me", headers=self.auth(token))
                self.assertEqual(response.status_code, 401, response.text)
        for tv in (False, "0", None, -1):
            with self.assertRaises(InvalidAccessToken):
                decode_access_claims(self.legacy_token("passenger", tv=tv))
        self.assertEqual(decode_access_claims(self.legacy_token("passenger", tv=0)).token_version, 0)
        for bad in (-1, 2**31, True, "1"):
            with self.subTest(issue=bad), self.assertRaises(ValueError):
                create_access_token(self.users["passenger"].id, "passenger", bad)

    async def test_suspended_user_cannot_use_any_authenticated_endpoint(self):
        ride_id = await self.add_ride(driver="other_driver")
        await self.suspend("passenger")
        await self.suspend("driver")
        token_p = self.token("passenger", 1)
        token_d = self.token("driver", 1)
        calls = (
            ("POST", f"/bookings/{ride_id}", token_p, None),
            ("GET", "/bookings/me", token_p, None),
            ("POST", "/rides/", token_d, {"origin": "Lahore", "destination": "Islamabad",
                                         "departure_time": FUTURE().isoformat(),
                                         "available_seats": 2, "fare_per_seat": 2000}),
            ("GET", "/rides/me", token_d, None),
        )
        for method, path, token, body in calls:
            with self.subTest(path=path):
                response = await self.client.request(method, path, json=body, headers=self.auth(token))
                self.assertEqual(response.status_code, 401, response.text)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(Booking.id).limit(1)), None)
            self.assertEqual(await db.scalar(
                select(Ride.id).where(Ride.driver_id == self.users["driver"].id)), None)


class SuspensionEffectTests(SuspensionTestBase):
    async def test_suspended_driver_future_rides_and_bookings_are_cancelled(self):
        future = await self.add_ride(seats=1)  # 3 seats total: 2 accepted
        b_pending = await self.add_booking(future, "passenger", BookingStatus.pending)
        b_acc1 = await self.add_booking(future, "other_passenger", BookingStatus.accepted)
        b_acc2 = await self.add_booking(future, "other_driver", BookingStatus.accepted)
        b_rejected = await self.add_booking(future, "admin", BookingStatus.rejected)
        departed = await self.add_ride(departure=PAST(), seats=2)
        b_departed = await self.add_booking(departed, "passenger", BookingStatus.accepted)
        done = await self.add_ride(departure=PAST(48), status=RideStatus.completed, seats=0)
        cancelled_before = await self.add_ride(status=RideStatus.cancelled, seats=3)
        # The driver is also a passenger on someone else's ride.
        other = await self.add_ride(driver="other_driver", seats=1)
        b_as_passenger = await self.add_booking(other, "driver", BookingStatus.accepted)
        b_other_passenger = await self.add_booking(other, "passenger", BookingStatus.pending)
        untouched_ride = await self.add_ride(driver="other_driver", seats=2)

        response = await self.suspend("driver")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["cancelled_ride_ids"], [future])
        self.assertEqual(body["cancelled_booking_ids"],
                         sorted([b_pending, b_acc1, b_acc2, b_as_passenger]))
        self.assertEqual(body["departed_active_ride_ids"], [departed])

        self.assertEqual(await self.ride_state(future), (RideStatus.cancelled, 3))
        for booking in (b_pending, b_acc1, b_acc2, b_as_passenger):
            self.assertEqual(await self.booking_status(booking), BookingStatus.cancelled)
        self.assertEqual(await self.booking_status(b_rejected), BookingStatus.rejected)
        self.assertEqual(await self.ride_state(departed), (RideStatus.active, 2))
        self.assertEqual(await self.booking_status(b_departed), BookingStatus.accepted)
        self.assertEqual(await self.ride_state(done), (RideStatus.completed, 0))
        self.assertEqual(await self.ride_state(cancelled_before), (RideStatus.cancelled, 3))
        self.assertEqual(await self.ride_state(other), (RideStatus.active, 2))  # seat returned
        self.assertEqual(await self.booking_status(b_other_passenger), BookingStatus.pending)
        self.assertEqual(await self.ride_state(untouched_ride), (RideStatus.active, 2))

        # Reinstatement restores the account, never the cancelled trips.
        await self.reinstate("driver")
        self.assertEqual(await self.ride_state(future), (RideStatus.cancelled, 3))
        self.assertEqual(await self.booking_status(b_as_passenger), BookingStatus.cancelled)

    async def test_suspended_passenger_bookings_are_cancelled_and_seats_returned(self):
        ride_a = await self.add_ride(seats=0)
        accepted = await self.add_booking(ride_a, "passenger", BookingStatus.accepted)
        ride_b = await self.add_ride(driver="other_driver", seats=2)
        pending = await self.add_booking(ride_b, "passenger", BookingStatus.pending)
        neighbour = await self.add_booking(ride_b, "other_passenger", BookingStatus.pending)
        past = await self.add_ride(departure=PAST(), seats=0)
        past_booking = await self.add_booking(past, "passenger", BookingStatus.accepted)

        body = (await self.suspend("passenger")).json()
        self.assertEqual(body["cancelled_ride_ids"], [])
        self.assertEqual(body["cancelled_booking_ids"], sorted([accepted, pending]))
        self.assertEqual(body["departed_active_ride_ids"], [])
        self.assertEqual(await self.ride_state(ride_a), (RideStatus.active, 1))
        self.assertEqual(await self.ride_state(ride_b), (RideStatus.active, 2))
        self.assertEqual(await self.booking_status(neighbour), BookingStatus.pending)
        self.assertEqual(await self.booking_status(past_booking), BookingStatus.accepted)
        self.assertEqual(await self.ride_state(past), (RideStatus.active, 0))

    async def test_failed_suspension_rolls_back_every_change(self):
        ride = await self.add_ride(seats=2)
        booking = await self.add_booking(ride, "passenger", BookingStatus.accepted)
        original_commit = AsyncSession.commit

        async def failing_commit(session):
            raise RuntimeError("simulated commit failure")

        with patch.object(AsyncSession, "commit", failing_commit):
            with self.assertRaises(RuntimeError):
                await self.suspend("driver")
        self.assertIs(AsyncSession.commit, original_commit)
        self.assertEqual(await self.account("driver"), ("active", 0))
        self.assertEqual(await self.ride_state(ride), (RideStatus.active, 2))
        self.assertEqual(await self.booking_status(booking), BookingStatus.accepted)


class MultiSeatSuspensionTests(SuspensionTestBase):
    """c1a7e0b00003: suspension restores exactly the seats each accepted booking holds."""

    async def test_suspended_driver_with_two_and_three_seat_bookings_gets_every_seat_back(self):
        # Ride A: capacity 3, one 2-seat acceptance -> 1 left.
        ride_a = await self.add_ride(seats=1)
        a_two = await self.add_booking(ride_a, "passenger", BookingStatus.accepted, seats=2)
        # Ride B: capacity 8, 3 + 2 accepted -> 3 left; a 4-seat request is pending (no debit).
        ride_b = await self.add_ride(seats=3)
        b_three = await self.add_booking(ride_b, "passenger", BookingStatus.accepted, seats=3)
        b_two = await self.add_booking(ride_b, "other_passenger", BookingStatus.accepted, seats=2)
        b_pending = await self.add_booking(ride_b, "admin", BookingStatus.pending, seats=4)
        b_rejected = await self.add_booking(ride_b, "other_admin", BookingStatus.rejected, seats=3)

        response = await self.suspend("driver")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["cancelled_ride_ids"], sorted([ride_a, ride_b]))
        self.assertEqual(body["cancelled_booking_ids"], sorted([a_two, b_three, b_two, b_pending]))
        self.assertEqual(body["expired_booking_ids"], [])
        self.assertEqual(await self.ride_state(ride_a), (RideStatus.cancelled, 3))
        self.assertEqual(await self.ride_state(ride_b), (RideStatus.cancelled, 8))
        for booking in (a_two, b_three, b_two, b_pending):
            self.assertEqual(await self.booking_status(booking), BookingStatus.cancelled)
        self.assertEqual(await self.booking_status(b_rejected), BookingStatus.rejected)

    async def test_suspended_driver_with_single_three_seat_booking_restores_three(self):
        # Boundary: one booking holding every seat of a 3-seat ride.
        ride = await self.add_ride(seats=0)
        booking = await self.add_booking(ride, "passenger", BookingStatus.accepted, seats=3)
        body = (await self.suspend("driver")).json()
        self.assertEqual(body["cancelled_booking_ids"], [booking])
        self.assertEqual(await self.ride_state(ride), (RideStatus.cancelled, 3))

    async def test_suspended_passenger_three_seat_booking_on_another_ride_restores_three(self):
        ride = await self.add_ride(driver="other_driver", seats=1)   # capacity 6
        mine = await self.add_booking(ride, "passenger", BookingStatus.accepted, seats=3)
        theirs = await self.add_booking(ride, "other_passenger", BookingStatus.accepted, seats=2)
        body = (await self.suspend("passenger")).json()
        self.assertEqual(body["cancelled_ride_ids"], [])
        self.assertEqual(body["cancelled_booking_ids"], [mine])
        self.assertEqual(await self.ride_state(ride), (RideStatus.active, 4))   # 1 + 3, not 1 + 1
        self.assertEqual(await self.booking_status(theirs), BookingStatus.accepted)

    async def test_due_pending_requests_are_expired_not_cancelled(self):
        # c1a7e0b00002: a pending request past expires_at reads 'expired' even
        # before the sweep. Suspension records it as such; no seat effect.
        own_ride = await self.add_ride(seats=2)
        due_on_own = await self.add_booking(own_ride, "passenger", BookingStatus.pending, seats=2,
                                            expires_at=PAST(1))
        live_on_own = await self.add_booking(own_ride, "other_passenger", BookingStatus.pending,
                                             expires_at=FUTURE(1))
        already_expired = await self.add_booking(own_ride, "admin", BookingStatus.expired,
                                                 expires_at=PAST(3))
        other = await self.add_ride(driver="other_driver", seats=1)
        due_as_passenger = await self.add_booking(other, "driver", BookingStatus.pending,
                                                  seats=3, expires_at=PAST(1))

        body = (await self.suspend("driver")).json()
        self.assertEqual(body["cancelled_ride_ids"], [own_ride])
        self.assertEqual(body["cancelled_booking_ids"], [live_on_own])
        self.assertEqual(body["expired_booking_ids"], sorted([due_on_own, due_as_passenger]))
        self.assertEqual(await self.booking_status(due_on_own), BookingStatus.expired)
        self.assertEqual(await self.booking_status(due_as_passenger), BookingStatus.expired)
        self.assertEqual(await self.booking_status(live_on_own), BookingStatus.cancelled)
        self.assertEqual(await self.booking_status(already_expired), BookingStatus.expired)
        self.assertEqual(await self.ride_state(own_ride), (RideStatus.cancelled, 2))
        self.assertEqual(await self.ride_state(other), (RideStatus.active, 1))


class SuspensionRaceTests(SuspensionTestBase):
    """Forced interleavings: each test pins which transaction takes the user lock first."""

    async def load(self, db, name):
        return await db.get(User, self.users[name].id)

    async def wait_until_blocked(self, task, timeout=5.0):
        """Wait until another backend is waiting on a lock in our database, or the task ended."""
        deadline = asyncio.get_running_loop().time() + timeout
        async with self.engine.connect() as conn:
            while asyncio.get_running_loop().time() < deadline:
                waiting = await conn.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock'"))
                if waiting or task.done():
                    return bool(waiting)
                # pg_stat_activity's list of backends is snapshotted once per transaction:
                # end it, or a backend that connected after the first poll is never seen.
                await conn.rollback()
                await asyncio.sleep(0.02)
        return False

    async def test_suspension_first_rejects_concurrent_booking(self):
        ride = await self.add_ride(driver="other_driver", seats=2)
        async with self.session() as admin_db, self.session() as passenger_db:
            passenger = await self.load(passenger_db, "passenger")  # authenticated before suspension
            admin = await self.load(admin_db, "admin")
            await account_service.suspend_user(admin_db, admin, passenger.id)  # holds the user lock
            book = asyncio.create_task(booking_service.book_ride(ride, passenger_db, passenger))
            self.assertTrue(await self.wait_until_blocked(book), "booking did not wait for the suspension")
            await admin_db.commit()
            with self.assertRaises(HTTPException) as raised:
                await book
            self.assertEqual(raised.exception.status_code, 401)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(Booking.id).where(Booking.ride_id == ride)), None)
        self.assertEqual(await self.ride_state(ride), (RideStatus.active, 2))

    async def test_idempotent_booking_waits_on_user_lock_before_claiming_its_key(self):
        # Lock order User -> Idempotency key -> Ride: while the suspension holds
        # the user row, the keyed request must not have claimed (inserted) its key.
        ride = await self.add_ride(driver="other_driver", seats=2)
        async with self.session() as admin_db, self.session() as passenger_db:
            passenger = await self.load(passenger_db, "passenger")
            admin = await self.load(admin_db, "admin")
            await account_service.suspend_user(admin_db, admin, passenger.id)
            book = asyncio.create_task(booking_routes.book_ride(
                ride_id=ride, body=None, db=passenger_db, current_user=passenger,
                idempotency_key="fake-key-race-0001"))
            self.assertTrue(await self.wait_until_blocked(book), "booking did not wait for the suspension")
            async with self.engine.connect() as conn:
                waiting_pid = await conn.scalar(text(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock'"))
                held_on_keys = await conn.scalar(text(
                    "SELECT count(*) FROM pg_locks WHERE pid = :pid "
                    "AND relation = 'idempotency_keys'::regclass"), {"pid": waiting_pid})
            await admin_db.commit()
            with self.assertRaises(HTTPException) as raised:
                await book
            self.assertEqual(raised.exception.status_code, 401)
            self.assertEqual(held_on_keys, 0, "idempotency key claimed before the user lock")
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(IdempotencyKey.key)), None)
            self.assertEqual(await db.scalar(select(Booking.id).where(Booking.ride_id == ride)), None)

    async def test_booking_first_is_cancelled_by_the_waiting_suspension(self):
        ride = await self.add_ride(driver="other_driver", seats=2)
        release = asyncio.Event()
        reached_commit = asyncio.Event()
        async with self.session() as admin_db, self.session() as passenger_db:
            passenger = await self.load(passenger_db, "passenger")
            admin = await self.load(admin_db, "admin")
            real_commit = passenger_db.commit

            async def held_commit():
                reached_commit.set()
                await release.wait()
                await real_commit()

            with patch.object(passenger_db, "commit", held_commit):
                book = asyncio.create_task(booking_service.book_ride(ride, passenger_db, passenger))
                await asyncio.wait_for(reached_commit.wait(), 5)
                suspend = asyncio.create_task(account_service.suspend_user(admin_db, admin, passenger.id))
                self.assertTrue(await self.wait_until_blocked(suspend), "suspension did not wait")
                release.set()
                booking = await book
            _, effects = await suspend
            await admin_db.commit()
        self.assertEqual(effects.cancelled_booking_ids, [booking.id])
        self.assertEqual(await self.booking_status(booking.id), BookingStatus.cancelled)
        self.assertEqual(await self.ride_state(ride), (RideStatus.active, 2))

    async def test_suspension_first_rejects_concurrent_ride_creation(self):
        payload = RideCreate(origin="Lahore", destination="Islamabad", departure_time=FUTURE(),
                             available_seats=2, fare_per_seat=2000)
        async with self.session() as admin_db, self.session() as driver_db:
            driver = await self.load(driver_db, "driver")
            await account_service.suspend_user(admin_db, await self.load(admin_db, "admin"), driver.id)
            create = asyncio.create_task(ride_service.create_ride(payload, driver_db, driver))
            self.assertTrue(await self.wait_until_blocked(create))
            await admin_db.commit()
            with self.assertRaises(HTTPException) as raised:
                await create
            self.assertEqual(raised.exception.status_code, 401)
        async with self.session() as db:
            self.assertEqual(await db.scalar(
                select(Ride.id).where(Ride.driver_id == self.users["driver"].id)), None)

    async def test_ride_created_first_is_cancelled_by_the_waiting_suspension(self):
        payload = RideCreate(origin="Lahore", destination="Islamabad", departure_time=FUTURE(),
                             available_seats=2, fare_per_seat=2000)
        release, reached = asyncio.Event(), asyncio.Event()
        async with self.session() as admin_db, self.session() as driver_db:
            driver = await self.load(driver_db, "driver")
            admin = await self.load(admin_db, "admin")
            real_commit = driver_db.commit

            async def held_commit():
                reached.set()
                await release.wait()
                await real_commit()

            with patch.object(driver_db, "commit", held_commit):
                create = asyncio.create_task(ride_service.create_ride(payload, driver_db, driver))
                await asyncio.wait_for(reached.wait(), 5)
                suspend = asyncio.create_task(account_service.suspend_user(admin_db, admin, driver.id))
                self.assertTrue(await self.wait_until_blocked(suspend))
                release.set()
                ride = await create
            _, effects = await suspend
            await admin_db.commit()
        self.assertEqual(effects.cancelled_ride_ids, [ride.id])
        self.assertEqual((await self.ride_state(ride.id))[0], RideStatus.cancelled)

    async def test_booking_on_suspended_drivers_ride_committed_first_is_cancelled(self):
        ride = await self.add_ride(driver="driver", seats=2)
        release, reached = asyncio.Event(), asyncio.Event()
        async with self.session() as admin_db, self.session() as passenger_db:
            passenger = await self.load(passenger_db, "passenger")
            admin = await self.load(admin_db, "admin")
            real_commit = passenger_db.commit

            async def held_commit():
                reached.set()
                await release.wait()
                await real_commit()

            with patch.object(passenger_db, "commit", held_commit):
                book = asyncio.create_task(booking_service.book_ride(ride, passenger_db, passenger))
                await asyncio.wait_for(reached.wait(), 5)
                suspend = asyncio.create_task(
                    account_service.suspend_user(admin_db, admin, self.users["driver"].id))
                self.assertTrue(await self.wait_until_blocked(suspend))
                release.set()
                booking = await book
            _, effects = await suspend
            await admin_db.commit()
        self.assertEqual(effects.cancelled_ride_ids, [ride])
        self.assertEqual(effects.cancelled_booking_ids, [booking.id])
        self.assertEqual(await self.booking_status(booking.id), BookingStatus.cancelled)

    async def test_concurrent_suspensions_apply_once(self):
        async def attempt(actor):
            return await self.suspend("passenger", actor=actor)

        responses = await asyncio.gather(attempt("admin"), attempt("other_admin"))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertEqual(await self.account("passenger"), ("suspended", 1))


if __name__ == "__main__":
    unittest.main()
