"""Validation privacy tests: no database connections or account creation."""

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, field_validator

with patch.dict(os.environ, {
    "DATABASE_URL": "postgresql+asyncpg://localhost/validation_tests_unused",
    "SECRET_KEY": "validation-tests-only-not-a-production-signing-key",
}):
    from main import app
    from database import get_db

from validation_errors import request_validation_error_handler


class NestedInput(BaseModel):
    password: str
    tokens: list[int]


class Envelope(BaseModel):
    credentials: list[NestedInput]


class CustomInput(BaseModel):
    secret: str

    @field_validator("secret")
    @classmethod
    def reject(cls, value):
        raise ValueError(f"Rejected submitted secret: {value}")


class ValidationPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def unused_database():
            yield None  # A malformed request must fail before route SQL executes.

        overrides = patch.dict(app.dependency_overrides, {get_db: unused_database})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def assert_safe(self, response, markers):
        self.assertEqual(response.status_code, 422)
        for marker in markers:
            self.assertFalse(marker in response.text, "Submitted value leaked")
        errors = response.json()["detail"]
        self.assertTrue(errors)
        for error in errors:
            self.assertEqual(set(error), {"type", "loc", "msg"})
            self.assertTrue(error["msg"])
        return errors

    async def test_registration_password_bounds_and_blank(self):
        for value, kind in (("tiny", "string_too_short"), ("x" * 73, "string_too_long"),
                            (" " * 12, "value_error"), ("é" * 37, "value_error")):
            with self.subTest(kind=kind):
                response = await self.client.post("/auth/register", json={
                    "name": "Audit", "email": "audit@example.com", "password": value,
                    "cnic": "private-cnic-marker", "phone_number": "private-phone-marker",
                })
                errors = self.assert_safe(response, [value, "private-cnic-marker", "private-phone-marker"])
                self.assertEqual(errors[0]["loc"], ["body", "password"])
                self.assertEqual(errors[0]["type"], kind)
                self.assertIn("72" if len(value.encode()) > 72 else ("blank" if not value.strip() else "12"), errors[0]["msg"])

    async def test_registration_nested_sensitive_values(self):
        private = {"password": "private-password-marker", "token": "private-token-marker",
                   "secret": "private-secret-marker", "cnic": "private-cnic-marker",
                   "phone_number": "private-phone-marker"}
        response = await self.client.post("/auth/register", json={
            "name": private, "email": "audit@example.com", "password": private,
            "cnic": private, "phone_number": private,
        })
        errors = self.assert_safe(response, list(private.values()))
        self.assertEqual({e["loc"][-1] for e in errors}, {"name", "password", "cnic", "phone_number"})
        self.assertTrue(all(e["type"] == "string_type" for e in errors))

    async def test_login_validation_does_not_echo_email_or_password(self):
        response = await self.client.post("/auth/login", json={
            "email": "private-email-marker", "password": {"token": "private-token-marker"},
        })
        errors = self.assert_safe(response, ["private-email-marker", "private-token-marker"])
        self.assertEqual([e["loc"] for e in errors], [["body", "email"], ["body", "password"]])
        self.assertEqual(errors[0]["msg"], "Value is not a valid email address")

    async def test_missing_field_does_not_echo_whole_body(self):
        for path in ("/auth/register", "/auth/login"):
            with self.subTest(path=path):
                response = await self.client.post(path, json={"password": "private-password-marker"})
                errors = self.assert_safe(response, ["private-password-marker"])
                self.assertTrue(all(e["type"] == "missing" for e in errors))
                self.assertTrue(all(e["msg"] == "Field required" for e in errors))

    async def test_invalid_role_keeps_location_and_type(self):
        response = await self.client.post("/auth/register", json={
            "name": "Audit", "email": "audit@example.com", "password": "private-password-marker",
            "cnic": "private-cnic-marker", "phone_number": "private-phone-marker",
            "role": "private-role-marker",
        })
        errors = self.assert_safe(response, ["private-role-marker", "private-password-marker",
                                             "private-cnic-marker", "private-phone-marker"])
        self.assertEqual(errors, [{"type": "literal_error", "loc": ["body", "role"],
                                   "msg": "Input should be one of the permitted values"}])

    async def test_query_bounds_keep_useful_messages(self):
        isolated = FastAPI()
        isolated.add_exception_handler(RequestValidationError, request_validation_error_handler)

        @isolated.get("/bounded")
        def bounded(limit: int = Query(default=20, ge=1, le=100)):
            return {"limit": limit}

        async with AsyncClient(transport=ASGITransport(app=isolated), base_url="http://test") as client:
            for value, kind, message in ((0, "greater_than_equal", "Input should be greater than or equal to 1"),
                                          (101, "less_than_equal", "Input should be less than or equal to 100")):
                response = await client.get("/bounded", params={"limit": value})
                errors = self.assert_safe(response, [])
                self.assertEqual(errors, [{"type": kind, "loc": ["query", "limit"], "msg": message}])

    async def test_malformed_json_keeps_position_without_body(self):
        for path in ("/auth/register", "/auth/login"):
            with self.subTest(path=path):
                response = await self.client.post(path, content='{"password":"private-json-marker",',
                                                  headers={"Content-Type": "application/json"})
                errors = self.assert_safe(response, ["private-json-marker"])
                self.assertEqual(errors[0]["type"], "json_invalid")
                self.assertEqual(errors[0]["loc"][0], "body")
                self.assertIsInstance(errors[0]["loc"][1], int)

    async def test_nested_locations_and_custom_validator_context(self):
        isolated = FastAPI()
        isolated.add_exception_handler(RequestValidationError, request_validation_error_handler)

        @isolated.post("/nested")
        def nested(data: Envelope):
            return {"ok": True}

        @isolated.post("/custom")
        def custom(data: CustomInput):
            return {"ok": True}

        async with AsyncClient(transport=ASGITransport(app=isolated), base_url="http://test") as client:
            response = await client.post("/nested", json={"credentials": [{
                "password": {"secret": "private-nested-marker"}, "tokens": ["private-token-marker"],
            }]})
            errors = self.assert_safe(response, ["private-nested-marker", "private-token-marker"])
            self.assertEqual([e["loc"] for e in errors], [
                ["body", "credentials", 0, "password"], ["body", "credentials", 0, "tokens", 0],
            ])
            response = await client.post("/custom", json={"secret": "private-custom-marker"})
            errors = self.assert_safe(response, ["private-custom-marker"])
            self.assertEqual(errors, [{"type": "value_error", "loc": ["body", "secret"], "msg": "Invalid value"}])
            response = await client.post("/nested", json={"credentials": [{"password": "valid", "tokens": [1]}]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
