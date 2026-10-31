# Migration readiness: production baseline → `d5e1a0b00002`

Prepared 31 Oct 2026 (staging-ops). **Nothing here was run against production, staging or Neon.**
Every step that touches a Neon database (even read-only) is approval-gated and is Maaz's to run.
Evidence was produced on disposable local PostgreSQL 17 databases (`rs_so_*`, C.UTF-8).

Related: [SECURITY_REVIEW_RC.md §10](SECURITY_REVIEW_RC.md) (per-revision rollback table, first
version), [PREFLIGHT_REVIEW.md](PREFLIGHT_REVIEW.md), [NEON_STAGING_PLAN.md](NEON_STAGING_PLAN.md),
[BACKUP_RESTORE_TEST.md](BACKUP_RESTORE_TEST.md), `docs/testing/CONCURRENCY_AND_MIGRATION_EVIDENCE.md`
on PR #27.

## 1. The chain

| # | Revision | PR | What | Lock profile | Reversible? |
|---|---|---|---|---|---|
| 0 | `d924a6e38f10` | (on `rideshare-v2` `e8da425`) | drop redundant id indexes | — | presumed production baseline |
| 1 | `e3a7c41b9f52` | #3 | 3 FK/listing indexes, `CREATE INDEX CONCURRENTLY` in autocommit, repairs INVALID leftovers | SHARE UPDATE EXCLUSIVE (writes continue) | yes, drops concurrently |
| 2 | `f6b2d8e0c413` | #5 | preflight (aborts on `lower(email)` twins, prints ids only), then unique index `uq_users_email_lower` (non-concurrent) | SHARE on `users` for the build (writes to `users` wait, seconds at current size) | yes (drop index) |
| 3 | `c1a7e0b00001` | #18 | `idempotency_keys` table + index | new table only | yes (keys are disposable) |
| 4 | `c1a7e0b00002` | #20 | `bookings.expires_at`, enum label `expired` (`ADD VALUE`, autocommit), partial index CONCURRENTLY | `lock_timeout 5s` on the `ALTER`s | **partly**: label cannot be dropped; downgrade remaps `expired → cancelled` |
| 5 | `c1a7e0b00003` | #22 | `bookings.seats_requested` default 1 + CHECK added `NOT VALID` then `VALIDATE` | `lock_timeout 5s`; validate takes SHARE UPDATE EXCLUSIVE | **refuses** while any booking has `seats_requested <> 1` |
| 6 | `d5e1a0b00001` | #19 | `users.account_status`, `users.token_version` + CHECKs | `lock_timeout 5s`, ACCESS EXCLUSIVE for metadata-only `ADD COLUMN … DEFAULT` | **lossy**: suspended state and token versions dropped |
| 7 | `d5e1a0b00002` | #21 | `admin_audit_log` + append-only triggers | new table only | **lossy**: audit history dropped |

`alembic/env.py` runs with `transaction_per_migration=True`: each revision commits with its own
`alembic_version` stamp, so an interrupted multi-revision upgrade always stops on a whole revision.

## 2. What is production's current revision? (unverified — how to verify)

**Presumed: `d924a6e38f10`.** Evidence, strongest first:

1. `rideshare-v2` @ `e8da425` (the newest code that could have been deployed) has exactly four
   revisions, head `d924a6e38f10` (`git ls-tree e8da425 alembic/versions/`). Nothing later exists
   on any merged branch.
2. Live app behaviour, passive GET of `https://rideshare-api.fastapicloud.dev/openapi.json`
   (31 Oct 2026 ~13:30 PKT): `BookingStatus` = `pending, accepted, rejected, cancelled` (no
   `expired`); `BookingResponse` has no `seats_requested`; no `/admin` or `/healthz` paths;
   `UserResponse.allowed_modes` present (commit `dc40207`, 11 Oct). So the deployed **code** predates
   every RC revision. `/test-db` 200, `/healthz` and `/readyz` 404.
3. What this cannot tell us: whether the **database** is at `d924a6e38f10`, at an earlier revision
   (`b83d12f7a906`, `7c2e9a4b6d10`), or was created by `Base.metadata.create_all` and never stamped
   (the pre-Alembic-authoritative era). The API surface is identical in all three cases.

