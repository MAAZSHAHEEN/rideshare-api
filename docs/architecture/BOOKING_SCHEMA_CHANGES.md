# Booking schema changes (Milestone C)

Status: development branches only. Nothing here has run against production,
staging or any Neon branch; every production step below is behind Maaz's
approval gate (merge, migration, deploy, env vars).

Companion to `docs/architecture/BOOKING_LIFECYCLE.md` (§6 designs). Alembic
chain after AST-005:

```
d924a6e38f10 (production, assumed)
  → e3a7c41b9f52  AST-003 FK listing indexes (CONCURRENTLY)
  → f6b2d8e0c413  AST-005 unique lower(email)
  → c1a7e0b00001  booking idempotency keys          (feat/booking-idempotency)
  → c1a7e0b00002  pending expiry: bookings.expires_at, 'expired' status  (feat/pending-expiry)
  → c1a7e0b00003  multi-seat (design only, §3)
  → d5e1a0b00001… Milestone D (other branches; lead re-chains)
```

`alembic/env.py` runs with `transaction_per_migration=True`: each revision and
its version stamp commit together, so a multi-revision upgrade that fails
part-way keeps every earlier revision and is retried by re-running
`alembic upgrade head`.

---

## 1. Idempotency keys — `c1a7e0b00001` (implemented, PR #18)

### Problem
A client that retries `POST /bookings/{ride_id}` after a timeout gets
`409 You already booked this ride` instead of its original `201`; a retried
accept gets `409 Booking already responded to` and cannot tell it won (gap G6).

### API
- Optional request header `Idempotency-Key`, 1–64 visible ASCII characters
  (`[\x21-\x7E]`). Invalid → `422 {"detail": "Idempotency-Key must be 1 to 64 visible ASCII characters"}`.
- Supported on `POST /bookings/{ride_id}` and `PATCH /bookings/{booking_id}/respond`.
  Without the header behaviour is byte-for-byte unchanged.
- Same user + same key + same request → the **original** status and JSON body,
  plus response header `Idempotent-Replayed: true`. The replay is the stored
  original even if the booking changed since (e.g. accepted later).
- Same user + same key + different request (other ride, other booking,
  accept vs reject, or a different endpoint) → `422 {"detail": "Idempotency-Key was already used for a different request"}`.
- Keys are scoped per user: two users may use the same key independently.
- Only 2xx outcomes are stored. A 4xx/5xx leaves no row, so the same key can
  be retried once the condition changes (a seat frees up).
- CORS: `Idempotency-Key` added to allowed request headers and
  `Idempotent-Replayed` to exposed headers (only relevant for cross-origin
  frontends; the bundled SPA is same-origin).

### Schema
```
idempotency_keys(
  user_id       integer  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key           varchar(64) NOT NULL CHECK (char_length(key) >= 1),
  operation     varchar(32) NOT NULL,        -- booking.create | booking.respond
  request_hash  varchar(64) NOT NULL,        -- sha256 of operation + parsed inputs
  status_code   smallint NULL,               -- NULL only inside the claiming transaction
  response_body jsonb NULL,
  created_at    timestamptz NOT NULL,
  expires_at    timestamptz NOT NULL,
  CONSTRAINT pk_idempotency_keys PRIMARY KEY (user_id, key))
ix_idempotency_keys_expires_at (expires_at)   -- purge job
```
The request fingerprint hashes the **parsed** inputs (`accept=1` and
`accept=true` are the same request), never the raw query string.

### Concurrency
In the request's own transaction, **before** the ride lock (lock order:
idempotency key → Ride → Booking):

```
INSERT INTO idempotency_keys (...) VALUES (...)
ON CONFLICT ON CONSTRAINT pk_idempotency_keys
DO UPDATE SET ... WHERE idempotency_keys.expires_at <= :now
RETURNING user_id
```
- Row returned → this request owns the key; the booking service runs
  unchanged and its `before_commit` hook stores status + body in the **same**
  transaction just before `commit()`.
- Nothing returned → a live key exists (the statement waited on the primary
  key if another transaction was inserting it). Read it: different hash → 422;
  same hash → replay.
