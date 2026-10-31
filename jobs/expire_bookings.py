"""Expire due pending requests and close stale rides: ``python -m jobs.expire_bookings``.

Uses DATABASE_URL like the app. Idempotent (a re-run changes nothing new) and
safe while the API serves traffic: one short transaction per ride, ride locks
taken with SKIP LOCKED in the usual Ride -> Booking order. Prints one
``name=value`` line per counter; exits 0 on success. Not scheduled anywhere:
see docs/architecture/BOOKING_SCHEMA_CHANGES.md §4.

Settings (optional env vars, validated at import):
  BOOKING_PENDING_TTL_MINUTES  default 360  (5 .. 10080)
  RIDE_AUTO_CLOSE_AFTER_HOURS  default 24   (1 .. 720)
"""

import argparse
import asyncio

import booking_expiry
from database import SessionLocal, engine


async def run(batch_size: int) -> booking_expiry.SweepResult:
    try:
        async with SessionLocal() as db:
            return await booking_expiry.sweep(db, batch_size=batch_size)
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size must be between 1 and 1000")
    result = asyncio.run(run(args.batch_size))
    print("\n".join(result.as_lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
