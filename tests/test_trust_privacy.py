"""CNIC/phone masking, response-schema privacy guard, and honest verification status."""

import os
import re
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "trust-privacy-tests-only-not-a-production-key",
}):
    from main import app
    from models import User, UserRole
    from privacy import (
        CNIC_FULLY_MASKED, EMAIL_FULLY_MASKED, PHONE_FULLY_MASKED, mask_cnic, mask_email, mask_phone,
    )
    from verification import (
        DRIVER_NOT_VERIFIED_LABEL, DriverVerificationStatus, driver_verification_status,
        is_verified_driver,
    )

# Obviously fake values only.
FAKE_CNIC = "12345-6789012-3"
FAKE_PHONE = "+923001234567"


class EmailMaskingTests(unittest.TestCase):
    def test_only_first_character_and_domain_shown(self):
        self.assertEqual(mask_email("ayesha.khan@example.com"), "a***@example.com")
        self.assertEqual(mask_email("  Z@example.org "), "Z***@example.org")
        self.assertNotIn("yesha", mask_email("ayesha.khan@example.com"))

    def test_unrecognised_values_are_fully_masked(self):
        for value in ("", "no-at-sign", "@example.com", "user@", "ü@example.com"):
            with self.subTest(value=value):
                self.assertEqual(mask_email(value), EMAIL_FULLY_MASKED)

    def test_none_passes_through(self):
        self.assertIsNone(mask_email(None))


class CnicMaskingTests(unittest.TestCase):
    def test_canonical_and_hyphenated_forms(self):
        for value in ("1234567890123", "12345-6789012-3", "  12345-6789012-3 ", "123456789012-3"):
            with self.subTest(value=value):
                self.assertEqual(mask_cnic(value), "*****-****012-*")

    def test_region_and_gender_digits_never_shown(self):
        masked = mask_cnic("98765-4321098-7")
        self.assertEqual(masked, "*****-****098-*")
        self.assertNotIn("98765", masked)       # region prefix
        self.assertFalse(masked.endswith("7"))  # gender digit
        self.assertEqual(len(re.findall(r"[0-9]", masked)), 3)

    def test_unrecognised_values_are_fully_masked(self):
        for value in ("", "   ", "fake-cnic-driver", "12345", "12345-678901-23", "1234-56789012-34",
                      "12345678901234", "12345--6789012-3", "١٢٣٤٥٦٧٨٩٠١٢٣", "12345-678901٢-3", "12345 6789012 3"):
            with self.subTest(value=value):
                self.assertEqual(mask_cnic(value), CNIC_FULLY_MASKED)

    def test_none_passes_through(self):
        self.assertIsNone(mask_cnic(None))


class PhoneMaskingTests(unittest.TestCase):
    def test_accepted_mobile_spellings(self):
        for value in ("+923001234567", "923001234567", "03001234567", "0300-1234567",
                      "+92 300 1234567", " 0300 123 4567 "):
            with self.subTest(value=value):
                self.assertEqual(mask_phone(value), "+92 3** *** **67")

    def test_only_last_two_digits_shown(self):
        masked = mask_phone("+923219876543")
        self.assertEqual(len(re.findall(r"[0-9]", masked)), 5)  # fixed "92" + "3", then "43"
        self.assertTrue(masked.endswith("43"))
        self.assertNotIn("321", masked)

    def test_unrecognised_values_are_fully_masked(self):
        for value in ("", "fake-phone", "private-phone", "0421234567", "+92421234567",
                      "00923001234567", "+920001234567", "0300123456", "030012345678",
                      "+1 415 555 0100", "٠٣٠٠١٢٣٤٥٦٧", "+923٠٠١٢٣٤٥٦٧"):
            with self.subTest(value=value):
                self.assertEqual(mask_phone(value), PHONE_FULLY_MASKED)

    def test_none_passes_through(self):
        self.assertIsNone(mask_phone(None))


SENSITIVE_PROPERTY = re.compile(r"cnic|phone|password|national_id", re.IGNORECASE)
# Response models allowed to carry a sensitive-looking property, mapped to the reason.
# Any entry here must serve masked values (privacy.mask_*) to non-owners.
MASKED_RESPONSE_ALLOWLIST: dict[str, str] = {
    "AdminUserResponse": "admin-only; cnic_masked/phone_masked are built by privacy.mask_* "
                         "(asserted in tests/test_account_suspension.py)",
}


def _response_schema_names(spec):
    names = set()

    def collect(schema):
        if isinstance(schema, dict):
            ref = schema.get("$ref")
            if ref:
                name = ref.rsplit("/", 1)[-1]
                if name not in names:
                    names.add(name)
                    collect(spec["components"]["schemas"][name])
            for value in schema.values():
                collect(value)
        elif isinstance(schema, list):
            for value in schema:
                collect(value)

    for operations in spec["paths"].values():
        for operation in operations.values():
            for response in operation.get("responses", {}).values():
                collect(response.get("content", {}))
    return names


def _leaks(spec, pattern, allowlist=()):
    return sorted(
        f"{name}.{prop}" for name in _response_schema_names(spec) - set(allowlist)
        for prop in spec["components"]["schemas"][name].get("properties", {})
        if pattern.search(prop)
    )


class ResponsePrivacyGuardTests(unittest.TestCase):
    def test_no_response_model_exposes_cnic_phone_or_password(self):
        spec = app.openapi()
        names = _response_schema_names(spec)
        self.assertTrue({"UserResponse", "RideResponse", "DriverBookingResponse"} <= names)
        self.assertEqual(_leaks(spec, SENSITIVE_PROPERTY, MASKED_RESPONSE_ALLOWLIST), [])

    def test_guard_detects_a_leaking_model(self):
        spec = {"paths": {"/x": {"get": {"responses": {"200": {"content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Leaky"}}}}}}}},
                "components": {"schemas": {
                    "Leaky": {"properties": {"id": {}, "owner": {"$ref": "#/components/schemas/Owner"}}},
                    "Owner": {"properties": {"phone_number": {}}}}}}
        self.assertEqual(_response_schema_names(spec), {"Leaky", "Owner"})
        self.assertEqual(_leaks(spec, SENSITIVE_PROPERTY), ["Owner.phone_number"])
        self.assertEqual(_leaks(spec, SENSITIVE_PROPERTY, {"Owner"}), [])


class DriverVerificationHonestyTests(unittest.TestCase):
    def test_every_driver_is_not_verified(self):
        for role in UserRole:
            driver = User(id=1, name="Test", email="t@example.com", cnic=FAKE_CNIC,
                          phone_number=FAKE_PHONE, password="x", role=role)
            with self.subTest(role=role):
                self.assertIs(driver_verification_status(driver), DriverVerificationStatus.not_verified)
                self.assertFalse(is_verified_driver(driver))

    def test_label_is_honest(self):
        self.assertEqual(DRIVER_NOT_VERIFIED_LABEL, "Driver documents not verified")

    def test_no_response_model_claims_verification(self):
        # Until a persisted review decision exists, no API field may suggest verification.
        spec = app.openapi()
        self.assertEqual(_leaks(spec, re.compile(r"verif|badge|trusted", re.IGNORECASE)), [])


if __name__ == "__main__":
    unittest.main()
