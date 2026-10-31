"""AST-007 input validation: CNIC / PK mobile normalisation, string and numeric bounds,
sanitised 422 bodies, no sensitive values in logs, legacy-row duplicate checks.

All CNIC and phone values here are obviously fake (zero-filled / repeated digits).
The DB-backed class needs TEST_DATABASE_URL (PostgreSQL, CREATE SCHEMA permission).
"""

import asyncio
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import bcrypt
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/input_validation_unused",
    "SECRET_KEY": "input-validation-tests-only-not-a-production-key",
}):
    import database
    from database import get_db
    from main import app
    from models import User, UserRole
    from routers.dependencies import get_current_user
    from schemas import (
        CNIC_ERROR, FARE_PER_SEAT_MAX, NAME_MAX_LENGTH, PHONE_ERROR, PLACE_MAX_LENGTH,
        RIDE_SEATS_MAX, RIDE_SEATS_MIN, SAME_PLACE_ERROR, RideCreate, UserCreate, clean_text, normalize_cnic,
        normalize_pk_mobile,
    )


FAKE_CNIC = "00000-0000000-1"
FAKE_CNIC_CANONICAL = "0000000000001"
FAKE_PHONE = "03000000000"
FAKE_PHONE_E164 = "+923000000000"


def registration(**overrides):
    payload = {
        "name": "Validation Test", "email": f"v-{uuid4().hex}@example.com",
        "password": "Test-password-123!", "cnic": FAKE_CNIC, "phone_number": FAKE_PHONE,
    }
    payload.update(overrides)
    return payload


def ride(**overrides):
    payload = {
        "origin": "Lahore", "destination": "Islamabad",
        "departure_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "available_seats": 3, "fare_per_seat": 2500,
    }
    payload.update(overrides)
    return payload


