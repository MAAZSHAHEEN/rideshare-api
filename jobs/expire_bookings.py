"""Expire due pending requests and close stale rides: ``python -m jobs.expire_bookings``.

Uses DATABASE_URL like the app. Idempotent (a re-run changes nothing new) and
safe while the API serves traffic and single-runner (advisory lock,
jobs/locking.py; a concurrent copy prints ``job_lock=skipped``): one short transaction per ride, ride locks
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
import database
from jobs.locking import job_lock

SKIPPED_LINE = "job_lock=skipped"


async def run(batch_size: int, engine=None) -> booking_expiry.SweepResult | None:
    """Run one sweep; None when another runner holds the job lock."""
    owned = engine is None
    engine = database.engine if engine is None else engine
    try:
        async with job_lock(engine, "expire_bookings") as acquired:
            if not acquired:
                return None
            async with database.SessionLocal(bind=engine) as db:
                return await booking_expiry.sweep(db, batch_size=batch_size)
    finally:
        if owned:
            await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size must be between 1 and 1000")
    result = asyncio.run(run(args.batch_size))
    if result is None:
        print(SKIPPED_LINE)  # another run holds the lock; not an error
        return 0
    print("\n".join(["job_lock=acquired", *result.as_lines()]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
