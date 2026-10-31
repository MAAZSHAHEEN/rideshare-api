# Scheduler and operations for the maintenance jobs

Prepared 31 Oct 2026 (staging-ops). Extends [BACKGROUND_JOBS.md](BACKGROUND_JOBS.md) (options
compared, frequency rationale, monitoring SQL) with the decision, the recovery evidence from PR #26,
retry/alerting, ownership, and a ready-to-commit-later workflow. **Nothing is scheduled.** Creating
the workflow, the secret and the environment is approval-gated.

## 1. Decision

**GitHub Actions `schedule:`** (BACKGROUND_JOBS §2 option A). FastAPI Cloud offers no scheduler and
scales to zero (in-process schedulers would not tick); an admin-endpoint pinger adds privileged HTTP
surface.

Two GitHub constraints (https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule, read 31 Oct 2026 by the ops-review agent):

1. **Scheduled workflows run only from the default branch.** Today that is the stale `main`
   (AST-002). The workflow therefore cannot run until the default branch is `rideshare-v2` (or
   `main` is fast-forwarded) — **LB-2 must land first.**
2. **In a public repository, schedules are disabled after 60 days without repository activity.**
   Mitigation: the dead-man heartbeat (§4) alerts within 45 min of the first missed run; re-enable
   from the Actions tab. Do not add "keep-alive" commits.

Also: minimum interval 5 min; runs may be delayed or dropped under load, especially at the top of the
hour (hence minutes 7/22/37/52).

## 2. Frequencies

| Job | Schedule | Batch | Why |
|---|---|---|---|
| `jobs.expire_bookings` | every 15 min (`7,22,37,52 * * * *`) | 100 rides | < 5 % of the 360-min pending TTL; correctness is lazy (requests expire on read), the sweep tidies state |
| `jobs.purge_idempotency_keys` | daily 03:41 UTC (08:41 PKT) | 1000 rows | 24 h key TTL |

## 3. Single-runner, retries and recovery — evidence from PR #26

Implementation: session-level `pg_try_advisory_lock(0x52534A42, job_id)` on a dedicated connection
held for the whole run (`jobs/locking.py`); one transaction per ride (expire) / per batch (purge)
with `SKIP LOCKED`; summary line + exit codes (`jobs/reporting.py`, commit `6637650` on
`feat/job-advisory-lock`).

Contract a scheduler relies on:

```
job=expire_bookings status=ok expired=3 closed=1 skipped_locked=0 duration_ms=42     exit 0
job=expire_bookings status=skipped duration_ms=5                                      exit 0   (another run holds the lock)
job=expire_bookings status=error error=ConnectionDoesNotExistError duration_ms=17     exit 1   (traceback on stderr)
```

Only the exception class is printed on the summary line (messages can contain SQL parameters).

Proved by `tests/test_job_recovery.py` (9 tests) + `tests/test_job_locking.py` (10), real PostgreSQL 17:

