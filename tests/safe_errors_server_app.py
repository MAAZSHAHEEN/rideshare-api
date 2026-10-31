"""ASGI app for the real-Uvicorn log-leak test in test_safe_errors.py.

Wraps the production ``main.app`` and replaces the database dependency with
one that raises errors whose text carries obviously fake sensitive markers,
the way PostgreSQL DETAIL and DSN-bearing connection errors do.
"""

from fastapi import Request
from sqlalchemy.exc import IntegrityError

from database import get_db
from main import app

FAKE_CNIC = "99999-9999999-1"
FAKE_PHONE = "+923009999991"
FAKE_DSN_PASSWORD = "fake-dsn-password-not-real-0001"
FAKE_TOKEN = "fake.jwt.token-not-real-0001"


class _FakeUniqueViolation(Exception):
    sqlstate = "23505"
    constraint_name = "uq_fake_unmapped_phone"


class _FakeAdaptedError(Exception):
    sqlstate = "23505"


async def failing_db(request: Request):
    if request.headers.get("x-fail-kind") == "integrity":
        cause = _FakeUniqueViolation(
            f"duplicate key value violates unique constraint \"uq_fake_unmapped_phone\"\n"
            f"DETAIL:  Key (phone_number)=({FAKE_PHONE}) already exists. cnic={FAKE_CNIC}"
        )
        orig = _FakeAdaptedError(str(cause))
        orig.__cause__ = cause
        raise IntegrityError("INSERT INTO users ...", {"cnic": FAKE_CNIC, "phone_number": FAKE_PHONE}, orig)
    raise RuntimeError(
        f"could not connect to postgresql://rideshare:{FAKE_DSN_PASSWORD}@db.internal/rideshare "
        f"token={FAKE_TOKEN} cnic={FAKE_CNIC}"
    )
    yield  # pragma: no cover - makes this a generator dependency like get_db


app.dependency_overrides[get_db] = failing_db
