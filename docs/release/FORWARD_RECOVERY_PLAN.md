# Forward-recovery and backup-validation plan for the RC migrations

Prepared 31 Oct 2026 (a1-docs). Complements MIGRATION_READINESS.md §4–§6 (irreversible boundaries,
per-revision rollback, recommended sequence) and BACKUP_RESTORE_TEST.md (drills). Everything here that
touches production or Neon is **approval-gated (A3 restore point, A8 deploy+migrate)** and is Maaz's to
run. Hark ran every script below only on local disposable databases with synthetic data (§8).

Principle (unchanged): **revert code first, keep the schema, fix forward.** Restore from backup only when
data is lost or corrupted and cannot be repaired forward (decision tree §6).

## 1. Files

| File | Kind | Runs where |
|---|---|---|
| [`sql/forward_recovery_detect.sql`](sql/forward_recovery_detect.sql) | READ-ONLY detection, aggregates + object names, safe at any revision d924 → head | psql or Neon SQL Editor |
| [`sql/forward_fix_indexes.psql`](sql/forward_fix_indexes.psql) | DDL: drop INVALID / create missing concurrent indexes, idempotent | **psql only** (`\gexec`; CONCURRENTLY cannot run in a transaction) |
| [`sql/pre_migration_export.psql`](sql/pre_migration_export.psql) | READ-ONLY `\copy` export of what downgrades destroy | psql only (writes files on the operator's machine) |
| [`sql/reapply_after_downgrade.psql`](sql/reapply_after_downgrade.psql) | WRITES: restores expired statuses, suspensions (+token bump), audit history after downgrade + re-upgrade | psql only |
| [`sql/restore_verify.sql`](sql/restore_verify.sql) | READ-ONLY fingerprint for backup/restore diff (existing) | psql or editor |
| [`tools/forward_recovery_selftest.sh`](tools/forward_recovery_selftest.sh) | local proof of all of the above | local only |

## 2. Boundaries, failure modes, detection, forward fix

| # | Boundary | Failure modes | Detection (`forward_recovery_detect.sql` row) | Forward fix | Lossy? |
|---|---|---|---|---|---|
| B1 | `e3a7c41b9f52` FK listing indexes (CONCURRENTLY) | build interrupted (deploy timeout, kill, `lock_timeout`, cancelled because an old transaction blocked it) → **INVALID** index; stalls while any transaction older than the build is open | 20 `invalid_or_unready_indexes`; 22 `expected_indexes_missing_for_revision`; 80 sessions in a transaction > 60 s; 82 build in progress | **F1** If `alembic_version` was not stamped: rerun `alembic upgrade head` (the migration drops the INVALID index and rebuilds). If stamped but INVALID/missing (manual interference): `forward_fix_indexes.psql`. Never `REINDEX` without `CONCURRENTLY` on production | No |
| B2 | `f6b2d8e0c413` email identity (`uq_users_email_lower`) | aborts on case twins (`CaseDuplicateEmails`, ids only, nothing changed, stamped `e3a7`); after it: old code (pre-RC) inserting a case variant gets a 500 on the unique violation | 10 revision `e3a7c41b9f52`; 60 `lower(email) duplicate groups` > 0 | **F2** per-group human decision (merge/rename; PREFLIGHT_REVIEW §4), then rerun upgrade. Never auto-merge. Never drop the index to "get past" it | No (the migration rewrites no email) — the twin resolution itself is a manual decision |
| B3 | `c1a7e0b00002` expiry (`expires_at`, enum `expired`, partial index CONCURRENTLY) | ADD VALUE/column hit `lock_timeout = 5s` → migration fails, retry; index INVALID (as B1); downgrade rewrites `expired` → `cancelled` and leaves the enum label | 40 enum label without column (downgraded); 41 expired count; 42 pending past expiry (job not running); 20 | rerun upgrade (idempotent: `IF NOT EXISTS`, INVALID index rebuilt); after a downgrade: **F3** `reapply_after_downgrade.psql` restores `expired` from the export | Downgrade is lossy without the export |
| B4 | `c1a7e0b00003` multi-seat | ADD COLUMN / VALIDATE hits lock timeout → retry; downgrade **refused** while any booking has `seats_requested <> 1` | 51 seats outside 1..8 (must be 0, CHECK); 52 multi-seat count; 50 negative `available_seats`; 53/54 live-booking invariants | rerun upgrade; **never downgrade with multi-seat rows** — kill-switch `BOOKING_MAX_SEATS=1` (MIGRATION_READINESS §5) and fix forward | Downgrade would be lossy → refused by design |
| B5 | `d5e1a0b00001` suspension / `token_version` | CHECK validation scan under lock; downgrade drops both columns → every suspended user active again, old tokens valid again | 70 suspended users vs export | rerun upgrade; after a downgrade: F3 restores `account_status` and sets `token_version = max(old, current) + 1` so tokens minted meanwhile die | Downgrade lossy without export |
| B6 | `d5e1a0b00002` audit log | downgrade drops `admin_audit_log` + triggers (all history gone); triggers disabled by someone | 71 audit rows vs export; 72 triggers enabled = 2 | F3 re-inserts history with original ids (INSERT is allowed by the append-only triggers) and resets the sequence | Downgrade lossy without export |

Not a migration boundary but checked by the same file: 30 NOT VALID constraints; 21 `_ccnew/_ccold`
leftovers of an interrupted `REINDEX CONCURRENTLY`; 81 lock waiters.

## 3. Detection: when to run it

`psql "$READONLY_URL" -X -v ON_ERROR_STOP=1 -A -F ' | ' -f docs/release/sql/forward_recovery_detect.sql`
(or paste into the Neon SQL Editor; READ ONLY + ROLLBACK, aggregates only).

1. Before A8 (with A1): baseline. Rows 20/21/30 must be empty, 80 = 0 (no long transactions; else
   `CREATE INDEX CONCURRENTLY` waits on them).
2. Immediately after any failed or interrupted `alembic upgrade`.
3. After A8 completes: expect revision `d5e1a0b00002`, rows 20/21/22/30 empty, 72 = 2.
4. After any rollback/recovery action, and compare 41/70/71 with the export file line counts.

## 4. Pre-migration backup requirement (gate for A8)

All of the following are recorded in the ops log **before** `alembic upgrade head` runs on production:

| # | What | How | Why |
|---|---|---|---|
| P1 | **Restore point**: UTC timestamp `T0` taken right before the upgrade; confirm `T0` lies inside the history window | Neon Console → project settings → history window (Free 6 h, Launch default 1 day up to 7 d, Scale up to 30 d — [plans](https://neon.com/docs/introduction/plans)); write down `T0` and the LSN if shown | instant restore / PITR branch to `T0` |
| P2 | **Snapshot** (if the plan allows: Free 1 manual, Launch/Scale 100) | Neon Console → Backup & restore → create snapshot of production, name `pre-rc-<yyyymmdd>` | restore point independent of the history window |
| P3 | **Logical dump** of the affected tables | `pg_dump "$DIRECT_URL" -Fc --no-owner -t users -t rides -t bookings -t alembic_version -f pre_rc_<date>.dump` (adds `-t idempotency_keys -t admin_audit_log` once they exist); encrypted at rest; operator machine | survives a project-level mistake; restorable anywhere |
| P4 | **Safety export** (small, targeted) | `pre_migration_export.psql` (at d924 it exports only row counts; after head it also exports expired ids, suspensions, audit log, multi-seat rows — run it again before **any** downgrade) | F3 forward re-apply |
| P5 | **Backup validated** | restore P3 into a local/scratch DB → `restore_verify.sql` on scratch and on production (read-only) → outputs identical except timestamps after `T0` | a backup nobody restored is not a backup |
| P6 | A1 grid + detection grid saved | A1_EXECUTION_INSTRUCTIONS | baseline for diffing |

P1 and P3 are mandatory; P2 if available; P5 must pass before A8 starts.

## 5. Forward fixes (scripts)

- **F1 invalid/missing index** — `psql "$MIGRATOR_DIRECT_URL" -X -v ON_ERROR_STOP=1 -f docs/release/sql/forward_fix_indexes.psql`.
  Reports every INVALID index, drops only RideShare's four concurrent indexes if INVALID
  (`DROP INDEX CONCURRENTLY`), recreates what is missing with the migrations' exact definitions
  (`CREATE INDEX CONCURRENTLY IF NOT EXISTS`, `lock_timeout 5s`), verifies zero INVALID. Idempotent. In the
  Neon SQL Editor run the printed statements one at a time instead (each must be its own statement).
  Before running: row 80 = 0 (long transactions make the build wait).
- **F2 case twins** — per-group decision, then rerun `alembic upgrade head`. No script by design.
- **F3 after an emergency downgrade** — order: `pre_migration_export.psql` (BEFORE the downgrade) →
  `alembic downgrade <rev>` → fix → `alembic upgrade head` → `reapply_after_downgrade.psql` in the export
  directory. One transaction; restores `expired` statuses (only rows still `cancelled`), suspensions with
  token bump, audit history with original ids + sequence. A second run fails on the audit primary key
  (no double insert). Record the recovery in the ops log (the script adds no audit row: the app accepts
  only its own action codes).
- **Kill-switch instead of downgrade (B4)** — `BOOKING_MAX_SEATS=1` env + redeploy; schema stays.

## 6. Decision tree: fix forward vs restore

```
Incident during/after A8
├─ Is data LOST or CORRUPTED (rows missing/wrong, not just a failed DDL)?
│   ├─ No ──► FIX FORWARD
│   │        ├─ upgrade failed/interrupted ──► run detection ──► F1 / F2 / rerun upgrade
│   │        ├─ app errors on new schema ──► revert CODE to previous deploy (schema is additive, old code runs on it
│   │        │                                except f6b2 case-variant inserts → 500, acceptable short-term) ──► fix code
│   │        └─ multi-seat / expiry behaviour wrong ──► kill-switch env (BOOKING_MAX_SEATS=1, pause jobs) ──► fix code
│   └─ Yes
│        ├─ Loss limited to what a downgrade dropped and the P4 export covers ──► F3 re-apply
│        ├─ Loss limited to a few tables, P3 dump recent enough ──► restore those rows from P3 into a scratch DB,
│        │                                                         copy back the delta (reviewed SQL, approval)
│        └─ Broad corruption, or unknown extent
│             ├─ Within history window ──► PITR: create branch at T0 (non-destructive) ──► verify with restore_verify
│             │       ──► decide: instant-restore production to T0 (writes after T0 are lost) or copy delta forward
│             └─ Outside window ──► restore P2 snapshot or P3 dump (RPO = time since dump)
```

Restore is the last resort because every write after `T0` (bookings made during the release) is lost
unless replayed; fix-forward keeps them.

## 7. RPO / RTO for the release window

| Scenario | RPO | RTO (target) | Basis |
|---|---|---|---|
| Failed/interrupted migration, no data loss | 0 | 15 min (detect + rerun / F1) | migrations are idempotent and stamped per revision; local interrupt evidence (#27 m3 `interrupt`) |
| Emergency downgrade with P4 export | 0 (for exported state) | 30 min | F3 tested (§8) |
| PITR to `T0` | writes after `T0` | 1 h | BACKUP_RESTORE_TEST §1 (proposed 24 h/1 h overall; for the release window the restore point is minutes old) |
| Snapshot / dump restore | time since P2/P3 (minutes during the release) | 1–2 h | P5 validated restore |

General targets (24 h RPO / 1 h RTO) stay as proposed in BACKUP_RESTORE_TEST.md; on the Free plan the 6-h
window and 1 manual snapshot mean the P3 dump carries the 24 h RPO after the release window.

## 8. Local evidence (Hark, 31 Oct 2026; PostgreSQL 17.11; RC migrations from `integration/rc-final` @ 7d66964)

`ALEMBIC_DIR=… VENV=… bash docs/release/tools/forward_recovery_selftest.sh` → **25 passed, 0 failed**:

1. **INVALID concurrent index**: on a seeded head DB, `ix_bookings_pending_expires_at` dropped, then
   `CREATE INDEX CONCURRENTLY` started while a REPEATABLE READ transaction held an old snapshot and was
   cancelled (`pg_cancel_backend`) → INVALID index; detection row 20 lists exactly
   `bookings.ix_bookings_pending_expires_at` while `alembic_version` still says head (so `upgrade` would not
   rebuild it); `forward_fix_indexes.psql` exit 0 → none INVALID, definition equals the migration's
   (`WHERE (status = 'pending'::bookingstatus)`); rerun exit 0 (idempotent); a dropped
   `ix_bookings_ride_id_id` is reported by row 22 and recreated.
2. **f6b2 abort path**: d924 synthetic DB with 2 case-twin groups → `alembic upgrade head` aborts with
   `CaseDuplicateEmails` (output contains no `@`), detection shows revision `e3a7c41b9f52` and 2 groups;
   after renaming the newer twin per group the upgrade reaches head with 0 INVALID indexes.
3. **Export → downgrade → re-upgrade → re-apply**: seeded head (1-seat so the downgrade is allowed):
   export wrote expired ids / user status / audit log with no `@`, `+92` or 13-digit runs; downgrade to
   `c1a7e0b00001` → audit table gone, expired = 0, row 40 reports the left-over enum label; upgrade head +
   `reapply_after_downgrade.psql` → expired/suspended/audit counts and audit-content md5 identical to before
   (`15/4/4/<md5>`); suspended users' `token_version` ≥ 2; audit UPDATE still rejected; rerun refused.
4. **Backup validation**: `pg_dump -Fc` of a seeded head DB → `pg_restore` into a scratch DB exit 0 →
   `restore_verify.sql` identical on both; per-table dump (users, bookings, admin_audit_log) readable;
   negative control: deleting one booking in the restore makes the verify diff fail.

Not covered locally: Neon instant restore / snapshots / PITR branches (no Neon access; procedure in
BACKUP_RESTORE_TEST.md §2–§3), timing on production-sized tables.
