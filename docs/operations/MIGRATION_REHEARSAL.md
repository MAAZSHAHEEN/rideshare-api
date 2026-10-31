# Migration rehearsal

Every schema migration is rehearsed on a Neon branch of production before it runs
on production. Running anything on a Neon branch, a preview or production needs
Maaz's separate approval.

The concrete Wave 1 plan (revisions `d924a6e38f10 → e3a7c41b9f52 (AST-003) →
f6b2d8e0c413 (AST-005)`, steps S1–S12, preflight, rollout and rollback) is the
lead's **Staging Migration Plan** (handover `STAGING_MIGRATION_PLAN.md`, 31 Oct
2026, not executed). This document is the reusable procedure it instantiates;
where they differ, the per-release plan wins.

## How migrations run here

- Alembic owns the schema. The app never creates or alters tables; startup only runs `SELECT 1` (`main.py` lifespan).
- `alembic/env.py` reads `DATABASE_URL` from the environment (via `database.py`), so the target is whatever that variable points to. Double-check it before every command:
  `python -c "from sqlalchemy.engine import make_url; import os; u=make_url(os.environ['DATABASE_URL']); print(u.host, u.database)"` (prints host and DB name only).
- From PR #3: `transaction_per_migration=True`, so each revision commits with its own version stamp; `CREATE INDEX CONCURRENTLY` runs in `autocommit_block()` and waits for open transactions on the table.
- Migrations are not part of any pipeline (audit O3). They are run by hand by the operator.

## Rehearsal procedure (per release)

| # | Step | Expected |
|---|---|---|
| R0 | PRs approved; CI green on the merged commit; release notes list each new revision with its lock behaviour and downgrade | — |
| R1 | Run any read-only preflight on production (approval), e.g. the lead's `prod_email_preflight.sql` for AST-005 (kept with the handover, not in this repo; reviewed, not yet approved to run) | counts/IDs only; blocking conditions resolved |
| R2 | Create Neon branch `staging-<release>-<date>` from production at "now" | own connection string |
| R3 | `alembic current` on the branch | equals the production revision |
| R4 | Long-transaction check: `SELECT pid, now()-xact_start AS age, state FROM pg_stat_activity WHERE xact_start < now()-interval '1 minute';` | none |
| R5 | `time alembic upgrade head` on the branch | exit 0; record duration (sizing for the prod window) |
| R6 | Validate: new indexes `indisvalid = true`, constraints present, `EXPLAIN` of affected queries | as designed |
| R7 | Deploy the merged build to a **preview** pointed at the branch (approval), with its own `SECRET_KEY` | preview healthy |
| R8 | [SMOKE_TESTS](SMOKE_TESTS.md) §1 + the release-specific functional checks | pass |
| R9 | Rollback rehearsal: `alembic downgrade <previous>` one revision at a time; app keeps working at each step with the code it will run then | pass; no data loss |
| R10 | Re-upgrade to head; R6 passes again | pass |
| R11 | Interrupted-run rehearsal for revisions that use `CONCURRENTLY`: cancel mid-build, re-run `upgrade head` | completes (e3a7 repairs INVALID indexes) |
| R12 | Delete the branch and preview; write results into the release PR | — |

## Production run (after a rehearsal pass and separate approval)

1. Restore point branch `pre-<release>-<ts>` ([BACKUP_RESTORE](BACKUP_RESTORE.md) §2).
2. Re-run the preflight.
3. Deploy order follows the release plan. For Wave 1: **code first, then `alembic upgrade head`** (old code turns a `uq_users_email_lower` conflict into a 500).
4. `alembic upgrade head` in an off-peak window; watch for lock waits.
5. Monitor 5xx, login failures and registration 409s for 30 minutes; run [SMOKE_TESTS](SMOKE_TESTS.md) §2.

## Writing future migrations (rules learned in Wave 1)

- Every revision must have a working `downgrade()` that deletes no data, or the PR must say why not.
- New constraints must not be violated by legacy rows: ship a preflight that aborts with counts and IDs only (never values), as `f6b2d8e0c413` does.
- Large-table indexes: `CREATE INDEX CONCURRENTLY` in `autocommit_block()`, idempotent (skip valid, rebuild INVALID).
- Expand → backfill → switch → contract for column changes (see the CNIC encryption design in PR #8); never rename/drop in the same release that stops using a column.
- `alembic upgrade head --sql` (offline) is useful for review, but revisions that inspect the database (e.g. `e3a7`'s INVALID-index repair, `f6b2`'s preflight) can only be fully exercised online on a branch.
