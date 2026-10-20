"""Shared auth throttling. Trust only the ASGI server's verified client IP.

Never parse forwarding headers here. Cloud proxy trust remains unverified.
Fixed windows start on the first attempt; denied attempts do not extend them.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
from ipaddress import ip_address
import json
import math
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class RateLimitSettings:
    enabled: bool = False
    redis_url: str = ""
    hmac_key: str = ""
    client_ip_source: str = "unverified"
    namespace: str = "rideshare:auth:v1"
    login_limit: int = 30
    login_window_seconds: int = 60
    register_limit: int = 5
    register_window_seconds: int = 600
    login_identity_limit: int = 5
    login_identity_window_seconds: int = 60
    timeout_seconds: float = 0.5

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", self.namespace):
            raise RuntimeError("Invalid authentication rate-limit namespace")
        for value in (
            self.login_limit, self.login_window_seconds,
            self.register_limit, self.register_window_seconds,
            self.login_identity_limit, self.login_identity_window_seconds,
        ):
            if type(value) is not int or not 1 <= value <= 86400:
                raise RuntimeError("Rate-limit counts/windows must be integers from 1 to 86400")
        if (type(self.timeout_seconds) not in {int, float}
                or not 0 < self.timeout_seconds <= 5):
            raise RuntimeError("Rate-limit timeout must be greater than zero and at most 5 seconds")
        if not self.enabled:
            return
        if self.client_ip_source != "verified-asgi":
            raise RuntimeError("Rate limiting requires a verified ASGI client-IP boundary")
        if (not self.hmac_key.strip() or len(self.hmac_key.encode()) < 32
                or self.hmac_key.lower().startswith(("replace", "change", "your-"))):
            raise RuntimeError("Rate limiting requires a separate non-placeholder HMAC key")
        try:
            parsed = urlsplit(self.redis_url)
            valid = parsed.scheme in {"redis", "rediss"} and parsed.hostname
            parsed.port
            valid = valid and (parsed.scheme == "rediss"
                               or parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            valid = valid and not parsed.query and not parsed.fragment
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("Rate limiting requires TLS Redis (or loopback test Redis)")

    @classmethod
    def from_environment(cls):
        enabled = os.getenv("AUTH_RATE_LIMIT_ENABLED", "false")
        if enabled not in {"true", "false"}:
            raise RuntimeError("AUTH_RATE_LIMIT_ENABLED must be true or false")
        try:
            timeout = float(os.getenv("AUTH_RATE_LIMIT_TIMEOUT_SECONDS", "0.5"))
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError(
                "Rate-limit timeout must be greater than zero and at most 5 seconds"
            ) from None
        try:
            return cls(
                enabled=enabled == "true",
                redis_url=os.getenv("AUTH_RATE_LIMIT_REDIS_URL", ""),
                hmac_key=os.getenv("AUTH_RATE_LIMIT_HMAC_KEY", ""),
                client_ip_source=os.getenv("AUTH_RATE_LIMIT_CLIENT_IP_SOURCE", "unverified"),
                namespace=os.getenv("AUTH_RATE_LIMIT_NAMESPACE", "rideshare:auth:v1"),
                **{name: int(os.getenv("AUTH_RATE_LIMIT_" + name.upper(), str(default)))
                   for name, default in (
                       ("login_limit", 30), ("login_window_seconds", 60),
                       ("register_limit", 5), ("register_window_seconds", 600),
                       ("login_identity_limit", 5), ("login_identity_window_seconds", 60),
                   )},
                timeout_seconds=timeout,
            )
        except ValueError:
            raise RuntimeError("Invalid authentication rate-limit numeric setting") from None


# Creation, expiry, decision and TTL are atomic. Saturate denied counters.
_COUNTER_SCRIPT = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local ttl = redis.call('PTTL', KEYS[1])
if ttl == -2 then
    redis.call('SET', KEYS[1], 1, 'PX', ARGV[2])
    return {1, tonumber(ARGV[2])}
end
if ttl == -1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    ttl = tonumber(ARGV[2])
end
if count >= tonumber(ARGV[1]) then return {0, ttl} end
redis.call('INCR', KEYS[1])
return {1, ttl}
"""


def unavailable():
    return HTTPException(503, "Authentication temporarily unavailable",
                         headers={"Cache-Control": "no-store", "Retry-After": "1"})


class AuthRateLimiter:
    def __init__(self, settings: RateLimitSettings):
        self.settings = settings
        self.redis = None
        if settings.enabled:
            self.redis = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.timeout_seconds,
                socket_timeout=settings.timeout_seconds,
                max_connections=32,
                retry=Retry(NoBackoff(), 0),
            )

    async def close(self):
        if self.redis is not None:
            await self.redis.aclose()

    def client_ip(self, scope):
        try:
            address = ip_address(scope["client"][0])
            if getattr(address, "ipv4_mapped", None):
                address = address.ipv4_mapped
            return address.compressed
        except (KeyError, TypeError, ValueError, IndexError):
            raise unavailable() from None

    def key(self, bucket, identifiers):
        payload = json.dumps([bucket, *identifiers], separators=(",", ":")).encode()
        digest = hmac.new(self.settings.hmac_key.encode(), payload, hashlib.sha256).hexdigest()
        return f"{self.settings.namespace}:{bucket}:{digest}"

    async def consume(self, bucket, identifiers, limit, window):
        try:
            # Total deadline covers pool acquisition, DNS, connect and response.
            async with asyncio.timeout(self.settings.timeout_seconds):
                result = await self.redis.eval(
                    _COUNTER_SCRIPT, 1, self.key(bucket, identifiers), limit, window * 1000,
                )
            # Redis Lua returns two integers. Never accept truthy strings/bools,
            # missing fields, or TTLs beyond the maximum configurable window.
            if (type(result) not in {list, tuple} or len(result) != 2
                    or type(result[0]) is not int or result[0] not in {0, 1}
                    or type(result[1]) is not int or not 0 <= result[1] <= 86_400_000):
                raise ValueError("Invalid rate-limit response")
            allowed, ttl = result
        except Exception:
            # Fail closed at the Redis boundary, including unexpected client/
            # decoder failures. CancelledError (BaseException) still propagates.
            raise unavailable() from None
        if not allowed:
            raise HTTPException(
                429, "Too many authentication attempts",
                headers={"Retry-After": str(max(1, math.ceil(ttl / 1000))),
                         "Cache-Control": "no-store"},
            )

    async def coarse(self, scope, operation):
        if self.settings.enabled:
            await self.consume(
                operation, [self.client_ip(scope)],
                getattr(self.settings, operation + "_limit"),
                getattr(self.settings, operation + "_window_seconds"),
            )

    async def login_identity(self, scope, email):
        if self.settings.enabled:
            # Normalize only the limiter identifier, not the authentication query.
            await self.consume(
                "login-identity", [self.client_ip(scope), email.strip().casefold()],
                self.settings.login_identity_limit,
                self.settings.login_identity_window_seconds,
            )


class AuthRateLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            operation = {"/auth/login": "login", "/auth/register": "register"}.get(
                scope["path"].rstrip("/")
            )
            if operation:
                try:
                    await scope["app"].state.auth_rate_limiter.coarse(scope, operation)
                except HTTPException as exc:
                    await JSONResponse(
                        {"detail": exc.detail}, status_code=exc.status_code,
                        headers=exc.headers,
                    )(scope, receive, send)
                    return
        await self.app(scope, receive, send)


async def check_login_identity(request: Request, email: str):
    await request.app.state.auth_rate_limiter.login_identity(request.scope, email)
