"""Delete expired idempotency keys: ``python -m jobs.purge_idempotency_keys``.

Uses DATABASE_URL like the app. Idempotent; safe while the API serves traffic
(batches of --batch-size rows, SKIP LOCKED, one commit per batch) and
single-runner (advisory lock, jobs/locking.py; a concurrent copy prints
``job_lock=skipped``). Prints the
number of rows deleted, then one structured summary line
(``job=purge_idempotency_keys status=ok purged=N duration_ms=D``, see
jobs/reporting.py); exits 0 on success or skip, 1 on failure.
"""

import argparse
import asyncio

import database
import idempotency
from jobs.locking import job_lock
from jobs.reporting import Timer, report_failure, summary

JOB = "purge_idempotency_keys"

SKIPPED_LINE = "job_lock=skipped"


async def run(batch_size: int, engine=None) -> int | None:
    """Purge once; None when another runner holds the job lock."""
    owned = engine is None
    engine = database.engine if engine is None else engine
    try:
        async with job_lock(engine, "purge_idempotency_keys") as acquired:
            if not acquired:
                return None
            async with database.SessionLocal(bind=engine) as db:
                return await idempotency.purge_expired(db, batch_size=batch_size)
    finally:
        if owned:
            await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 10000:
        parser.error("--batch-size must be between 1 and 10000")
    timer = Timer()
    try:
        deleted = asyncio.run(run(args.batch_size))
    except Exception as exc:  # report, exit non-zero; committed batches stay committed
        return report_failure(JOB, timer, exc)
    if deleted is None:
        print(SKIPPED_LINE)  # another run holds the lock; not an error
        print(summary(JOB, "skipped", timer))
        return 0
    print("job_lock=acquired")
    print(f"purged_idempotency_keys={deleted}")
    print(summary(JOB, "ok", timer, purged=deleted))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
