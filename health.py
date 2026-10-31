"""Liveness and readiness probes (AST-010). See docs/operations/HEALTH_CHECKS.md.

- GET/HEAD /healthz: liveness. Touches no database, Redis or other dependency,
  so it stays 200 while the process can serve HTTP, even during a DB outage.
- GET/HEAD /readyz: readiness. Runs ``SELECT 1`` on the application's engine
  under a hard total deadline (pool checkout + connect + query), and pings Redis
  only when authentication rate limiting is enabled (Redis is then a hard
  dependency of login/register). Returns 200 or 503 with fixed, enumerated
  values only: never exception text, DSNs, hostnames or secrets.

Neither endpoint requires authentication, and neither is rate limited (the
limiter only matches POST /auth/login and POST /auth/register).

Configuration: READINESS_TIMEOUT_SECONDS (default 2, range (0, 10]) bounds each
dependency check. At most READINESS_MAX_CONCURRENT_CHECKS (2) probes touch the
database at once; extra concurrent probes wait for a slot inside the same
deadline, so a probe flood can never take more than two pooled connections from
real traffic and never hangs.
"""

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

import database

NO_STORE = {"Cache-Control": "no-store"}
READINESS_MAX_CONCURRENT_CHECKS = 2


def _readiness_timeout() -> float:
    raw = os.getenv("READINESS_TIMEOUT_SECONDS", "2")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        value = float("nan")
    if not 0 < value <= 10:  # Also rejects NaN.
        raise RuntimeError("READINESS_TIMEOUT_SECONDS must be greater than 0 and at most 10")
    return value


READINESS_TIMEOUT_SECONDS = _readiness_timeout()
_db_slots: tuple | None = None  # (event loop, semaphore), created per loop.

router = APIRouter(tags=["operations"])


def _database_slots() -> asyncio.Semaphore:
    global _db_slots
    loop = asyncio.get_running_loop()
    if _db_slots is None or _db_slots[0] is not loop:
        _db_slots = (loop, asyncio.Semaphore(READINESS_MAX_CONCURRENT_CHECKS))
    return _db_slots[1]


async def check_database(timeout: float) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with _database_slots():
                async with database.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # Includes TimeoutError. CancelledError still propagates.
        return False


async def check_redis(limiter, timeout: float) -> str:
    settings = getattr(limiter, "settings", None)
    if limiter is None or settings is None or not settings.enabled:
        return "disabled"
    try:
        async with asyncio.timeout(timeout):
            ok = await limiter.redis.ping()
        return "ok" if ok is True else "unavailable"
    except Exception:
        return "unavailable"


@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def healthz():
    return JSONResponse({"status": "ok"}, headers=NO_STORE)


@router.api_route("/readyz", methods=["GET", "HEAD"], include_in_schema=False)
async def readyz(request: Request):
    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    db_ok, redis_state = await asyncio.gather(
        check_database(READINESS_TIMEOUT_SECONDS),
        check_redis(limiter, READINESS_TIMEOUT_SECONDS),
    )
    checks = {"database": "ok" if db_ok else "unavailable", "redis": redis_state}
    ready = db_ok and redis_state != "unavailable"
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
        headers=NO_STORE,
    )