- If the owner rolls back (any error), its key row vanishes with it and a
  waiter proceeds as a fresh request. No "in progress" row is ever committed,
  so there is no stuck key and no cleanup on failure.
- Two requests with the same key only ever wait on the key; neither holds a
  ride lock first, so no deadlock cycle is possible. Different keys never
  interact through this table.
- Verified on PostgreSQL 17 with `asyncio.gather`: 12 identical creates →
  exactly 1 booking, 12×201 (1 original + 11 replays); 8 identical accepts →
  1 seat debited; 4 same-key creates for different rides → 1×201 + 3×422.

### TTL and cleanup
- `expires_at = created_at + IDEMPOTENCY_KEY_TTL_HOURS` (default 24, allowed
  1–720; invalid values stop the app at import). No env change is needed to
  ship: the default applies.
- An expired key is reclaimed in place by the next request that uses it.
- `python -m jobs.purge_idempotency_keys [--batch-size N]` deletes expired
  rows in batches (`FOR UPDATE SKIP LOCKED`, one commit per batch), prints
  `purged_idempotency_keys=<n>`. Idempotent; safe during traffic. Not
  scheduled anywhere (§4). Without it the table grows by one small row per
  keyed write; correctness does not depend on it.

### Code touched
`idempotency.py` (new), `models.py` (`IdempotencyKey`), `routers/bookings.py`
(header dependency, claim/replay), `services/bookings.py` (optional
`before_commit` hook in `book_ride` and `respond_to_booking`, 16 lines, no
behaviour change when absent), `main.py` (CORS header lists), `jobs/`.

### Rollout
1. Merge after #5 (#3) and #12/#2 (approval gate).
2. Staging (Neon branch, approval gate): `alembic upgrade head` from
   `f6b2d8e0c413` → `c1a7e0b00001`. Creates one empty table; the only lock on
   an existing table is the brief `SHARE ROW EXCLUSIVE` on `users` for the FK
   (no scan, the new table is empty). Expected run time: milliseconds.
3. Deploy code. Old clients send no header and are unaffected.
4. Frontend may then send `Idempotency-Key: <uuid>` per user action (one
   key per click, reused only for retries of that click).

Order: migration **before** code. Code with the header on a DB without the
table would return 500 for keyed requests only (unkeyed requests never touch
the table).

### Rollback
- Code: revert; the header is then ignored. Safe at any time.
- Database: `alembic downgrade f6b2d8e0c413` drops the table and its index.
  Do this only after the code revert (keyed requests would 500 otherwise).
  Data loss: stored replays only; no booking, ride or user row is touched.

---

## 2. Pending-request expiry and stale ride closure — `c1a7e0b00002` (implemented)

### Problem
Pending requests never expired (gap G4): passengers waited until departure.
Departed rides stayed `active` forever (gap G3).

### Rules
- New request: `expires_at = min(now + BOOKING_PENDING_TTL_MINUTES, departure_time)`
  (default 360 min = 6 h; allowed 5–10 080; validated at import).
- Legacy rows: `expires_at IS NULL` = "expires at departure" — exactly the rule
  that already applied (accept after departure was 409). No row is backfilled,
  so no existing request changes meaning on deploy.
- Effective expiry = `min(coalesce(expires_at, departure_time), departure_time)`;
  boundary inclusive (expired **at** the instant), like `clock.has_departed`.
  All comparisons use `clock.utc_now()` (application clock, UTC instants).
- New terminal booking status `expired` (`pending → expired`, no seat effect,
  no exits). Lifecycle table: `(pending, "expire") → expired` in `lifecycle.py`.

### Enforcement (lazy — never depends on the sweep)
| Path | Behaviour |
|---|---|
| `PATCH /bookings/{id}/respond?accept=true` | after the ride lock: booking state → ride active → departed (`Ride has already departed`, unchanged) → **expired → 409 `Booking request has expired`** → seats. No write. |
| `…?accept=false` (reject) | still allowed on an expired-but-unswept request (driver clears it) |
| `PATCH /bookings/{id}/cancel` (pending) | still allowed (passenger withdraws) |
| any action on a persisted `expired` booking | 409 `Booking already responded to` / `Booking cannot be cancelled` (terminal) |
| `POST /bookings/{ride_id}` | under the ride lock, the passenger's own due request on that ride becomes `expired` in the same transaction, then the new request is created — so an expired request no longer blocks re-requesting. A live one still gets 409 `You already booked this ride`. |
| `GET /bookings/me`, `GET /rides/{id}/bookings` | a due pending request is **shown** as `expired` (read-only; GET never writes). `?status=expired` includes them; `?status=pending` excludes them. |

