"""python-jose -> PyJWT migration (AST-008): tokens issued before the switch keep working.

The GOLDEN tokens were produced once with python-jose 3.5.0 (the library the API
used before AST-008) and are frozen here, so these tests do not need python-jose.
The key is a fake, test-only value; exp is 2038-01-19 so the vectors stay valid.
"""

import base64
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch

import jwt

with patch.dict(os.environ, {
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/rideshare_test"),
    "SECRET_KEY": secrets.token_hex(64),
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
}):
    from routers import auth, dependencies
    from models import UserRole

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_KEY = "test-only-golden-vector-key-0123456789abcdef-not-a-secret-pad-64"
GOLDEN_IAT = 1767225600  # 2026-01-01T00:00:00Z
GOLDEN_EXP = 2**31 - 1   # 2038-01-19T03:14:07Z
# (current format: sub, role=driver, iat, exp;  pre-hardening format: sub, role=passenger, exp)
GOLDEN = {
    "HS256": (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJkcml2ZXIiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6MjE0NzQ4MzY0N30.TGGsAPJczOXk74jIBgxwFHCmjg_GJ01SrzXnH5WiSnk",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJwYXNzZW5nZXIiLCJleHAiOjIxNDc0ODM2NDd9.tMOVkfGAInUw_baj-Kb2xGvt3ti3NKv4-Y4yRV_LBaU",
    ),
    "HS384": (
        "eyJhbGciOiJIUzM4NCIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJkcml2ZXIiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6MjE0NzQ4MzY0N30.12mdDxudjITi5z7a16pNBhwpxEPhTChky0cIjPreVJwTKmAE77IAij55r56eyXh7",
        "eyJhbGciOiJIUzM4NCIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJwYXNzZW5nZXIiLCJleHAiOjIxNDc0ODM2NDd9.GKWgI9yXir_6FNePs5RUR4D6aansqjHRhT1EiBI88LZqt3sMtAcw-2wdHCp7cIdv",
    ),
    "HS512": (
        "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJkcml2ZXIiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6MjE0NzQ4MzY0N30.kTbzyF5F6ZxXIaYOECxqp68j5jUhEV3tbw_tuNb6hWKpcKCq8YJyMRSTAASZrvsmjmmNj9wLkTvefQBG6Da5WA",
        "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJwYXNzZW5nZXIiLCJleHAiOjIxNDc0ODM2NDd9.H-Jd3PCQOjqx9oGRXLgNzwdf5E7TmooafIf2BLWwqQY135_6iBIbjYNpUWNN3bvdCsoruuLgjo9-LH-XldWClQ",
    ),
}


def b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


class JoseIssuedTokenCompatibilityTests(unittest.TestCase):
    def configured(self, algorithm="HS256", key=GOLDEN_KEY):
        return patch.multiple(dependencies, SECRET_KEY=key, ALGORITHM=algorithm)

    def test_jose_issued_tokens_verify_for_every_supported_algorithm(self):
        for algorithm, (current, legacy) in GOLDEN.items():
            with self.subTest(algorithm=algorithm), self.configured(algorithm):
                self.assertEqual(dependencies.decode_access_token(current), (42, UserRole.driver))
                self.assertEqual(dependencies.decode_access_token(legacy), (42, UserRole.passenger))

    def test_pyjwt_encoding_is_byte_identical_to_jose(self):
        claims = {
            "sub": "42", "role": "driver",
            "iat": datetime.fromtimestamp(GOLDEN_IAT, timezone.utc),
            "exp": datetime.fromtimestamp(GOLDEN_EXP, timezone.utc),
        }
        for algorithm, (current, _) in GOLDEN.items():
            with self.subTest(algorithm=algorithm):
                self.assertEqual(jwt.encode(claims, GOLDEN_KEY, algorithm=algorithm), current)

    def test_issuer_keeps_header_claims_and_expiry(self):
        token = auth.create_access_token(7, "passenger")
        self.assertEqual(jwt.get_unverified_header(token), {"alg": "HS256", "typ": "JWT"})
        claims = jwt.decode(token, options={"verify_signature": False})
        # Milestone D adds `tv` (token_version); golden tokens without it still verify.
        self.assertEqual(list(claims), ["sub", "role", "tv", "iat", "exp"])
        self.assertEqual(claims["tv"], 0)
        self.assertEqual(claims["exp"] - claims["iat"], auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        self.assertEqual(dependencies.decode_access_token(token), (7, UserRole.passenger))

    def test_golden_token_rejected_with_wrong_key_or_unpinned_algorithm(self):
        current = GOLDEN["HS256"][0]
        with self.configured(key=GOLDEN_KEY[:-1] + "X"), self.assertRaises(dependencies.InvalidAccessToken):
            dependencies.decode_access_token(current)
        # An HS256 token must not verify when the server is configured for HS512.
        with self.configured("HS512"), self.assertRaises(dependencies.InvalidAccessToken):
            dependencies.decode_access_token(current)

    def test_tampered_unsigned_and_malformed_tokens_rejected(self):
        header, payload, signature = GOLDEN["HS256"][1].split(".")
        forged_payload = b64({"sub": "42", "role": "admin", "exp": GOLDEN_EXP})
        candidates = (
            f"{header}.{forged_payload}.{signature}",
            f"{b64({'alg': 'none', 'typ': 'JWT'})}.{payload}.",
            f"{header}.{payload}",
            "", "not-a-jwt", f"{header}.{payload}.{signature}x",
        )
        with self.configured():
            for token in candidates:
                with self.subTest(token=token[:40]), self.assertRaises(dependencies.InvalidAccessToken):
                    dependencies.decode_access_token(token)

    def test_claim_boundaries_still_enforced(self):
        now = int(datetime.now(timezone.utc).timestamp())
        base = {"sub": "42", "role": "driver", "exp": now + 60}
        bad_claims = (
            {k: v for k, v in base.items() if k != "sub"},
            {k: v for k, v in base.items() if k != "role"},
            {k: v for k, v in base.items() if k != "exp"},
            {**base, "exp": now},                     # expires exactly now
            {**base, "exp": now - 1},
            {**base, "sub": 42},                      # non-string subject
            {**base, "sub": "0"}, {**base, "sub": "042"}, {**base, "sub": str(2**31)},
            {**base, "role": "superadmin"},
            {**base, "iat": now + 60},                # issued in the future
            {**base, "iat": base["exp"]},
            {**base, "nbf": now + 60},
            {**base, "aud": "someone-else"},
        )
        with self.configured():
            for claims in bad_claims:
                with self.subTest(claims=claims), self.assertRaises(dependencies.InvalidAccessToken):
                    dependencies.decode_access_token(jwt.encode(claims, GOLDEN_KEY, algorithm="HS256"))
            self.assertEqual(
                dependencies.decode_access_token(jwt.encode({**base, "sub": str(2**31 - 1)}, GOLDEN_KEY)),
                (2**31 - 1, UserRole.driver),
            )

    def test_python_jose_is_gone(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("python-jose", "ecdsa", "rsa==", "pyasn1"):
            self.assertNotIn(package, requirements)
        jose_import = re.compile(r"^\s*(from|import)\s+jose\b", re.MULTILINE)
        for path in ROOT.glob("**/*.py"):
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            self.assertIsNone(jose_import.search(path.read_text(encoding="utf-8")), str(path))


if __name__ == "__main__":
    unittest.main()