class NormalisationTests(unittest.TestCase):
    def test_valid_cnic_forms_normalise_to_13_digits(self):
        for raw, expected in (
            ("00000-0000000-1", "0000000000001"),
            ("0000000000001", "0000000000001"),
            ("  99999-9999999-9 \n", "9999999999999"),
            ("\t9999999999999", "9999999999999"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_cnic(raw), expected)

    def test_invalid_cnic_forms_are_rejected(self):
        for raw in (
            "", "   ", "000000000000", "00000000000012",            # 12 / 14 digits
            "00000-00000001", "000000000000-1",                     # only one hyphen
            "0000-00000000-1", "00000-000000-01", "00000-0000000-",  # misplaced hyphens
            "00000 0000000 1", "00000_0000000_1", "00000--0000000-1",
            "0000a00000001", "+000000000001",
            "٠٠٠٠٠٠٠٠٠٠٠٠١",                                        # Arabic-Indic digits
            "０００００００００００００１",                              # full-width digits
            "0000000000001\x00", "00000-0000000-1\n0",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    normalize_cnic(raw)
                self.assertEqual(str(caught.exception), CNIC_ERROR)

    def test_valid_phone_forms_normalise_to_e164(self):
        for raw in ("03000000000", "+923000000000", "923000000000",
                    "0300-0000000", "+92 300 0000000", " 0300 0000000 "):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_pk_mobile(raw), FAKE_PHONE_E164)
        self.assertEqual(normalize_pk_mobile("03459999999"), "+923459999999")

    def test_invalid_phone_forms_are_rejected(self):
        for raw in (
            "", "0300000000", "030000000000",          # 10 / 12 digits
            "04200000000", "+924200000000",            # landline, not mobile
            "3000000000", "00923000000000",            # no prefix / 00 prefix
            "+9203000000000", "+92-0300-0000000",      # trunk 0 kept after country code
            "+13000000000", "92300000000", "+92300000000a",
            "0300.0000000", "(0300)0000000", "٠٣٠٠٠٠٠٠٠٠٠",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    normalize_pk_mobile(raw)
                self.assertEqual(str(caught.exception), PHONE_ERROR)

    def test_user_create_stores_canonical_forms_and_hides_them_in_repr(self):
        user = UserCreate(**registration(name="  Fake Person  "))
        self.assertEqual((user.name, user.cnic, user.phone_number),
                         ("Fake Person", FAKE_CNIC_CANONICAL, FAKE_PHONE_E164))
        text = repr(user) + str(user)
        for secret in (FAKE_CNIC_CANONICAL, FAKE_PHONE_E164, "Test-password-123!"):
            self.assertNotIn(secret, text)

    def test_invisible_display_spoofing_characters_are_rejected(self):
        for char in ("\u202e", "\u202a", "\u2066", "\u2069", "\u200b", "\u2060", "\ufeff", "\u180e"):
            with self.subTest(char=hex(ord(char))):
                with self.assertRaisesRegex(ValueError, "control characters"):
                    clean_text(f"Fake{char}Name")
        # Ordinary Urdu text uses ZWNJ/ZWJ and directional marks; keep accepting them.
        for value in ("\u0639\u0644\u06cc\u200c\u0631\u0636\u0627", "Fake\u200dName",
                      "Lahore \u200f\u0644\u0627\u06c1\u0648\u0631"):
            with self.subTest(value=value):
                self.assertEqual(clean_text(value), value)

    def test_ride_bounds_and_places(self):
        self.assertEqual(RideCreate(**ride(available_seats=RIDE_SEATS_MAX,
                                           fare_per_seat=FARE_PER_SEAT_MAX)).available_seats, 8)
        self.assertEqual(RideCreate(**ride(available_seats=RIDE_SEATS_MIN, fare_per_seat=0)).available_seats, 1)
        for seats in (0, -1):
            with self.subTest(seats=seats), self.assertRaises(ValidationError):
                RideCreate(**ride(available_seats=seats))
        stripped = RideCreate(**ride(origin="  Lahore ", destination=" Rawalpindi"))
        self.assertEqual((stripped.origin, stripped.destination), ("Lahore", "Rawalpindi"))
        for bad in ({"available_seats": RIDE_SEATS_MAX + 1}, {"fare_per_seat": FARE_PER_SEAT_MAX + 1},
                    {"available_seats": -1}, {"fare_per_seat": -1},
                    {"origin": "Lahore", "destination": " lahore "},
                    {"origin": "x" * (PLACE_MAX_LENGTH + 1)}, {"destination": "   "},
                    {"origin": "Lah\x00ore"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                RideCreate(**ride(**bad))


class _CaptureAll(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.text = []

    def emit(self, record):
        # httpx is the in-process test *client* logging its own request line; the
        # server side (app, uvicorn, sqlalchemy, fastapi) is what must stay clean.
        if not record.name.startswith(("httpx", "httpcore")):
            self.text.append(self.format(record))


class SanitisedErrorTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 422 behaviour; no database is touched (validation fails before route SQL)."""

    async def asyncSetUp(self):
        async def unused_database():
            yield None

        async def fake_driver():
            return User(id=1, name="Fake Driver", email="d@example.com",
                        role=UserRole.driver, cnic="unused", phone_number="unused")

        overrides = patch.dict(app.dependency_overrides,
                               {get_db: unused_database, get_current_user: fake_driver})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

        root = logging.getLogger()
        self.capture = _CaptureAll()
        old_level = root.level
        root.addHandler(self.capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, old_level)
        self.addCleanup(root.removeHandler, self.capture)

    def assert_safe_422(self, response, markers, expected):
        self.assertEqual(response.status_code, 422, response.text)
        errors = response.json()["detail"]
        for error in errors:
            self.assertEqual(set(error), {"type", "loc", "msg"})
        logged = "\n".join(self.capture.text)
        for marker in markers:
            self.assertNotIn(marker, response.text, "submitted value echoed in 422 body")
            self.assertNotIn(marker, logged, "submitted value written to logs")
        self.assertEqual(errors, expected)

    async def test_malformed_cnic_and_phone_get_fixed_messages_without_echo(self):
        # Each value resembles a real identifier closely enough that echoing it would matter.
        for cnic, phone in (("00000-0000000-12", "0300000000099"),
                            ("12345-1234567", "+92 42 0000000"),
                            ("9" * 32, "0" * 32)):
            with self.subTest(cnic=cnic):
                response = await self.client.post("/auth/register", json=registration(
                    cnic=cnic, phone_number=phone, password="private-password-marker"))
                self.assert_safe_422(response, [cnic, phone, "private-password-marker"], [
                    {"type": "value_error", "loc": ["body", "cnic"], "msg": "Value error, " + CNIC_ERROR},
                    {"type": "value_error", "loc": ["body", "phone_number"],
                     "msg": "Value error, " + PHONE_ERROR},
                ])

    async def test_overlong_sensitive_values_fail_on_length_without_echo(self):
        cnic, phone = "1" * 33, "2" * 33
        response = await self.client.post("/auth/register", json=registration(
            cnic=cnic, phone_number=phone))
        self.assert_safe_422(response, [cnic, phone], [
            {"type": "string_too_long", "loc": ["body", "cnic"],
             "msg": "String should have at most 32 characters"},
            {"type": "string_too_long", "loc": ["body", "phone_number"],
             "msg": "String should have at most 32 characters"},
        ])

    async def test_name_boundaries(self):
        marker = "N" * (NAME_MAX_LENGTH + 1)
        response = await self.client.post("/auth/register", json=registration(name=marker))
        self.assert_safe_422(response, [marker], [{
            "type": "string_too_long", "loc": ["body", "name"],
            "msg": f"String should have at most {NAME_MAX_LENGTH} characters"}])
        for name, msg in (("   ", "Value error, Value must not be blank"),
                          ("Fake\x1bName", "Value error, Value must not contain control characters"),
                          ("Fake\u202eemaN", "Value error, Value must not contain control characters")):
            with self.subTest(name=name):
                response = await self.client.post("/auth/register", json=registration(name=name))
                self.assert_safe_422(response, [], [
                    {"type": "value_error", "loc": ["body", "name"], "msg": msg}])

    async def test_ride_create_bounds(self):
        for field, value, kind, msg in (
            ("available_seats", RIDE_SEATS_MAX + 1, "less_than_equal",
             f"Input should be less than or equal to {RIDE_SEATS_MAX}"),
            ("fare_per_seat", FARE_PER_SEAT_MAX + 1, "less_than_equal",
             f"Input should be less than or equal to {FARE_PER_SEAT_MAX}"),
            ("origin", "o" * (PLACE_MAX_LENGTH + 1), "string_too_long",
             f"String should have at most {PLACE_MAX_LENGTH} characters"),
            ("destination", "   ", "value_error", "Value error, Value must not be blank"),
            ("available_seats", 0, "greater_than_equal", "Input should be greater than or equal to 1"),
        ):
            with self.subTest(field=field):
                response = await self.client.post("/rides/", json=ride(**{field: value}))
                self.assert_safe_422(response, [], [{"type": kind, "loc": ["body", field], "msg": msg}])

    async def test_origin_equal_to_destination_is_rejected(self):
        response = await self.client.post("/rides/", json=ride(origin="Lahore", destination=" LAHORE "))
        self.assert_safe_422(response, [], [
            {"type": "value_error", "loc": ["body"], "msg": "Value error, " + SAME_PLACE_ERROR}])

    async def test_search_query_place_length_is_bounded(self):
        marker = "q" * (PLACE_MAX_LENGTH + 1)
        for param in ("origin", "destination"):
            with self.subTest(param=param):
                response = await self.client.get("/rides/", params={param: marker})
                self.assert_safe_422(response, [marker], [{
                    "type": "string_too_long", "loc": ["query", param],
                    "msg": f"String should have at most {PLACE_MAX_LENGTH} characters"}])


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class LegacyIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(TEST_DATABASE_URL).drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        # Same parameter hiding as database.engine, so the log assertions below
        # exercise application code rather than this test's own engine.
        engine = create_async_engine(TEST_DATABASE_URL, hide_parameters=True)
        self.addAsyncCleanup(engine.dispose)
        self.connection = await engine.connect()
        self.addAsyncCleanup(self.connection.close)
        transaction = await self.connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        schema = f"test_input_validation_{uuid4().hex}"
        await self.connection.execute(CreateSchema(schema))
        await self.connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        await self.connection.run_sync(User.__table__.create)

        async def override_get_db():
            async with AsyncSession(bind=self.connection, expire_on_commit=False,
                                    join_transaction_mode="create_savepoint") as session:
                yield session

        overrides = patch.dict(app.dependency_overrides, {get_db: override_get_db})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def insert_legacy(self, cnic, phone="0300-0000000", password="Legacy-pass1"):
        email = f"legacy-{uuid4().hex}@example.com"
        hashed = await asyncio.to_thread(
            lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode())
        await self.connection.execute(insert(User.__table__).values(
            name="Legacy", email=email, password=hashed, cnic=cnic, phone_number=phone,
            role=UserRole.passenger))
        return email, password

    async def user_count(self):
        return await self.connection.scalar(select(func.count()).select_from(User))

    async def test_new_registration_stores_canonical_cnic_and_phone(self):
        payload = registration(cnic="00000-0000000-7", phone_number="+92 300 0000007")
        capture = _CaptureAll()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)  # application/framework loggers; SQL echo: see hide_parameters test
        try:
            response = await self.client.post("/auth/register", json=payload)
        finally:
            root.removeHandler(capture)
            root.setLevel(old_level)
        self.assertEqual(response.status_code, 201, response.text)
        logged = "\n".join(capture.text)
        for value in ("0000000000007", "00000-0000000-7", "+923000000007", "300 0000007",
                      payload["password"]):
            self.assertNotIn(value, logged, "sensitive value written to logs")
        self.assertNotIn("cnic", response.json())
        self.assertNotIn("phone_number", response.json())
        row = (await self.connection.execute(select(User.cnic, User.phone_number).where(
            User.email == payload["email"]))).one()
        self.assertEqual(tuple(row), ("0000000000007", "+923000000007"))

    async def test_same_cnic_in_either_new_format_conflicts(self):
        first = await self.client.post("/auth/register", json=registration(cnic="0000000000008"))
        self.assertEqual(first.status_code, 201, first.text)
        second = await self.client.post("/auth/register", json=registration(cnic="00000-0000000-8"))
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(second.json(), {"detail": "Registration details already in use"})
        self.assertEqual(await self.user_count(), 1)

    async def test_legacy_non_canonical_cnic_rows_still_block_duplicates(self):
        for legacy, submitted in (
            ("00000-0000001-1", "0000000000011"),     # legacy hyphenated, new digits
            ("00000-0000002-2", "00000-0000002-2"),   # legacy hyphenated, same spelling
            ("00000 0000003 3", "00000-0000003-3"),   # legacy free-form spaces
            (" 0000000000044 ", "0000000000044"),     # legacy untrimmed
        ):
            with self.subTest(legacy=legacy):
                await self.insert_legacy(legacy)
                before = await self.user_count()
                response = await self.client.post("/auth/register", json=registration(cnic=submitted))
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json(), {"detail": "Registration details already in use"})
                self.assertEqual(await self.user_count(), before)

    async def test_legacy_rows_with_unrelated_or_garbage_cnic_do_not_block(self):
        await self.insert_legacy("legacy-free-text")
        await self.insert_legacy("00000-0000005-5")
        response = await self.client.post("/auth/register", json=registration(cnic="0000000000056"))
        self.assertEqual(response.status_code, 201, response.text)

    async def test_app_engine_hides_bound_parameters_in_errors(self):
        self.assertTrue(database.engine.sync_engine.hide_parameters)
        marker = "00000-0000000-0-private-marker"
        with self.assertRaises(Exception) as caught:
            async with database.engine.connect() as connection:
                # Fails server-side (division by zero) after binding the value, so only
                # SQLAlchemy's "[parameters: ...]" suffix could reveal it. Note: driver
                # coercion errors and PostgreSQL DETAIL text can still contain values;
                # see docs/privacy/CNIC_PHONE_PROTECTION.md.
                await connection.execute(text("SELECT CAST(:value AS text), 1 / 0"), {"value": marker})
        self.assertNotIn(marker, str(caught.exception))
        self.assertIn("hidden", str(caught.exception))

    async def test_legacy_user_with_old_formats_can_still_log_in(self):
        email, password = await self.insert_legacy("legacy cnic 1", phone="0300 000 0009")
        response = await self.client.post("/auth/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        me = await self.client.get("/me", headers={
            "Authorization": f"Bearer {response.json()['access_token']}"})
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(set(me.json()), {"id", "name", "email", "role", "allowed_modes"})


if __name__ == "__main__":
    unittest.main()