`expires_at` is **not** yet in API responses (keeps the listing contract and
tests of other open branches stable); adding it is additive.

### Sweep — `python -m jobs.expire_bookings [--batch-size N]`
1. Stale rides: `status = 'active' AND departure_time <= now − RIDE_AUTO_CLOSE_AFTER_HOURS`
   (default 24 h, allowed 1–720). Per ride, one transaction: lock ride
   `FOR UPDATE SKIP LOCKED`, re-check status and departure, lock its pending
   and accepted bookings `ORDER BY id`, pending → `expired`; ride →
   `completed` if it has ≥ 1 accepted booking, else `cancelled`. Accepted
   bookings and `available_seats` are untouched (same as a manual completion;
   a cancel with no accepted booking restores nothing). Completion only ever
   happens after departure (product rule, 31 Oct).
2. Due requests: pending with `expires_at <= now`, or legacy NULL on a
   departed ride. Per ride: lock ride `SKIP LOCKED`, lock due bookings by id,
   → `expired`.
- Lock order Ride → Booking like every writer; SKIP LOCKED means the sweep
  never waits behind, or blocks, a user request; skipped rides are counted
  (`rides_skipped_locked`) and handled by the next run.
- Idempotent: a re-run prints all zeros. Output: `expired_requests=`,
  `rides_completed=`, `rides_cancelled=`, `rides_skipped_locked=`.
- Status names: no new **ride** status. `completed`/`cancelled` already exist
  in `ridestatus`; the only DB CHECK constraints (`7c2e9a4b6d10`) are
  `available_seats >= 0` and `fare_per_seat >= 0`, which the sweep never
  changes. Without `closed_by`/`closed_at` columns the UI cannot tell an
  automatic closure from a driver's; those columns are a later additive
  revision if wanted.

### Schema (`c1a7e0b00002`, down `c1a7e0b00001`)
- `ALTER TABLE bookings ADD COLUMN IF NOT EXISTS expires_at timestamptz NULL`
  — metadata-only; then a type check that refuses a stray column of another
  shape (`UnexpectedExpiresAtColumn`).
- `ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'expired'` (autocommit).
- Both under `lock_timeout = 5s` so the brief ACCESS EXCLUSIVE lock can't queue
  behind a long transaction and stall traffic; on timeout just re-run.
- `CREATE INDEX CONCURRENTLY ix_bookings_pending_expires_at ON bookings
  (expires_at) WHERE status = 'pending'` — re-runnable (valid index kept,
  INVALID one dropped concurrently and rebuilt), same pattern as `e3a7`.

### Verified (disposable PostgreSQL 17.11)
- Upgrade `c1a7e0b00001 → c1a7e0b00002`, downgrade, re-upgrade with legacy
  bookings in every status: legacy columns byte-identical, `expires_at` all
  NULL, index valid with predicate `status = 'pending'`.
- `d924a6e38f10 → head → downgrade f6b2d8e0c413 → head`: identical schema.
- Multi-revision upgrade failing in `c1a7e0b00002` (stray `expires_at text`):
  stops at `c1a7e0b00001` with the idempotency table committed; retry after
  dropping the stray column reaches head.
- Interrupted concurrent build (INVALID index, column and label already
  present) repaired by re-running `alembic upgrade head`.
- Load check (`/workspace/work/rs_expiry_load_check.py`): see the PR body for
  the measured write latencies during upgrade and downgrade.

### Rollout
1. Merge after `c1a7e0b00001` (approval gate).
2. **Frontend first or together:** passenger/driver UIs must render an
   unknown/`expired` status (the open UI branches type `BookingStatus` as four
   values and look labels up in a fixed map) and map the new 409 detail
   `Booking request has expired`.
