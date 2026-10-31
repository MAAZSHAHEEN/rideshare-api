"""One-shot maintenance jobs, run as ``python -m jobs.<name>``.

No scheduler is deployed (FastAPI Cloud worker/cron support is unverified);
see docs/architecture/BOOKING_SCHEMA_CHANGES.md for scheduling options.
Every job is idempotent and safe to run concurrently with the API.
"""
