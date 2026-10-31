# Background jobs: `jobs.expire_bookings` and `jobs.purge_idempotency_keys`

Prepared 31 Oct 2026 (PKT). **Design + one small safety change; nothing is scheduled.**
Scheduling any job against production is an approval gate (Maaz): it needs a production
credential somewhere outside the app and it writes production rows.

## 1. What the jobs do (as built)

| | `python -m jobs.expire_bookings` | `python -m jobs.purge_idempotency_keys` |
|---|---|---|
| Source | `jobs/expire_bookings.py` → `booking_expiry.sweep()` (PR #20, `c1a7e0b00002`) | `jobs/purge_idempotency_keys.py` → `idempotency.purge_expired()` (PR #18, `c1a7e0b00001`) |
| Work | (1) close `active` rides with `departure_time <= now − RIDE_AUTO_CLOSE_AFTER_HOURS` (default 24): `completed` if any accepted booking else `cancelled`; their pending requests → `expired`. (2) persist `expired` on due pending requests (`expires_at <= now`, or legacy NULL and departed). Accepted bookings and seat counters are never touched. | `DELETE` idempotency keys with `expires_at <= now` (TTL `IDEMPOTENCY_KEY_TTL_HOURS`, default 24) |
| Transactions | one short transaction per ride; ride lock `FOR UPDATE SKIP LOCKED`; order Ride → Booking by id | batches of `--batch-size` (default 1000), `SKIP LOCKED`, one commit per batch |
| Idempotent | yes — a second run with the same `now` changes nothing | yes |
| Needed for correctness? | **No.** Expiry is enforced lazily on every request path (accept → 409, listings show `expired`, re-request expires the caller's own due row). The sweep only makes stored state match and closes stale rides. | **No.** An expired key is reclaimed in place by the next request with the same key. The purge only bounds table growth. |
| Output | `name=value` lines (`expired_requests`, `rides_completed`, `rides_cancelled`, `rides_skipped_locked`) | `purged_idempotency_keys=N` |
| Exit | 0 on success; non-zero + traceback on failure | same |

Because neither job is needed for correctness, **a missed or late run is a hygiene problem, not
an incident.** That drives every choice below toward simple and boring.

## 2. Who owns the schedule — options compared

| Option | How | For | Against | Verdict |
|---|---|---|---|---|
| **A. GitHub Actions `schedule:`** running `python -m jobs.<name>` with a prod `DATABASE_URL` secret | workflow on the default branch, `concurrency:` group per job | free; logs + history + failure e-mails built in; no new attack surface on the API; jobs already are one-shot CLIs | puts a **production DB credential in GitHub** (use a dedicated `rideshare_jobs` role, §5 of SECURITY_REVIEW_RC); **scheduled workflows run only from the default branch** — today that is the stale `main`, so this depends on AST-002's default-branch change; minimum interval 5 min; runs "can be delayed during periods of high loads… some queued jobs may be dropped", especially at the top of the hour; **in a public repo, schedules are auto-disabled after 60 days without repository activity** (all from https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule, read 31 Oct 2026) | **Accept (recommended for MVP)** with the mitigations in §2.1 |
| **B. FastAPI Cloud scheduler / cron** | — | would keep everything on one platform | **Not offered.** FastAPI Cloud's docs navigation (https://fastapicloud.com/docs/, read 31 Oct 2026) has no cron, scheduled-task or worker page (sections: builds/deployments, CLI, team, monitoring, autoscaling, custom domains, deploy tokens, GitHub, Neon/Redis/Supabase/Logfire integrations). A third-party write-up reports the same gap and uses GitHub Actions as a workaround (https://abelcastro.dev/blog/trying-fastapicloud-a-small-deploy-and-the-cron-gap). | **Reject for now**; re-check when FastAPI Cloud ships it |
| **C. External cron calling an admin-only endpoint** (e.g. `POST /admin/jobs/expire-bookings`) | a pinger (GitHub Actions, cron-job.org, …) sends a long-lived token | no DB credential outside FastAPI Cloud | new privileged HTTP surface; needs a **non-expiring** machine credential (our JWTs expire in 60 min, admin tokens should get shorter per D8), so a separate secret-header scheme is required; a long sweep inside a request hits proxy timeouts; still needs an external scheduler; scale-to-zero means the call also cold-starts the app | **Reject** (keep as fallback if a DB credential in GitHub is unacceptable) |
| **D. In-process APScheduler** in the API | scheduler started in FastAPI lifespan | no external moving parts | FastAPI Cloud **scales to zero by default, and Hobby apps always do** (https://fastapicloud.com/docs/advanced-features/autoscaling/) → no replica = no ticks; with >1 replica every replica ticks (needs the lock anyway); ticks die mid-run on every redeploy (gradual rollout); couples job failures to API memory/CPU; FastAPI's author advises against cron inside the app for exactly the multi-worker reason (https://github.com/fastapi/fastapi/issues/5645) | **Reject** |
| E. Neon `pg_cron` calling SQL | — | in-database | duplicates Python logic in SQL; availability on the plan unverified | Reject |

### 2.1 Recommended shape for option A (proposal — do not add the workflow without approval)

Prerequisites: AST-002 default branch is `rideshare-v2` (or `main` fast-forwarded); dedicated
`rideshare_jobs` DB role (grants in SECURITY_REVIEW_RC §5); **direct (non-`-pooler`) Neon host** in the
secret (session advisory locks, §4); an Actions environment `production-jobs` with Maaz as required
reviewer for manual runs.

```yaml
# .github/workflows/maintenance-jobs.yml  — PROPOSAL, NOT COMMITTED
name: Maintenance jobs
on:
  schedule:
    - cron: "7,22,37,52 * * * *"   # every 15 min, off the top of the hour (GitHub load guidance)
    - cron: "41 3 * * *"           # daily purge at 03:41 UTC (08:41 PKT)
  workflow_dispatch:
    inputs:
      job: {type: choice, options: [expire_bookings, purge_idempotency_keys], required: true}
permissions: {contents: read}
jobs:
  run:
    runs-on: ubuntu-latest
    environment: production-jobs
    timeout-minutes: 10
    concurrency: {group: "maint-${{ github.event.schedule || inputs.job }}", cancel-in-progress: false}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version-file: .python-version}
      - run: pip install -r requirements.txt
      - name: Run
        env:
          DATABASE_URL: ${{ secrets.JOBS_DATABASE_URL }}   # rideshare_jobs role, direct host
        run: |
          JOB="${{ inputs.job }}"
          if [ -z "$JOB" ]; then
            if [ "${{ github.event.schedule }}" = "41 3 * * *" ]; then JOB=purge_idempotency_keys; else JOB=expire_bookings; fi
          fi
          python -m "jobs.$JOB" | tee out.txt
      - name: Heartbeat
        if: success()
        run: curl -fsS -m 10 "${{ secrets.JOBS_HEARTBEAT_URL }}" > /dev/null   # external dead-man switch
```

Note on secrets: the jobs import `database`, `models`, `booking_expiry`/`idempotency` and
`lifecycle`, **not** `config.py` (checked 31 Oct: `'config' in sys.modules` is False after importing
both job modules), so they need only `DATABASE_URL`. Never give a job the production `SECRET_KEY`.

## 3. Frequency

| Job | Recommended | Why |
|---|---|---|
| `expire_bookings` | **every 15 min** (offset minutes), `--batch-size 100` | Pending TTL default 360 min and auto-close 24 h, so 15 min is < 5 % of the shortest window. Lazy enforcement already covers correctness; the sweep only tidies stored state and notifications (none yet). GitHub's 5-min floor and best-effort delays make tighter cadences meaningless. |
| `purge_idempotency_keys` | **daily**, `--batch-size 1000` | TTL default 24 h; table grows by one small row per keyed write. Daily keeps it ≤ ~2 days of keys. |

## 4. Distributed locking and idempotency

Both jobs were already idempotent and row-safe (SKIP LOCKED, short transactions), so two copies
cannot corrupt data. What two copies *can* do: double the DB load, contend on the same rides (one
copy reports `rides_skipped_locked` for rows the other holds), and emit misleading counters. GitHub's
`concurrency:` group prevents overlap only *within* GitHub; a manual run from a laptop or a second
scheduler would still overlap. So the jobs take a **PostgreSQL session-level advisory lock**:

- Key: two-int form `pg_try_advisory_lock(0x52534A42 /* "RSJB" */, job_id)` with
  `expire_bookings = 1`, `purge_idempotency_keys = 2` (`jobs/locking.py`, `JOB_LOCK_NAMESPACE`,
  `JOB_LOCK_IDS`). Namespaced so it cannot collide with another advisory-lock user of the database.
- `try` (non-blocking): a second copy prints `job_lock=skipped` and exits **0** — overlap is normal
  for a late cron tick, not an error. A run that does work prints `job_lock=acquired` first.
- Held on its **own connection** for the whole run, separate from the work session (which commits
  many times; `pg_try_advisory_xact_lock` would be released at the first commit — mutation-tested).
- Released explicitly in `finally`, and by PostgreSQL automatically when the holder connection closes
  (crash, kill, network loss) — so a dead run never blocks the next one (tested).
- **Requires a direct connection.** Behind PgBouncer in transaction mode (Neon `-pooler` hosts) the
  lock and unlock can land on different server sessions → use the direct host for jobs.
- Different jobs use different ids and never block each other (tested).

Implementation: branch `feat/job-advisory-lock` (stacked on `feat/admin-audit-log` / #21) — see §8.

## 5. Failure recovery

- **Mid-run failure** (exception, DB restart, timeout): completed per-ride / per-batch transactions
  stay committed; the in-flight one rolls back; the lock is released; exit non-zero. **Recovery is
  "run it again"** — the next scheduled tick resumes because both jobs select by state, not by a cursor.
- **Repeated failure** (≥ 3 consecutive): look at the traceback (no PII: the jobs log no row data;
  once #8 is merged, `hide_parameters` keeps bound values out of SQLAlchemy errors). Typical causes: credential
  rotated (update `JOBS_DATABASE_URL`), migration not applied (`expires_at` missing → run
  `alembic upgrade`), `statement_timeout` on a huge backlog (lower `--batch-size`).
- **Huge backlog** (e.g. first run after months): `expire_bookings` already pages by ride id in
  batches of `--batch-size`; run once manually off-peak with `--batch-size 100`, watch duration.
- **Wrong results suspected:** stop the schedule (disable the workflow), then follow PR #20 Rollback
  (remap `expired → cancelled` needs approved prod SQL). Closed rides stay `completed`/`cancelled`,
  which are valid states.
- **Kill switch:** disable the GitHub workflow (UI or `gh workflow disable`); no app redeploy needed.

## 6. Monitoring

Proposed (not built — a heartbeat table would be a new migration, so prefer an external dead-man switch first):

1. **Dead-man heartbeat (recommended first):** the workflow pings an external heartbeat URL after a
   successful run (step "Heartbeat" above). Alert if no ping for **45 min** (`expire_bookings`, 3 missed
   15-min ticks — tolerates GitHub delays) or **26 h** (`purge`). Vendor choice is Maaz's.
2. **GitHub failure notifications** go to the user who last edited the cron line; make sure that is Maaz.
3. **In-DB health checks** (read-only SQL, run from the readiness dashboard or ad hoc):
   - stuck lock: `SELECT pid, now()-backend_start FROM pg_locks l JOIN pg_stat_activity a USING (pid)
     WHERE l.locktype='advisory' AND l.classid::bigint = 1381190210;` → alert if held > 15 min.
   - backlog: `SELECT count(*) FROM bookings WHERE status='pending' AND expires_at <= now() - interval '1 hour';`
     → alert if > 0 for 2 consecutive checks (sweep not running).
   - stale rides: `SELECT count(*) FROM rides WHERE status='active' AND departure_time <= now() - interval '48 hours';` → > 0 means closure is not running.
   - purge: `SELECT count(*) FROM idempotency_keys WHERE expires_at <= now() - interval '2 days';` → > 0 means purge not running.
4. **Optional later:** `job_runs(job, started_at, finished_at, outcome, counters jsonb)` heartbeat table
   written by the job itself (additive migration; jobs role gets INSERT). Gives history inside the DB.
   Alert thresholds as in (1). Not in the RC.
5. **Skip rate:** `job_lock=skipped` more than 3 runs in a row means a run is stuck holding the lock →
   check (3) stuck-lock query, terminate that backend if it is idle (approval: prod action).

## 7. Safe restart / deploy behaviour

- The jobs are separate processes from the API: redeploying the API never interrupts them, and a job
  run never blocks a deploy.
- A job killed mid-run (runner cancelled, timeout, Ctrl-C) leaves only committed, consistent per-ride
  transactions; the advisory lock dies with the connection; the next run continues.
- **Migrations:** pause the schedule during `alembic upgrade`/`downgrade` of `c1a7e0b00002` (CONCURRENTLY
  index builds wait on open transactions; the sweep's transactions are short but frequent) and during any
  rollback that remaps `expired`. Re-enable after the post-deploy smoke.
- Deploy order: code that understands `expired` must be live before the first sweep (PR #20 Rollout 5).
- `SKIP LOCKED` means the sweep never waits on, or blocks, a user request holding a ride lock.

## 8. Implementation: advisory-lock wrapper (done, draft PR)

Small and safe, so it was implemented: `feat/job-advisory-lock`, stacked on `origin/feat/admin-audit-log`
(#21 @ `7cedddb`). Files: new `jobs/locking.py` (`job_lock(engine, job)` async context manager yielding
`True`/`False`), `jobs/expire_bookings.py` and `jobs/purge_idempotency_keys.py` (`run(batch_size, engine=None)`
returns `None` when skipped; CLI prints `job_lock=skipped|acquired`), new `tests/test_job_locking.py`
(10 tests: exclusivity + release, independent jobs, skip changes nothing, concurrent double run = one
effective run for each job, release after exception, release when the holder connection dies, CLI skip
exits 0, key ranges). Mutation checks: no lock → 8 fail; never unlock → 5 fail; xact-level lock → 6 fail.
Core of the diff:

```python
@asynccontextmanager
async def job_lock(engine: AsyncEngine, job: str):
    params = {"ns": JOB_LOCK_NAMESPACE, "job": JOB_LOCK_IDS[job]}
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:ns, :job)"), params))
        await conn.commit()          # session lock survives; holder not idle-in-transaction
        try:
            yield acquired
        finally:
            if acquired:
                await conn.scalar(text("SELECT pg_advisory_unlock(:ns, :job)"), params)
                await conn.commit()
```

Behaviour change visible to callers: the CLI output gains a first line `job_lock=acquired` (or is only
`job_lock=skipped`). The existing subprocess tests check for contained lines and still pass.