3. Staging Neon branch (approval): `alembic upgrade head`. Check
   `indisvalid` for `ix_bookings_pending_expires_at`; no long transactions on
   `bookings` while it runs (CONCURRENTLY waits for them).
4. Deploy code. Effects are immediate and lazy; no job is needed.
5. Optionally (approval): schedule the sweep (§4), first with
   `RIDE_AUTO_CLOSE_AFTER_HOURS` at its default.

Order: migration **before** code (code selects `expires_at`; without the
column every booking endpoint 500s).

### Rollback
Old code cannot read a row whose status is `expired` (SQLAlchemy enum lookup
fails → 500 on any listing containing it), so:
1. Stop the sweep if it was scheduled.
2. `UPDATE bookings SET status = 'cancelled' WHERE status = 'expired';`
   (idempotent; needs approval on production).
3. Revert the code. Old code ignores `expires_at` (not in its model), so the
   column may stay.
4. Re-run step 2 (catches requests a re-request expired in between), then
   `alembic downgrade c1a7e0b00001`: maps any remaining `expired` →
   `cancelled`, drops the index (concurrently) and the column.
- The `expired` enum label stays in the type (PostgreSQL cannot drop it);
  harmless, and re-upgrade is a no-op for it.
- Rides closed by the sweep stay `completed`/`cancelled` — valid states for
  old code; reopening them is not offered (no approval-free undo for that).

---

## 3. Multi-seat bookings — `c1a7e0b00003` (design only)

Not implemented in Milestone C. Design follows BOOKING_LIFECYCLE.md §6.4 with
the chain position fixed:

- Revision `c1a7e0b00003` (down `c1a7e0b00002`):
  - `ALTER TABLE bookings ADD COLUMN seats smallint NOT NULL DEFAULT 1`
    (PG ≥ 11 fast default, no rewrite).
  - `ALTER TABLE rides ADD COLUMN total_seats integer NULL`.
  - `CHECK (seats BETWEEN 1 AND 8) NOT VALID`, then `VALIDATE CONSTRAINT`
    (SHARE UPDATE EXCLUSIVE; writes continue).
- Backfill (separate, batched by 1 000 rides, re-runnable, outside the DDL
  transaction): `total_seats = available_seats + count(accepted)` for
  `active`/`completed` rides and `= available_seats` for `cancelled` rides
  (their debits were reversed). Pre-flight query reports rides where the
  result would be < `available_seats` (should be none).
- `CHECK (available_seats <= total_seats) NOT VALID` + `VALIDATE` only after
  the backfill; `SET NOT NULL` on `total_seats` one release later, after code
  always writes it.
- Code: `POST /bookings/{ride_id}` takes an optional body `{"seats": n}`
  (absent = 1); request requires `available_seats >= seats`; `SEAT_EFFECT`
  becomes `±booking.seats`; responses gain `seats`. The idempotency
  fingerprint must then include `seats` (operation parameters already hash
  the parsed inputs; add the field). The partial unique index stays: one live
  booking per passenger per ride. Lock order unchanged.
- Rollback: code revert is safe while every booking has `seats = 1`.
  Downgrade pre-checks `SELECT count(*) FROM bookings WHERE seats <> 1` and
  aborts with the count if non-zero; otherwise drops constraints and columns.

---

## 4. Running the jobs (no scheduler deployed)

FastAPI Cloud background-worker / cron support is **not verified**. Options,
in order of preference, each needing Maaz's approval before any deploy:

1. External scheduler calling a one-shot command against the production DB:
   GitHub Actions `schedule:` workflow (every 5–15 min) running
   `python -m jobs.<name>` with `DATABASE_URL` from an Actions secret.
   Simple, but puts a production DB credential in GitHub.
2. Neon/pg_cron (if available on the plan) calling equivalent SQL. Duplicates
   logic in SQL; not recommended.
3. A FastAPI Cloud worker/cron, if the platform supports it — verify first.
4. An authenticated admin-only `POST /admin/jobs/...` endpoint triggered by an
   external pinger. Adds attack surface; last resort.

Correctness never depends on the jobs running: expiry and replay rules are
enforced on the request path.