**Verification (approval-gated, read-only, Maaz runs it in the Neon SQL Editor on the production
branch or with a read-only role):**

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SELECT version_num FROM alembic_version;                        -- expect exactly one row: d924a6e38f10
SELECT to_regclass('public.alembic_version') IS NOT NULL AS stamped,
       (SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND tablename IN ('users','rides','bookings')) AS index_count,
       (SELECT string_agg(indexname, ',' ORDER BY indexname) FROM pg_indexes
         WHERE schemaname='public' AND tablename IN ('users','rides','bookings')) AS index_names;
ROLLBACK;
```

| Result | Meaning | Action |
|---|---|---|
| `d924a6e38f10` | baseline as presumed | proceed with §6 |
| `b83d12f7a906` / `7c2e9a4b6d10` | older Alembic state | `alembic upgrade head` still works (the chain starts at `50872040220b`); rehearse from that revision on the Neon branch first |
| `relation "alembic_version" does not exist` | schema created without Alembic | **stop**: compare `index_names` / columns with a local DB at `d924a6e38f10`, then `alembic stamp d924a6e38f10` only if identical (a write; separate approval) |
| two rows / unknown id | someone ran a branch build against prod | stop and investigate |

## 3. Rehearsal from the presumed baseline (local, this session)

Code: RC integration `integration/rc-local` @ `52e266b` (rideshare-v2 `e8da425` + PRs #2–#24),
checked out read-only with `git worktree add --detach /workspace/work/rsso-rc 52e266b`.
Checker: `tests/tools/m3_db_checks.py` from PR #27 (`0c11eed`), copied to
[`tools/m3_db_checks_rc.py`](tools/m3_db_checks_rc.py) with three changes: `M3_ROOT` selects the code
tree, the `rs_so` database prefix is allowed, and a `noop` check (repeat execution) was added.
Total on the RC: **30/30** (`legacy noop downgrade` 16/16, `interrupt` 14/14).

```bash
export PGPASSWORD=… M3_PG=postgresql://rideshare_ci@localhost:5432 M3_PYTHON=$PY \
       M3_ROOT=/workspace/work/rsso-rc M3_DB_PREFIX=rs_so_m3
