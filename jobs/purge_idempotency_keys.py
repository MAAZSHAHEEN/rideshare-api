"""Delete expired idempotency keys: ``python -m jobs.purge_idempotency_keys``.

Uses DATABASE_URL like the app. Idempotent; safe while the API serves traffic
(batches of --batch-size rows, SKIP LOCKED, one commit per batch). Prints the
number of rows deleted; exits 0 on success.
"""

import argparse
import asyncio

import idempotency
from database import SessionLocal, engine


async def run(batch_size: int) -> int:
    try:
        async with SessionLocal() as db:
            return await idempotency.purge_expired(db, batch_size=batch_size)
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 10000:
        parser.error("--batch-size must be between 1 and 10000")
    deleted = asyncio.run(run(args.batch_size))
    print(f"purged_idempotency_keys={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
