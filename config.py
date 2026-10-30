import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

from auth_rate_limit import RateLimitSettings

AUTH_RATE_LIMIT_SETTINGS = RateLimitSettings.from_environment()

# Comma-separated exact browser origins. Empty disables cross-origin access.
FRONTEND_ORIGINS = [value.strip() for value in os.getenv("FRONTEND_ORIGINS", "").split(",")
                    if value.strip()]
for origin in FRONTEND_ORIGINS:
    try:
        parsed = urlsplit(origin)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and "*" not in origin and not any(char.isspace() for char in origin))
        parsed.port  # Validate a supplied port.
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("FRONTEND_ORIGINS must contain exact HTTP(S) origins without paths")

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
try:
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
except ValueError:
    raise RuntimeError("ACCESS_TOKEN_EXPIRE_MINUTES must be a positive integer") from None

# Shared-secret HMAC signing: algorithms are chosen by configuration, not tokens.
_MIN_KEY_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}
if ALGORITHM not in _MIN_KEY_BYTES:
    raise RuntimeError("ALGORITHM must be HS256, HS384, or HS512")
if (
    not SECRET_KEY
    or not SECRET_KEY.strip()
    or len(SECRET_KEY.encode("utf-8")) < _MIN_KEY_BYTES[ALGORITHM]
    or SECRET_KEY.strip().lower().startswith(("your-secret", "yoursecret", "replace-with", "change-me", "changeme"))
):
    raise RuntimeError("SECRET_KEY must be a non-placeholder signing key of sufficient length for ALGORITHM")
if ACCESS_TOKEN_EXPIRE_MINUTES <= 0:
    raise RuntimeError("ACCESS_TOKEN_EXPIRE_MINUTES must be a positive integer")
if AUTH_RATE_LIMIT_SETTINGS.enabled and AUTH_RATE_LIMIT_SETTINGS.hmac_key == SECRET_KEY:
    raise RuntimeError("Rate-limit HMAC key must be separate from the JWT signing key")

# Largest accepted request body in bytes (default 64 KiB; API payloads are small JSON).
_MAX_BODY_BOUNDS = (1024, 10 * 1024 * 1024)
_max_body_raw = os.getenv("MAX_REQUEST_BODY_BYTES", "65536").strip()
if not _max_body_raw.isascii() or not _max_body_raw.isdigit():
    raise RuntimeError("MAX_REQUEST_BODY_BYTES must be an integer between 1024 and 10485760")
MAX_REQUEST_BODY_BYTES = int(_max_body_raw)
if not _MAX_BODY_BOUNDS[0] <= MAX_REQUEST_BODY_BYTES <= _MAX_BODY_BOUNDS[1]:
    raise RuntimeError("MAX_REQUEST_BODY_BYTES must be an integer between 1024 and 10485760")
