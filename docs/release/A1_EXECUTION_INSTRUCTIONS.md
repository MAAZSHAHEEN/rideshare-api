# A1 — read-only production baseline + email preflight: execution instructions

Prepared 31 Oct 2026 (a1-docs). **A1 changes nothing.** It reads the production schema revision, a
catalog-only schema fingerprint, aggregate row counts and the email-preflight counts, then rolls back.
It returns no email, name, phone, CNIC, password hash or token. Hark has never connected to production,
staging or Neon; this file and the SQL were tested only on local disposable databases (§7).

| | |
|---|---|
| Who | **Maaz only** (owner of the Neon project). Hark never receives the connection string. |
| Where | Neon Console → project → **SQL Editor** → branch **production** (the one FastAPI Cloud uses) → database `neondb` (or the app's database) |
| What | [`sql/A1_readonly_baseline_and_preflight.sql`](sql/A1_readonly_baseline_and_preflight.sql) (main) — or, only if §4 says so, [`sql/A1_readonly_single_statement.sql`](sql/A1_readonly_single_statement.sql) |
| Duration | seconds; the query is cancelled after 15 s; it never waits more than 2 s for a lock |
| Changes | none (READ ONLY transaction, single SELECT, ROLLBACK; proven locally: §7) |

## 1. Before you start (2 minutes)

1. **Time Travel OFF** in the SQL Editor (the toggle queries a past point in time; A1 must read *now*).
2. Do **not** press *Explain* / *Explain (Analyze)*. Use **Run** only.
3. **Branch** selector = production; **database** = the app database. If unsure which branch FastAPI
   Cloud uses, compare the host in FastAPI Cloud's `DATABASE_URL` (look, don't copy it anywhere) with
   Neon → Branches → production → Connection details.
4. **Role.** The editor connects as the role you pick. Prefer a read-only role if one exists:
   - Neon → Branches → production → Roles: is there a role other than the owner (e.g. `readonly`,
     `analytics`)? If yes, pick it in the editor's role selector (if shown).
   - Or simply run A1 with the owner role: row **E 903** then reads `true` ("role could write"), which
     is expected and harmless — the transaction itself is READ ONLY (row **E 900** = `on`).
   - Creating a read-only role is a *change* (not part of A1); do not create one for this.
   - A read replica compute, if the project has one, is ideal: select it; row **E 905** then reads `true`.
5. Nothing else needs to stop. A1 takes no write locks and never blocks the app; at worst it is
   cancelled itself (15 s / 2 s timeouts).

## 2. Steps

