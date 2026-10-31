"""Single-runner guard for the maintenance jobs (RC readiness, BACKGROUND_JOBS.md).

Each job takes a PostgreSQL *session-level* advisory lock before doing any
work: ``pg_try_advisory_lock(JOB_LOCK_NAMESPACE, <job id>)``. A second copy
started while the first is still running (an overlapping cron tick, a manual
run during a scheduled one, a retried CI job) does not wait and does not
touch any row: it reports ``job_lock=skipped`` and exits 0.

The jobs are already idempotent and row-safe (SKIP LOCKED, one transaction
per ride / batch), so the lock is not needed for correctness. It stops two
runners from duplicating work, competing for the same rows, and producing
misleading counters, and gives monitoring one clear "already running" signal.

Rules:
- The lock lives on its own connection, held for the whole run, separate from
  the session that does the work (that session commits many times; a
  transaction-level lock would be released at the first commit).
- It is released explicitly, and PostgreSQL also releases it if the process
  dies or the connection drops, so a crashed run never blocks the next one.
- Session advisory locks need a *direct* connection. Behind a transaction-
  pooling proxy (PgBouncer "transaction" mode, e.g. Neon's ``-pooler`` host)
  the lock and the unlock can land on different server sessions. Run the jobs
  with the direct (non-pooler) DATABASE_URL.
"""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Two-key form (int4, int4): the namespace keeps these ids away from any other
# advisory-lock user of the same database. 0x52534A42 = ASCII "RSJB".
JOB_LOCK_NAMESPACE = 0x52534A42
JOB_LOCK_IDS = {
    "expire_bookings": 1,
    "purge_idempotency_keys": 2,
}


@asynccontextmanager
async def job_lock(engine: AsyncEngine, job: str):
    """Yield True when this process holds the job's lock, False when another does."""
    job_id = JOB_LOCK_IDS[job]
    params = {"ns": JOB_LOCK_NAMESPACE, "job": job_id}
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(
            text("SELECT pg_try_advisory_lock(:ns, :job)"), params))
        # End the implicit transaction: the session-level lock survives it and
        # the holder connection must not sit "idle in transaction" for the run.
        await conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                await conn.scalar(text("SELECT pg_advisory_unlock(:ns, :job)"), params)
                await conn.commit()
