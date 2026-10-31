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

## 1. Idempotency keys — `c1a7e0b00001` (implemented)

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

## 2. Pending-request expiry and stale ride closure — `c1a7e0b00002`

Implemented on the stacked branch `feat/pending-expiry`; that branch's copy of
this document carries the full section.

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
