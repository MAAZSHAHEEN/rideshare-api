"""Focused password/JWT/configuration tests; no database or real secrets needed."""

import os
from pathlib import Path
import runpy
import secrets
import unittest
from unittest.mock import patch

from jose import jwt


with patch.dict(os.environ, {
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/rideshare_test"),
    "SECRET_KEY": secrets.token_hex(64),
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
}):
    from routers import auth


class AuthSecurityTests(unittest.TestCase):
    def test_issued_claims_and_configured_duration(self):
        token = auth.create_access_token(42, "driver")
        claims = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        self.assertEqual(set(claims), {"sub", "role", "iat", "exp"})
        self.assertEqual(claims["sub"], "42")
        self.assertEqual(claims["role"], "driver")
        self.assertIs(type(claims["iat"]), int)
        self.assertIs(type(claims["exp"]), int)
        self.assertEqual(claims["exp"] - claims["iat"], auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    def test_issuer_rejects_invalid_user_ids_and_roles(self):
        for user_id in (0, -1, True, "1", 2**31):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    auth.create_access_token(user_id, "passenger")
        with self.assertRaises(ValueError):
            auth.create_access_token(1, "superadmin")

    def test_hash_helper_never_silently_truncates(self):
        for label, password in (("ascii", "a" * 73), ("utf8", "é" * 37)):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    auth.hash_password(password)
                self.assertFalse(auth.verify_password(password, "not-a-hash"))

    def test_bad_stored_hash_fails_cleanly(self):
        self.assertFalse(auth.verify_password("Valid-password-123!", "not-a-hash"))
        self.assertFalse(auth.verify_password("Valid-password-123!", "non-ascii-é"))

    def test_equal_passwords_receive_independent_salts(self):
        first = auth.hash_password("Valid-password-123!")
        second = auth.hash_password("Valid-password-123!")
        self.assertTrue(first != second, "bcrypt must generate independent salts")
        self.assertTrue(auth.verify_password("Valid-password-123!", first))
        self.assertTrue(auth.verify_password("Valid-password-123!", second))


class ConfigurationTests(unittest.TestCase):
    def load_settings(self, **overrides):
        values = {"SECRET_KEY": secrets.token_hex(64), "ALGORITHM": "HS256",
                  "ACCESS_TOKEN_EXPIRE_MINUTES": "60"}
        values.update(overrides)
        values = {key: value for key, value in values.items() if value is not None}
        # No fallback to a developer's .env during isolated configuration checks.
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, values, clear=True):
            return runpy.run_path(str(Path(__file__).resolve().parents[1] / "config.py"))

    def test_missing_blank_short_and_placeholder_secrets_fail_fast(self):
        values = [None, "", " " * 64, "short", "your-secret-key-change-this" * 2,
                  "replace-with-a-random-secret-key" * 2, "changeme" * 10]
        for index, value in enumerate(values):
            with self.subTest(case=index):
                with self.assertRaisesRegex(RuntimeError, "SECRET_KEY") as raised:
                    self.load_settings(SECRET_KEY=value)
                if value:
                    self.assertFalse(value in str(raised.exception), "Errors must not expose signing keys")

    def test_hmac_algorithms_enforce_minimum_key_lengths(self):
        for algorithm, minimum in (("HS256", 32), ("HS384", 48), ("HS512", 64)):
            with self.subTest(algorithm=algorithm):
                key = secrets.token_hex(64)
                self.assertEqual(self.load_settings(ALGORITHM=algorithm, SECRET_KEY=key[:minimum])["ALGORITHM"], algorithm)
                with self.assertRaises(RuntimeError):
                    self.load_settings(ALGORITHM=algorithm, SECRET_KEY=key[:minimum - 1])

    def test_unknown_and_asymmetric_algorithms_are_rejected(self):
        for algorithm in ("none", "RS256", "ES256", "invalid", ""):
            with self.subTest(algorithm=algorithm):
                with self.assertRaisesRegex(RuntimeError, "ALGORITHM"):
                    self.load_settings(ALGORITHM=algorithm)

    def test_expiration_must_be_a_positive_integer(self):
        for value in ("0", "-1", "abc", "1.5", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "ACCESS_TOKEN_EXPIRE_MINUTES"):
                    self.load_settings(ACCESS_TOKEN_EXPIRE_MINUTES=value)
        self.assertEqual(self.load_settings(ACCESS_TOKEN_EXPIRE_MINUTES="15")["ACCESS_TOKEN_EXPIRE_MINUTES"], 15)


if __name__ == "__main__":
    unittest.main()
