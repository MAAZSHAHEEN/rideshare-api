# Backup and restore test (drill) procedure

Prepared 31 Oct 2026 (staging-ops). Extends `docs/operations/BACKUP_RESTORE.md` (PR #11: what
protects the data and the three recovery procedures) with **how to prove a restore works**.
Every step touching the Neon production project is approval-gated and is Maaz's to run. Hark ran
only the local logical-dump drill in §5 on disposable databases.

## 1. Targets (proposed; Maaz accepts or changes)

| Target | Value | What it requires |
|---|---|---|
| **RPO** (max data loss) | **24 h** at launch, 1 h once there is real traffic | Neon history window ≥ 24 h (Launch: default 1 day, max 7; Scale: max 30; **Free: 6 h, 1 GB cap**), *or* a daily `pg_dump` (§4). Source: [history window](https://neon.com/docs/postgres/backup-restore/history-window), read 31 Oct 2026 |
| **RTO** (time to restored service) | **1 h** | A rehearsed procedure (this file), connection string unchanged after instant restore, code compatible with the restored schema revision |
| Drill cadence | **Quarterly**, plus after any plan change, Neon project move or major migration | Calendar reminder; record results in the ops log |
| Retention of drill branches | delete within 24 h (set branch expiration) | [branch expiration](https://neon.com/docs/guides/branch-expiration) |

With the Free plan's 6-hour window, a problem noticed the next morning is outside PITR: the 24 h RPO
then depends entirely on §4.

## 2. Drill A — point-in-time branch (non-destructive, the quarterly default)

Restores production **as of a timestamp** into a new branch; production is never touched.

1. Pick `T` = now − 1 h (inside the window). Record `T` in UTC and the current production
   `alembic_version` (read-only).
2. Neon Console → Branches → New branch → parent = production, **Time** = `T`, name
   `drill-<yyyymmdd>`, auto-delete after 1 day.
3. Start the stopwatch (RTO measurement starts at "decision to restore").
4. Connect to the drill branch read-only and run [`sql/restore_verify.sql`](sql/restore_verify.sql).
5. Run the same file on production (read-only) and diff. Expected: identical schema checks;
   row counts and `max(id)` on the drill branch ≤ production and consistent with `T`; fingerprints
   differ only by rows written after `T`.
6. Pass criteria (all): `alembic_version` equals the revision production had at `T`;
   `constraints.not_validated = 0`; `indexes.invalid = 0`; `indexes.expected_present = 5`;
   `triggers.audit_append_only = 2`; every `inv.*` = 0; every `seq.*_ok` = t.
7. Optional (recommended once): point a FastAPI Cloud preview at the drill branch (after the
   staging sanitization of NEON_STAGING_PLAN §5.1, since the branch contains real PII) and run
   STAGING_SMOKE_CHECKLIST §4 — proves the app starts on a restored database.
8. Stop the stopwatch; record elapsed time vs RTO. Delete the branch.

## 3. Drill B — instant restore of a root branch (destructive; rehearse on a copy first)

Instant restore "overwrites" the branch in place and keeps the previous state as
`<branch>_old_<timestamp>` ([instant restore](https://neon.com/docs/postgres/backup-restore/branch-restore)).
Never rehearse it on production. Rehearse on a disposable **root** branch (instant restore works only
on root branches): create a schema-only root branch, seed synthetic data with the API, note `T1`, make
a destructive change (`DELETE FROM bookings`), instant-restore to `T1`, run `restore_verify.sql`
before/after, confirm the `_old_` branch exists, delete everything. Then production drill B is only
ever used in a real incident (BACKUP_RESTORE.md §3B).

## 4. Logical backup alternative (`pg_dump`)

Use when the plan's window is shorter than the RPO, and as the off-platform copy in any case.

```bash
# Direct (non-pooler) host, a read-only role; custom format, compressed.
pg_dump -Fc --no-owner --no-privileges -d "$READONLY_DIRECT_URL" -f rideshare-$(date -u +%Y%m%dT%H%MZ).dump
# Encrypt before it leaves the machine (contains plaintext CNIC/phone until PRIVACY_CNIC_PHONE is done):
age -r "$BACKUP_PUBLIC_KEY" -o rideshare-….dump.age rideshare-….dump && shred -u rideshare-….dump
# Restore into a NEW empty database / branch (same PostgreSQL major):
pg_restore --no-owner --no-privileges -d "$NEW_DIRECT_URL" rideshare-….dump
```

- Schedule: daily (GitHub Actions cron in the same workflow family as SCHEDULER_AND_OPS, or a
  cron host Maaz controls); keep 7 daily + 4 weekly; storage with server-side encryption and
  access limited to Maaz. Neon's guide: [backup with pg_dump](https://neon.com/docs/manage/backup-pg-dump).
- Client `pg_dump` major version ≥ server major (Neon runs PostgreSQL 17 for new projects; confirm).
- Dumps include `alembic_version`, so a restored dump is at the dumped revision.
- The `admin_audit_log` triggers are restored as well (`pg_restore` restores them after data).

## 5. Local drill evidence (disposable databases, this session)

Source `rs_so_sanitize` at `d5e1a0b00002` (500 synthetic users, 50 rides, 50 two-seat bookings) →
`pg_dump -Fc --no-owner --no-privileges` → `pg_restore` into an empty `rs_so_drill_restored`:
exit 0 in ~1 s; `restore_verify.sql` output on source and restore **identical** (alembic
`d5e1a0b00002`; row counts; md5 fingerprints of rides, bookings and user state; 0 NOT VALID
constraints; 0 invalid indexes; 5/5 expected indexes; 2/2 audit triggers enabled; all invariants 0;
sequences ahead of data). The dump file was deleted afterwards.

## 6. Record template (ops log)

| Date | Drill | Plan / window | T | Elapsed (RTO) | Pass? | Notes | Branch deleted |
|---|---|---|---|---|---|---|---|
| | A / B / dump | | | | | | |
