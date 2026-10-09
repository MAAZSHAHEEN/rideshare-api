"""Authenticated dashboard reads against isolated PostgreSQL schemas."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "frontend-tests-only-not-a-production-key",
}):
    from database import Base, get_db
    from main import app
    from models import Booking, BookingStatus, Ride, RideStatus, User, UserRole
    from routers.auth import create_access_token


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class FrontendReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_frontend_{uuid4().hex}"
        self.engine = create_async_engine(TEST_DATABASE_URL, connect_args={
            "server_settings": {"search_path": self.schema},
        })
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            self.users = {}
            for name, role in (("driver", UserRole.driver), ("other_driver", UserRole.driver),
                               ("passenger", UserRole.passenger), ("other_passenger", UserRole.passenger),
                               ("admin", UserRole.admin)):
                user = User(name=name, email=f"{name}@example.com", cnic=name,
                            phone_number="private-phone", password="private-hash", role=role)
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

    def headers(self, actor):
        user = self.users[actor]
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    async def seed(self, count=1, driver="driver", passenger="passenger"):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            rides, bookings = [], []
            for i in range(count):
                # Equal departure times exercise the ID tie-break; all are past/sold out.
                ride = Ride(driver_id=self.users[driver].id, origin="Peshawar", destination="Islamabad",
                            departure_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
                            available_seats=0, fare_per_seat=500,
                            status=list(RideStatus)[i % 3])
                db.add(ride)
                await db.flush()
                booking = Booking(ride_id=ride.id, passenger_id=self.users[passenger].id,
                                  status=(BookingStatus.pending if ride.status == RideStatus.active
                                          else BookingStatus.accepted if ride.status == RideStatus.completed
                                          else BookingStatus.cancelled))
                db.add(booking)
                rides.append(ride)
                bookings.append(booking)
            await db.commit()
            return rides, bookings

    async def get(self, path, actor="passenger", **params):
        response = await self.client.get(path, headers=self.headers(actor), params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_details_include_history_and_require_authentication(self):
        rides, _ = await self.seed(3)
        for ride in rides:
            for actor in self.users:
                result = await self.get(f"/rides/{ride.id}", actor)
                self.assertEqual(result["status"], ride.status.value)
                self.assertEqual(result["available_seats"], 0)
                self.assertEqual(set(result), {"id", "driver_id", "origin", "destination",
                                              "departure_time", "available_seats", "fare_per_seat", "status"})
        response = await self.client.get("/rides/2147483647", headers=self.headers("passenger"))
        self.assertEqual(response.status_code, 404)
        for path in (f"/rides/{rides[0].id}", "/rides/me", "/bookings/me",
                     f"/rides/{rides[0].id}/bookings"):
            for headers in ({}, {"Authorization": "Bearer malformed"}):
                response = await self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    async def test_private_collections_enforce_role_and_ownership(self):
        rides, _ = await self.seed()
        for actor in ("passenger", "other_passenger", "admin"):
            for path in ("/rides/me", f"/rides/{rides[0].id}/bookings"):
                response = await self.client.get(path, headers=self.headers(actor))
                self.assertEqual(response.status_code, 403)
        for actor in ("driver", "other_driver", "admin"):
            response = await self.client.get("/bookings/me", headers=self.headers(actor))
            self.assertEqual(response.status_code, 403)
        response = await self.client.get(f"/rides/{rides[0].id}/bookings", headers=self.headers("other_driver"))
        self.assertEqual(response.status_code, 403)
        response = await self.client.get("/rides/2147483647/bookings", headers=self.headers("driver"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(await self.get("/rides/me", "other_driver"), [])
        self.assertEqual(await self.get("/bookings/me", "other_passenger"), [])

    async def test_my_rides_pagination_order_history_and_status(self):
        rides, _ = await self.seed(105)
        await self.seed(2, driver="other_driver")
        ids = [ride.id for ride in reversed(rides)]
        for params, expected in (({}, ids[:20]), ({"limit": 100}, ids[:100]),
                                 ({"limit": 3, "offset": 2}, ids[2:5]), ({"offset": 105}, [])):
            result = await self.get("/rides/me", "driver", **params)
            self.assertEqual([item["id"] for item in result], expected)
        for status in RideStatus:
            expected = [ride.id for ride in reversed(rides) if ride.status == status]
            result = await self.get("/rides/me", "driver", status=status.value, offset=1, limit=2)
            self.assertEqual([item["id"] for item in result], expected[1:3])
        async with AsyncSession(self.engine) as db:
            first = await db.get(Ride, rides[0].id)
            first.departure_time += timedelta(days=1)
            await db.commit()
        self.assertEqual((await self.get("/rides/me", "driver", limit=1))[0]["id"], rides[0].id)

    async def test_passenger_history_pagination_nested_rides_and_status(self):
        rides, bookings = await self.seed(105)
        await self.seed(2, passenger="other_passenger")
        async with AsyncSession(self.engine) as db:
            booking = await db.get(Booking, bookings[0].id)
            booking.status = BookingStatus.rejected
            await db.commit()
        bookings[0].status = BookingStatus.rejected
        ids = [booking.id for booking in reversed(bookings)]
        for params, expected in (({}, ids[:20]), ({"limit": 100}, ids[:100]),
                                 ({"limit": 2, "offset": 1}, ids[1:3]), ({"offset": 105}, [])):
            result = await self.get("/bookings/me", **params)
            self.assertEqual([item["id"] for item in result], expected)
            for item in result:
                self.assertEqual(item["ride_id"], item["ride"]["id"])
                self.assertEqual(item["passenger_id"], self.users["passenger"].id)
                self.assertEqual(set(item), {"id", "ride_id", "passenger_id", "status", "ride"})
        for status in BookingStatus:
            expected = [b.id for b in reversed(bookings) if b.status == status]
            result = await self.get("/bookings/me", status=status.value, limit=100)
            self.assertEqual([item["id"] for item in result], expected)

    async def test_driver_requests_history_privacy_pagination_and_status(self):
        rides, bookings = await self.seed()
        ride = rides[0]
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Multiple historical rows are valid; one other passenger's accepted booking is live.
            extra = [Booking(ride_id=ride.id, passenger_id=self.users["passenger"].id,
                             status=BookingStatus.rejected if i % 2 else BookingStatus.cancelled)
                     for i in range(103)]
            extra.append(Booking(ride_id=ride.id, passenger_id=self.users["other_passenger"].id,
                                 status=BookingStatus.accepted))
            db.add_all(extra)
            await db.commit()
        bookings += extra
        await self.seed()  # Another ride's booking must never leak into this list.
        path = f"/rides/{ride.id}/bookings"
        ids = [b.id for b in reversed(bookings)]
        for params, expected in (({}, ids[:20]), ({"limit": 100}, ids[:100]),
                                 ({"limit": 3, "offset": 1}, ids[1:4]), ({"offset": 105}, [])):
            result = await self.get(path, "driver", **params)
            self.assertEqual([item["id"] for item in result], expected)
            for item in result:
                self.assertEqual(set(item), {"id", "ride_id", "passenger_id", "status", "passenger"})
                self.assertEqual(set(item["passenger"]), {"id", "name"})
                self.assertEqual(item["passenger"]["id"], item["passenger_id"])
        for status in BookingStatus:
            expected = [b.id for b in reversed(bookings) if b.status == status]
            result = await self.get(path, "driver", status=status.value, offset=0, limit=100)
            self.assertEqual([item["id"] for item in result], expected)
        # Historical ride states impose no search-style visibility cutoff.
        for status in (RideStatus.completed, RideStatus.cancelled):
            async with AsyncSession(self.engine) as db:
                stored = await db.get(Ride, ride.id)
                stored.status = status
                for booking in (await db.execute(select(Booking).where(
                    Booking.ride_id == ride.id, Booking.status.in_([BookingStatus.pending, BookingStatus.accepted]),
                ))).scalars():
                    booking.status = BookingStatus.cancelled
                await db.commit()
            self.assertEqual(len(await self.get(path, "driver")), 20)

    async def test_collection_validation(self):
        rides, _ = await self.seed()
        for path, actor in (("/rides/me", "driver"), ("/bookings/me", "passenger"),
                            (f"/rides/{rides[0].id}/bookings", "driver")):
            for params in ({"limit": 0}, {"limit": 101}, {"limit": -1}, {"limit": "bad"},
                           {"offset": -1}, {"offset": 2**63}, {"offset": "bad"}, {"status": "invalid"}):
                response = await self.client.get(path, headers=self.headers(actor), params=params)
                self.assertEqual(response.status_code, 422, response.text)

    async def test_me_contract_and_privacy(self):
        for actor, user in self.users.items():
            self.assertEqual(await self.get("/me", actor), {
                "id": user.id, "name": user.name, "email": user.email, "role": user.role.value,
            })


class FrontendOpenAPITests(unittest.TestCase):
    def test_new_read_contracts_and_me_schema(self):
        spec = app.openapi()
        expected = {"/rides/me": "RideResponse", "/rides/{ride_id}": "RideResponse",
                    "/bookings/me": "PassengerBookingResponse",
                    "/rides/{ride_id}/bookings": "DriverBookingResponse", "/me": "UserResponse"}
        for path, model in expected.items():
            operation = spec["paths"][path]["get"]
            self.assertEqual(operation["security"], [{"HTTPBearer": []}])
            schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            if path not in ("/me", "/rides/{ride_id}"):
                self.assertEqual(schema["type"], "array")
                schema = schema["items"]
                params = {p["name"]: p["schema"] for p in operation["parameters"]}
                self.assertEqual(params["limit"]["default"], 20)
                self.assertEqual(params["limit"]["maximum"], 100)
                self.assertEqual(params["offset"]["minimum"], 0)
                self.assertIn("status", params)
            self.assertEqual(schema["$ref"], f"#/components/schemas/{model}")
        self.assertEqual(set(spec["components"]["schemas"]["PassengerSummary"]["properties"]), {"id", "name"})
        paths = [route.path for route in app.routes]
        self.assertLess(paths.index("/rides/me"), paths.index("/rides/{ride_id}"))
