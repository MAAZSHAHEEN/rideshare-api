"""Idempotency keys for booking writes (Milestone C, AST-022).

A client may send ``Idempotency-Key: <1-64 visible ASCII characters>`` on
``POST /bookings/{ride_id}`` and ``PATCH /bookings/{booking_id}/respond``.
Without the header nothing changes. With it:

1. ``claim()`` runs first in the request's transaction, *before* the ride row
   lock (lock order: idempotency key -> Ride -> Booking). It inserts
   ``(user_id, key)`` with ``INSERT ... ON CONFLICT DO UPDATE ... WHERE
   expires_at <= now``. A concurrent request with the same key blocks on the
   primary key until the first transaction ends:
   - first commits  -> the waiter finds the stored response and replays it;
   - first rolls back (any 4xx/5xx) -> the key vanishes with it and the waiter
     proceeds as a fresh request. No "in progress" row is ever committed.
2. The business operation runs unchanged. Its ``before_commit`` hook calls
   ``recorder()``, which stores the status code and JSON body in the same
   transaction, after every guard passed and immediately before commit.
3. A later request with the same key and the same fingerprint gets the stored
   status and body back with ``Idempotent-Replayed: true``. A different
   fingerprint (other operation, path parameter or query value) is 422.

Only successful (2xx) outcomes are stored: errors are recomputed because state
may legitimately change (e.g. a seat frees up). Keys are scoped to the
authenticated user, so one user's key never reveals or replays another's.
Rows expire after ``IDEMPOTENCY_KEY_TTL_HOURS`` (default 24); an expired key is
reclaimed in place and ``python -m jobs.purge_idempotency_keys`` deletes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

import clock
from models import IdempotencyKey

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
# RFC 9110 visible ASCII (no spaces or control characters), bounded length.
KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,64}")
INVALID_KEY = "Idempotency-Key must be 1 to 64 visible ASCII characters"
KEY_REUSED = "Idempotency-Key was already used for a different request"
KEY_IN_PROGRESS = "A request with this Idempotency-Key is still in progress"

_DEFAULT_TTL_HOURS = 24
_MAX_TTL_HOURS = 24 * 30


def _ttl_from_environment() -> timedelta:
    raw = os.getenv("IDEMPOTENCY_KEY_TTL_HOURS", str(_DEFAULT_TTL_HOURS))
    try:
        hours = int(raw)
    except ValueError:
        raise RuntimeError("IDEMPOTENCY_KEY_TTL_HOURS must be an integer") from None
    if not 1 <= hours <= _MAX_TTL_HOURS:
        raise RuntimeError(f"IDEMPOTENCY_KEY_TTL_HOURS must be between 1 and {_MAX_TTL_HOURS}")
    return timedelta(hours=hours)


KEY_TTL = _ttl_from_environment()


def validate_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not KEY_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail=INVALID_KEY)
    return value


async def key_header(
    value: Annotated[str | None, Header(alias=HEADER)] = None,
) -> str | None:
    """FastAPI dependency: the validated header value, or None when absent."""
    return validate_key(value)


def fingerprint(operation: str, parameters: dict[str, Any]) -> str:
    """Hash of the operation and its *parsed* inputs (accept=1 == accept=true)."""
    canonical = json.dumps({"operation": operation, "parameters": parameters},
                           sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


Recorder = Callable[[Any], Awaitable[None]]


@dataclass
class Claim:
    """Outcome of ``claim``: either a response to replay, or a recorder."""
    replay: JSONResponse | None
    user_id: int
    key: str
    db: AsyncSession

    def recorder(self, status_code: int, model: type[BaseModel]) -> Recorder:
        async def record(result: Any) -> None:
            body = model.model_validate(result).model_dump(mode="json")
            await self.db.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.user_id == self.user_id, IdempotencyKey.key == self.key)
                .values(status_code=status_code, response_body=body)
            )
        return record


async def claim(
    db: AsyncSession, user_id: int, key: str, operation: str, parameters: dict[str, Any],
    *, now: datetime | None = None,
) -> Claim:
    """Reserve ``key`` for this request or return the stored response to replay.

    Must be the first lock taken in the request's transaction. Raises 422 when
    the key was used for a different request.
    """
    request_hash = fingerprint(operation, parameters)
    # Two attempts: the only reason for a second is the purge job deleting the
    # conflicting (expired) row between our insert and our read.
    for _ in range(2):
        current = now or clock.utc_now()
        values = dict(user_id=user_id, key=key, operation=operation, request_hash=request_hash,
                      status_code=None, response_body=None,
                      created_at=current, expires_at=current + KEY_TTL)
        statement = pg_insert(IdempotencyKey).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="pk_idempotency_keys",
            set_={name: statement.excluded[name] for name in (
                "operation", "request_hash", "status_code", "response_body",
                "created_at", "expires_at")},
            # Only an expired key may be reused; a live one is left untouched
            # (but locked until this transaction ends).
            where=IdempotencyKey.expires_at <= current,
        ).returning(IdempotencyKey.user_id)
        if (await db.execute(statement)).first() is not None:
            return Claim(replay=None, user_id=user_id, key=key, db=db)

        stored = (await db.execute(
            select(IdempotencyKey.request_hash, IdempotencyKey.status_code,
                   IdempotencyKey.response_body)
            .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        )).first()
        if stored is None:
            continue
        # Nothing else is written by this request: end the transaction now.
        await db.rollback()
        if stored.request_hash != request_hash:
            raise HTTPException(status_code=422, detail=KEY_REUSED)
        if stored.status_code is None:  # defensive: never committed by this module
            raise HTTPException(status_code=409, detail=KEY_IN_PROGRESS)
        return Claim(
            replay=JSONResponse(status_code=stored.status_code, content=stored.response_body,
                                headers={REPLAY_HEADER: "true"}),
            user_id=user_id, key=key, db=db,
        )
    await db.rollback()
    raise HTTPException(status_code=409, detail=KEY_IN_PROGRESS)


async def purge_expired(db: AsyncSession, *, now: datetime | None = None,
                        batch_size: int = 1000) -> int:
    """Delete expired keys in short batches; returns the number deleted.

    SKIP LOCKED: a key being reclaimed by a live request is left for next time.
    Each batch commits on its own, so the job never holds many row locks.
    """
    current = now or clock.utc_now()
    total = 0
    while True:
        due = (select(IdempotencyKey.user_id, IdempotencyKey.key)
               .where(IdempotencyKey.expires_at <= current)
               .order_by(IdempotencyKey.expires_at)
               .limit(batch_size).with_for_update(skip_locked=True))
        result = await db.execute(
            delete(IdempotencyKey)
            .where(IdempotencyKey.expires_at <= current)
            .where(tuple_(IdempotencyKey.user_id, IdempotencyKey.key).in_(due))
        )
        await db.commit()
        total += result.rowcount or 0
        if (result.rowcount or 0) < batch_size:
            return total
