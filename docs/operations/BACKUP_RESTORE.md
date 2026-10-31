# Backup and restore (Neon PostgreSQL)

Production data lives in one Neon project (root branch = production). Every step
that touches the production project (creating a branch from it, restoring it,
running `pg_dump` against it) needs Maaz's approval. The data includes plaintext
CNIC and phone numbers (see `docs/privacy/CNIC_PHONE_PROTECTION.md`, PR #8), so
every copy, branch and dump is as sensitive as production itself.

References: Neon [Instant restore](https://neon.com/docs/postgres/backup-restore/branch-restore),
[History window](https://neon.com/docs/postgres/backup-restore/history-window),
[pg_dump backups](https://neon.com/docs/manage/backup-pg-dump).

## 1. What protects the data

| Layer | What it gives | Limit |
|---|---|---|
| Neon history window (PITR) | Restore the root branch, or branch from it, at any timestamp/LSN inside the window | Free plan: 6 h maximum (and 1 GB of history). Launch: default 1 day, max 7. Scale: default 1 day, max 30. **Confirm the plan and the configured window in Neon → Settings.** |
| Named restore point branch | A child branch created right before a risky change; survives after the window moves on | Costs storage for divergent data; delete when no longer needed |
| Logical dump (`pg_dump`) | Off-platform copy, survives loss of the Neon project or account | Point-in-time of the dump only; must be encrypted and access-controlled |

Proposed targets (for Maaz to accept): **RPO ≤ 24 h, RTO ≤ 1 h** for the MVP. A
6-hour Free-plan window means an error noticed the next morning cannot be undone
by PITR, so either a paid plan with a 7-day window or a daily dump is required to
meet a 24 h RPO in practice.

## 2. Before every risky change (migration, data fix, bulk delete)

1. Record the time in UTC and the current Alembic revision (`alembic current` against the branch you are about to change).
2. Create a child branch of production named `pre-<change>-<YYYYMMDD-HHMM>` (Neon console → Branches → New branch, parent = production, "head"). No compute is needed unless you will query it.
3. Note the branch name in the change record / PR. Delete it after the change has been stable for 7 days.

## 3. Recovery procedures

Pick the least destructive option that solves the problem.

### A. Selective recovery (preferred for "some rows were deleted or corrupted")

1. Use Time Travel Assist or a branch at a timestamp just before the incident (Neon → Branches → New branch → "Time" / LSN). Name it `recover-<incident>-<ts>`.
2. Connect to that branch **read-only** and confirm the rows you need exist (by ID).
3. Copy back only those rows to production, in a transaction, by primary key, with a reviewed SQL script (approval required). Respect FKs: `users` → `rides` → `bookings`.
4. Delete the recovery branch.

Production stays online; nothing after the incident is lost.

### B. Full instant restore of the production root branch (whole-database rewind)

Use only when the whole database must go back (e.g. a migration destroyed data and selective repair is impractical).

1. Announce downtime; consider stopping writes (scale the app to zero or deploy a maintenance build, if available).
2. Neon → production branch → Backup & Restore → **Restore from history**, choose timestamp or LSN, review, Restore.
3. What happens (per Neon docs): it is an **overwrite**, not a merge, of all databases on the branch; existing connections drop briefly; the connection string does not change; Neon keeps the pre-restore state as `{branch}_old_{timestamp}`.
4. Run `alembic current`. If the restore point predates a migration, the schema is now at the older revision: the deployed code must be compatible with it (see [ROLLBACK](ROLLBACK.md)) before traffic resumes.
5. Run the production smoke checklist ([SMOKE_TESTS](SMOKE_TESTS.md) §2).
6. Writes made after the restore point are gone from production but still exist in `{branch}_old_{timestamp}`; decide whether any must be replayed by hand.
7. To undo the restore: Instant restore again with the backup branch as the source.

### C. Restore from a logical dump (Neon project lost or unusable)

1. Create a new Neon project/branch (same PostgreSQL major version).
2. `pg_restore --no-owner --no-privileges -d "<new direct URL>" rideshare-<date>.dump`.
3. `alembic current` must match the dump's revision; then point `DATABASE_URL` at it (config change, approval).

## 4. Logical backup (recommended addition, not set up)

```sh
# direct (non-pooler) host; pg_dump major version >= server version
pg_dump --format=custom --no-owner --no-privileges \
  --file "rideshare-$(date -u +%Y%m%dT%H%MZ).dump" "$PROD_DIRECT_URL"
```

- Run from a trusted machine or a scheduled job with the URL in a secret store; never in CI logs or with `set -x`.
- Encrypt at rest (e.g. `age`/`gpg`) before upload; restrict the bucket; set retention (e.g. 14 daily, 8 weekly).
- The dump contains password hashes, CNIC and phone numbers; deleting a user later does not delete them from old dumps, so retention must match the privacy policy.

## 5. Restore drill (quarterly, and before the first real-user launch)

Nothing here touches the production root branch. Needs approval because it reads production data.

| # | Step | Pass criterion |
|---|---|---|
| D1 | Pick a target time T = now − 2 h (inside the window) | — |
| D2 | Create branch `drill-<date>` from production at T | branch created; record start time |
| D3 | `DATABASE_URL=<drill URL> alembic current` | matches the revision deployed at T |
| D4 | Row counts per table (`SELECT count(*)` on `users`, `rides`, `bookings`) | plausible vs. production; no errors |
| D5 | Integrity: `SELECT count(*) FROM bookings b LEFT JOIN rides r ON r.id=b.ride_id WHERE r.id IS NULL` (expect 0); `indisvalid` check from STAGING plan S5 | as expected |
| D6 | Optional: run a local build against the drill branch with its own throwaway `SECRET_KEY`; run [SMOKE_TESTS](SMOKE_TESTS.md) §1 read-only steps | 200s, login with a known **test** account only |
| D7 | `pg_dump` the drill branch and `pg_restore` into a disposable local DB | restore exits 0; counts match D4 |
| D8 | Delete `drill-<date>`; delete local copies | — |
| D9 | Record: date, T, time from D2 to D6 (= measured RTO), issues found | stored in the ops log |

Never print rows that contain CNIC, phone or email while drilling; use counts and IDs.