$PY -B docs/release/tools/m3_db_checks_rc.py legacy noop downgrade   # 16/16
$PY -B docs/release/tools/m3_db_checks_rc.py interrupt                # 14/14
``` Seed at `d924a6e38f10`: 5 users incl.
mixed-case twins and an admin, 5 rides (two with 0 seats, one departed-but-active, one cancelled),
9 bookings across all statuses. Logs: `/workspace/work/rsso-m3-rest.log`,
`/workspace/work/rsso-m3-interrupt.log` (one JSON line per result).

| Check | Result on RC `52e266b` | PR #27 result (`0c11eed`, stack up to #21) |
|---|---|---|
| Preflight blocks `lower(email)` twins (exit 1, stamp stays `e3a7c41b9f52`, ids only, rows untouched) | **7/7** | 7/7 legacy |
| After operator fix: upgrade to `d5e1a0b00002`, legacy rows byte-identical (md5 fingerprint), `seats_requested=1`, `expires_at` NULL, 0 invalid indexes, 0 NOT VALID constraints, `alembic check` clean | (in legacy row above) | — |
| **Repeat execution**: `alembic upgrade head` ×3 on an at-head DB → exit 0 each, same stamp, schema dump identical, rows identical | **1/1** | not covered |
| `kill -9` of the alembic process while each of the 7 revisions is parked on a lock, 2 variants (orphan backend terminated / orphan allowed to finish) → stamp = previous revision, retry converges to a schema identical to a clean head | **14/14** (stamp after kill = previous revision in every case) | 14/14 |
| Downgrade head → each revision → head with data; schema = clean build except the undroppable `expired` label; multi-seat refusal | **7/7 + 1/1** | 7/7 + 1/1 |

## 4. Irreversible boundaries

| Boundary | Revision | What is lost or cannot be undone | Mitigation before crossing | Evidence |
|---|---|---|---|---|
| Enum label `expired` | `c1a7e0b00002` | PostgreSQL cannot drop an enum value. After downgrade the label stays in `bookingstatus` (harmless to old code: never produced, never selected). Downgrade rewrites every `expired` booking to `cancelled` — the distinction is gone. | Export first: `\copy (SELECT id FROM bookings WHERE status='expired') TO 'expired_ids.csv' CSV`. Prefer forward-fix. | downgrade check ignores exactly this diff (m3 `downgrade`) |
| Audit-log history | `d5e1a0b00002` | Downgrade drops `admin_audit_log` and its triggers: every admin action record is deleted. | `\copy admin_audit_log TO 'audit_<date>.csv' CSV HEADER` by the migrator role; keep the file encrypted (contains actor/target ids). | migration code; SECURITY_REVIEW_RC §10 |
| Suspension state + token versions | `d5e1a0b00001` | Downgrade drops `account_status` and `token_version`: **every suspended user becomes active and their old tokens (still within 60 min) are accepted again.** | Export suspended ids; after downgrade, if any suspended user matters, rotate `SECRET_KEY` (invalidates all tokens) or keep them blocked another way. | m3 `tokens` check (PR #27) |
| Multi-seat counts | `c1a7e0b00003` | Downgrade **refuses** (`MultiSeatDowngradeRefused`, nothing changed) while any booking has `seats_requested <> 1`; forcing it would erase how many seats each passenger holds and break seat conservation. | Do not downgrade. Use the kill-switch (§5). | m3 `downgrade` "refused while a 2-seat booking exists" |
| Email lowercasing / `uq_users_email_lower` | `f6b2d8e0c413` | The migration never rewrites emails (identity is `lower(email)` at query time), so nothing is lost. The boundary is the **preflight**: it aborts on case twins, and resolving twins is a manual, per-account data decision that cannot be undone automatically. | Run the read-only preflight (PREFLIGHT_REVIEW.md, `sql/prod_email_preflight.sql` rev 2) first; written decision per twin group. | m3 `legacy`; PREFLIGHT_REVIEW §4 |
| Concurrently built indexes | `e3a7c41b9f52`, `c1a7e0b00002` | Not lossy. `CREATE INDEX CONCURRENTLY` cannot run in a transaction; if interrupted it leaves an INVALID index, which the next `upgrade` drops and rebuilds. It waits for every older transaction, so a long-running transaction stalls it. | Check `pg_stat_activity` for transactions older than 1 min; pause the job schedule during the upgrade. | m3 `interrupt` (both variants) |

## 5. Rollback strategy per revision

Principle: **revert code first, keep the schema, forward-fix.** Every RC revision is additive and
safe under the currently deployed code except `f6b2` (old code turns a case-variant duplicate
registration into a 500 instead of a 409). Downgrades are a last resort and only for the
revisions whose rows matter less than the outage.

| Revision | Preferred rollback | Downgrade (only if needed) | Data proposals (SQL **proposals**, approval-gated, never run by Hark) |
|---|---|---|---|
| `d5e1a0b00002` audit | keep table; revert code only (old code never touches it) | `alembic downgrade d5e1a0b00001` after export | `\copy admin_audit_log TO 'audit.csv' CSV HEADER` |
| `d5e1a0b00001` suspension | keep columns; revert code (old code ignores them; suspended users regain access through old code anyway) | `alembic downgrade c1a7e0b00003` after export | `\copy (SELECT id, token_version FROM users WHERE account_status='suspended') TO 'suspended.csv' CSV` |
| `c1a7e0b00003` multi-seat | **kill-switch** `BOOKING_MAX_SEATS=1` (env, approval-gated; new requests limited to 1 seat, existing multi-seat bookings stay valid), then forward-fix | only when zero rows have `seats_requested <> 1`; otherwise it refuses | Remap proposal for a forced exit (each N-seat booking becomes 1 seat, the N−1 seats return to the ride): `BEGIN; LOCK TABLE rides, bookings IN SHARE ROW EXCLUSIVE MODE; UPDATE rides r SET available_seats = r.available_seats + s.extra FROM (SELECT ride_id, sum(seats_requested-1) extra FROM bookings WHERE status='accepted' AND seats_requested>1 GROUP BY ride_id) s WHERE r.id=s.ride_id; UPDATE bookings SET seats_requested=1 WHERE seats_requested>1; -- verify seat conservation, then COMMIT` — **this changes what passengers bought; needs a product decision and passenger notice.** |
| `c1a7e0b00002` expiry | stop the sweep schedule; revert code; keep column + label | `alembic downgrade c1a7e0b00001` (remaps `expired → cancelled` itself) | old code shows unknown `expired` rows badly: `UPDATE bookings SET status='cancelled' WHERE status='expired'` after export (PR #20 Rollback 1–4) |
| `c1a7e0b00001` idempotency | revert code; keep table | `alembic downgrade f6b2d8e0c413` | none (keys are replay caches, TTL 24 h) |
| `f6b2d8e0c413` email index | downgrade **before** redeploying old code | `alembic downgrade e3a7c41b9f52` | none |
| `e3a7c41b9f52` indexes | keep (compatible with all code) | `alembic downgrade d924a6e38f10` | none |

`alembic downgrade` from head to below `c1a7e0b00003` while multi-seat rows exist stops at
`c1a7e0b00003` **after** having already dropped the suspension and audit columns/tables (per-revision
commits). Never run a multi-step downgrade blind: step one revision at a time.

## 6. Deploy order and the single recommended production sequence

| Revision | Order constraint |
|---|---|
| `e3a7` | none |
| `f6b2` | preflight `case_duplicate_groups = 0` first; new code tolerates no index; old code + index → 500 on case-variant duplicate register (exposure = window between migrate and code deploy) |
| `c1a7…01`, `c1a7…02` | **migrate before code** (new code reads `idempotency_keys` and `expires_at`) |
| `c1a7…03`, `d5e1…01`, `d5e1…02` | **migrate before code** (new code selects the new columns/table); all additive with defaults, old code unaffected |

**Recommended sequence (one maintenance window, off-peak, after a green Neon-branch rehearsal of the
exact same steps — NEON_STAGING_PLAN.md):**

1. Gate: PRs merged, CI green on the merge commit; Maaz approval for this window.
2. Read-only checks (Maaz): §2 `version_num` = `d924a6e38f10`; `sql/prod_email_preflight.sql` rev 2
   → `case_duplicate_groups = 0`; no transaction older than 1 min on `users/rides/bookings`.
3. Backup: create a Neon branch / snapshot named `pre-rc-<date>` and note the LSN/time
   (BACKUP_RESTORE_TEST.md §2). Confirm the history window covers ≥ 24 h.
4. Job schedule paused (not yet created at first launch — nothing to do).
5. `DATABASE_URL=<direct, migrator role> alembic upgrade head` with the **old code still serving**.
   If interrupted: re-run the same command (proved idempotent and convergent, §3). If the preflight
   aborts: stop, nothing changed beyond `e3a7`.
6. Verify: `SELECT version_num FROM alembic_version` = `d5e1a0b00002`; `SELECT count(*) FROM pg_index
   WHERE NOT indisvalid` = 0; `SELECT count(*) FROM pg_constraint WHERE NOT convalidated` = 0.
7. Deploy the RC build (`BOOKING_MAX_SEATS` per LB-11 decision; `1` is the conservative start) →
   STAGING_SMOKE_CHECKLIST.md §4 against production (read-mostly subset).
8. Then (separate approval) enable the job schedule (SCHEDULER_AND_OPS.md) and uptime monitor.

Exposure accepted by this sequence: between steps 5 and 7, a case-variant duplicate registration
returns 500 instead of 409 (old code + new unique index). The zero-exposure alternative is two
deploys (`upgrade e3a7` → deploy a build with #5 only → `upgrade head` → deploy RC); not recommended
because it needs an intermediate build that was never tested as a unit.

Rollback for the window: redeploy the previous build (it works on the upgraded schema). Immediately
after step 5 the new tables/columns are still empty (no suspended users, no audit rows, no multi-seat or
`expired` bookings), so a full `alembic downgrade d924a6e38f10` at that moment loses nothing — but once
the RC build has served traffic, the lossy boundaries of §4 apply: keep the schema and fix forward. A
data problem → Neon restore to the `pre-rc-<date>` point (BACKUP_RESTORE_TEST.md), accepting the loss
of writes since then.