1. Open the file on GitHub (PR #25, branch `docs/rc-readiness`): `docs/release/sql/A1_readonly_baseline_and_preflight.sql` → **Raw** → select all → copy.
2. Neon SQL Editor → **New Query** → paste the **whole** file (comments included) → **Run**.
3. The editor shows one result tab per statement (Neon docs: "Running multiple query statements at once
   returns a separate result set for each statement… in separate tabs, numbered in order of execution",
   [Neon SQL Editor](https://neon.com/docs/get-started/query-with-neon-sql-editor), read 31 Oct 2026).
   Tabs 1–5 are `BEGIN`/`SET`, **tab 6 is the result grid** (66 rows: `ord | section | check_name | value`),
   tab 7 is `ROLLBACK`.
4. In tab 6 check row **900** first:
   - `transaction_read_only = on` and **901** `repeatable read` → the editor kept the transaction. Good.
   - `off` → the editor ran statements in separate sessions. The result is **still read-only and still
     one consistent snapshot** (it is a single SELECT), but the 15 s/2 s timeouts did not apply. For the
     record, re-run with `sql/A1_readonly_single_statement.sql` (same SELECT, no BEGIN/SET) and send that
     grid. (Neon's docs do not say whether editor statements share one session, so A1 is built to be
     correct either way: §4.)
5. Export: the editor's copy/download buttons (bottom right) → **CSV** or **copy as JSON** of tab 6.

## 3. What to send back to Hark

- The **tab 6 grid** as CSV/JSON (or pasted text). It contains only counts, booleans, revision ids,
  object names, server settings and one timestamp.
- **No screenshots** that show the connection string, host, role password, or the Neon URL bar with
  project ids. If you send a screenshot, crop to the grid.
- If anything errors: the **error text only** (it never contains row data), plus the tab number.
- Optional extra (only if Hark asks after reading the grid): the commented block at the end of the main
  file returns **integer user ids** of blocking email groups (approved earlier: ids, never emails).

## 4. Neon SQL Editor behaviour and the variants

| Situation | What happens with the main file | Use |
|---|---|---|
| Editor sends the batch on one connection (expected; same as psql) | BEGIN READ ONLY + SET LOCAL apply; row 900 `on`, 901 `repeatable read`, 902 `15s / 2s / 1min` | main file |
| Editor runs each statement in its own session/transaction | `SET LOCAL` prints *WARNING: SET LOCAL can only be used in transaction blocks*; `ROLLBACK` prints *WARNING: there is no transaction in progress*; the SELECT still runs as one read-only statement; row 900 `off` | send the grid anyway; optionally re-run the single-statement file |
| Editor rejects multiple statements | error before anything runs | single-statement file |
| psql available (preferred by engineers) | `PGOPTIONS='-c default_transaction_read_only=on' psql "$READONLY_URL" -X -v ON_ERROR_STOP=1 -A -F ' \| ' -f docs/release/sql/A1_readonly_baseline_and_preflight.sql` | main file |

The single-statement file is byte-identical to the SELECT in the main file (`tools/a1_selftest.py`
checks it). Query history in the editor keeps only the first 9 KB of a query; A1 is ~17 KB, so the
history copy is truncated — it still runs in full (Neon docs, same page).

## 5. Expected output shape and interpretation

### 5.1 Revision rows

| Row | `d924a6e38f10` (presumed production) | head `d5e1a0b00002` (after A8) |
|---|---|---|
| 100 `alembic_version.version_num` | `d924a6e38f10` | `d5e1a0b00002` |
| 101 `fingerprint_inferred_revision` | `d924a6e38f10` | `d5e1a0b00002` |
| 102 `fingerprint_contiguous` | `true` | `true` |
| 103 `version_matches_fingerprint` | `true` | `true` |
| 111–121 markers 01…11 | 01–04 `true`, 05–11 `false` | all `true` |
| 130 bookingstatus labels | `accepted,cancelled,pending,rejected` | `+expired` |
| 134 users columns | `cnic,created_at,email,id,name,password,phone_number,role,updated_at` | `+account_status,token_version` |
| 135 bookings columns | `created_at,id,passenger_id,ride_id,status` | `+expires_at,seats_requested` |
| 136 INVALID indexes | empty | empty |
| 137 concurrent indexes | all `absent` | all `true` |
| 138 email indexes | `ix_users_email=true,uq_users_email_lower=absent` | both `true` |
| 141 tables | `alembic_version,bookings,rides,users` | `+admin_audit_log,idempotency_keys` |

Markers (one per migration): 01 tables exist · 02 `uq_bookings_active_passenger_ride` · 03
`ix_rides_status_departure_time_id` · 04 `ix_*_id` dropped · 05 three FK listing indexes · 06
`uq_users_email_lower` · 07 `idempotency_keys` · 08 `bookings.expires_at` · 09 `seats_requested` + its
CHECK · 10 `users.token_version` + `account_status` · 11 `admin_audit_log`.

### 5.2 Go / no-go

| Observation | Meaning | Decision |
|---|---|---|
| 100 = 101 = `d924a6e38f10`, 102/103 `true`, 136 empty | baseline confirmed | **GO** to staging rehearsal (A2–A4) on this baseline |
| 100 = 101 = another known revision (e.g. `7c2e9a4b6d10`, `e3a7c41b9f52`) | production is behind/ahead of the presumption | **HOLD**: Hark re-plans the sequence from that revision (MIGRATION_READINESS §6); still no prod change |
| 103 `false` or 102 `false` | `alembic_version` and the real schema disagree (manual DDL, partial upgrade, stamped without migrating) | **NO-GO**: forensic review; FORWARD_RECOVERY_PLAN §3 detection on a branch |
| 100 = `(table alembic_version missing)` | schema created without Alembic (e.g. `create_all`) | **NO-GO**: needs a stamp decision after fingerprint review |
| 136 not empty | an INVALID index exists in production now | **NO-GO** until fixed (FORWARD_RECOVERY_PLAN §5 F1, approval-gated) |
| 300 `BLOCKING lower(email) duplicate groups` > 0 | `f6b2d8e0c413` will abort (changes nothing) | **GO for staging, HOLD for production** until a per-group decision is written (PREFLIGHT_REVIEW §4); ids via the optional block |
| 302/310/313 > 0, 300 = 0 | padded / NBSP / invisible-char twins: do not block; padded accounts cannot log in | GO; list for support follow-up (non-blocking) |
| 221 pending on departed rides, 211 stale active rides | first run size of the expiry job (A10) | GO; informs job rollout |
| 223 > 0 at head | multi-seat bookings exist; downgrade of `c1a7e0b00003` refused | info (forward-fix only) |
| 900 `off` | editor did not keep the transaction | still valid (single SELECT); optional re-run with the single-statement file |
| 903 `true` | role has write privileges | expected with the owner role; transaction was read-only (900) |
| 909 not empty | a transaction id was assigned = something wrote | **STOP** and tell Hark (cannot happen with this script; would mean a different script was run) |
| any error | — | send error text; nothing was changed (READ ONLY + ROLLBACK) |

## 6. Abort conditions (stop, change nothing, tell Hark)

- The editor shows a different branch than production, or Time Travel is on.
- Any statement other than `BEGIN`, `SET`, `SELECT`, `ROLLBACK` appears in what you pasted (copy error).
- The run takes longer than ~20 s or an error mentions `lock timeout` / `statement timeout` (production is
  busy or a migration is running — retry later, never raise the timeouts).
- Row 909 is not empty, or row 900 is `off` **and** the editor shows an `INSERT/UPDATE/DELETE` count.

## 7. Local evidence (Hark, 31 Oct 2026; disposable PostgreSQL 17.11, synthetic data only)

`python3 docs/release/tools/a1_selftest.py rs_a1_d924:<seed.json> rs_a1_head:<seed.json>` → **29/29 checks passed**:

- DBs: `rs_a1_d924` (alembic `upgrade d924a6e38f10`, 412 synthetic users incl. 2 case-twin groups,
  NBSP/space/ideographic padded twins, a ZWSP near-twin; 320 rides; 801 bookings) and `rs_a1_head`
  (`upgrade head`, 407 users, 12 suspended + 12 audit rows, multi-seat, expired, 320 rides, 806 bookings).
  Seeds: `tools/seed_synthetic.py --email-anomalies`.
- Revision rows equal `alembic_version` and the fingerprint on both; markers as in §5.1.
- Email counts equal the seeded truth: d924 blocking 2 groups/4 rows, trim+lower 4/9, near 5/11, padded 4,
  NBSP 1, invisible 1; head blocking 0, trim+lower 2/4, near 3/6, padded 3.
- Output (main and single-statement) grep'd for `@`, `example.com`, `Synthetic`, `+92`, any 13-digit run,
  bcrypt prefix: **0 hits**.
- Single-statement grid = main grid (except session rows 900–902, 910).
- Read-only proof: INSERT, UPDATE, DELETE and CREATE TABLE appended after the A1 header are each rejected
  (`cannot execute … in a read-only transaction`); `txid_current_if_assigned()` empty;
  `pg_stat_database` tup_inserted/updated/deleted unchanged; full `pg_dump` byte-identical before/after.