| Scenario | How it is forced | Proven outcome |
|---|---|---|
| Exception mid-batch | `lifecycle.apply_booking_transition` raises on ride 2 of 3 | ride 1 stays `expired` (committed), rides 2–3 `pending` (rolled back), lock released, seats unchanged; rerun expires exactly 2; third run 0 |
| **Process killed (`kill -9`)** | child process runs `jobs.expire_bookings.main()`, parks *inside* ride 2's transaction (row lock held, uncommitted), parent sends SIGKILL | before the kill: the advisory lock is held by the child's own backend (pg_locks pid ∈ child's sessions). After: PostgreSQL ends both sessions when the sockets close → **advisory lock and row locks released with no Python cleanup**; ride 1 committed, ride 2 rolled back; `SELECT … FOR UPDATE NOWAIT` on all rides succeeds; rerun CLI `status=ok expired=2`, next run `expired=0` |
| Second worker during a run | in-process `run()` and a second CLI process while the child holds the lock | `None` / `job_lock=skipped` + `status=skipped`, exit 0, no rows touched |
| DB connection drop (work session) | `pg_terminate_backend(<work pid>)` while parked | exit **1**, last line `status=error`, traceback on stderr, no success counters; lock released by the explicit unlock; ride 1 kept; rerun finishes |
| DB connection drop (lock session) | `pg_terminate_backend(<lock pid>)` while parked | lock free immediately (another runner could start — safe because of SKIP LOCKED); the job finishes its rows but exits **1** (`status=error`) because it could not confirm the unlock: never a silent ok after losing exclusivity |
| DB unreachable | `DATABASE_URL` → closed port | exit 1 + `status=error`, both jobs |
| Bad arguments | `--batch-size 0` | exit 2 |

Mutation checks (each must make the suite fail; all did): exception handler removed → 4 tests fail;
lock never released → 7; transaction-level lock (`pg_try_advisory_xact_lock`) → 9; per-ride commit
replaced by flush (one transaction for the whole sweep) → 6.

**Retries / backoff.** The workflow retries once after 60 s inside the same run (transient Neon
cold start or network). It does not loop: the next scheduled tick 15 min later is the real retry,
and the jobs resume from state (no cursor), so a missed or failed run costs only latency. Three
consecutive red runs → investigate (BACKGROUND_JOBS §5 has the playbook).

**Restarts / deploys.** Jobs are separate processes; an API deploy never interrupts them. Pause the
schedule (disable the workflow) during `alembic upgrade`/`downgrade` of `c1a7e0b00002` and any
rollback remapping `expired`.

## 4. Alerting

| Signal | Mechanism | Threshold | Route |
|---|---|---|---|
| Job failed | red workflow run (exit 1 after one retry) → GitHub notification e-mail | every failure | the user who last changed the cron line → must be **Maaz**; enable Actions failure e-mails in his notification settings |
| Job not running (schedule disabled after 60 days, default branch wrong, secret revoked, runner outage) | dead-man heartbeat: the workflow calls `JOBS_HEARTBEAT_URL` after success | no ping for **45 min** (expire) / **26 h** (purge) | Maaz phone/e-mail; vendor is Maaz's choice |
| API down | external uptime monitor on **`/readyz`** (not `/test-db`, retired by #7) | 1-min interval, alert after 2 consecutive failures | Maaz phone |
| Backlog (sweep silently not effective) | read-only SQL from BACKGROUND_JOBS §6.3 (due pending > 1 h old; stale active rides > 48 h) | > 0 on 2 checks | weekly manual check until a dashboard exists |
| Stuck lock | `status=skipped` 3 runs in a row, or the stuck-lock query (BACKGROUND_JOBS §6.3) | > 15 min | Maaz; terminating a prod backend needs approval |

Heartbeat table: **not now** (needs a migration and an INSERT grant). Design for a later revision
after `d5e1a0b00002`: `job_runs(id bigserial, job text, started_at timestamptz, finished_at
timestamptz, status text CHECK (status IN ('ok','skipped','error')), counters jsonb, duration_ms int)`
with an index on `(job, started_at DESC)`, written by `jobs/reporting.py` at the end of each run with
a best-effort insert on a separate connection; retention 90 days via the purge job. Until then the
summary line in the Actions log is the history.

## 5. Ownership (RACI)

R = does it, A = accountable/approves, C = consulted, I = informed.

| Activity | Maaz | Hark | Codex Astra |
|---|---|---|---|
| Approve scheduling and default-branch change (LB-2) | **A/R** | C | I |
| Create `rideshare_jobs` DB role (least privilege) and `JOBS_DATABASE_URL` secret (direct host) | **A/R** | C (SQL proposal in SECURITY_REVIEW_RC §5) | I |
| Commit the workflow from `docs/release/workflows/` to `.github/workflows/` | A | **R** (PR) | C (review) |
| Heartbeat + uptime monitor accounts and routing | **A/R** | C (config) | I |
| Respond to a failed-run / missed-heartbeat alert | **A/R** | R (diagnosis on request, read-only) | I |
| Code changes to jobs, tests, runbook | A | **R** | C (review) |
| Production data fix after a bad sweep (remap SQL) | **A/R** | C (proposal) | C |
| Quarterly review of thresholds and the 60-day-inactivity risk | A | **R** | I |

## 6. Ready-to-commit-later workflow

[`workflows/maintenance-jobs.yml`](workflows/maintenance-jobs.yml) — **kept under `docs/`, not in
`.github/workflows/`**, so it cannot run. It differs from the BACKGROUND_JOBS §2.1 sketch by: Python
3.11 and `pip install -r requirements.txt` exactly like `ci.yml`, `persist-credentials: false`, one
in-run retry with 60 s backoff, `set -o pipefail` so a failing job is not masked by `tee`, the summary
line copied to the run summary, and an optional heartbeat that is skipped when the secret is unset.

Activation checklist (all approval-gated): (1) LB-2 default branch; (2) `rideshare_jobs` role with
SELECT/UPDATE on `rides`/`bookings`, SELECT/DELETE on `idempotency_keys`, nothing else; (3) Actions
environment `production-jobs` with secret `JOBS_DATABASE_URL` (direct host) and optional
`JOBS_HEARTBEAT_URL`; (4) PR moving the file to `.github/workflows/`; (5) one manual
`workflow_dispatch` run of each job, check the summary line; (6) enable the heartbeat alert.
