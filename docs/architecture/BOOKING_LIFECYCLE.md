# Ride and booking lifecycle (Milestone 4)

Status: analysis of `fix/booking-departure-rules` (PR #2, `b656e19`) plus the
code-only changes on `feat/booking-lifecycle-guards`. Every "current behaviour"
statement below was checked against `services/bookings.py`, `services/rides.py`,
`models.py` and the PostgreSQL tests named in brackets. Schema-changing work in
§6 is **design only**: no migration is part of this branch.

Legend: **[PR #2]** behaviour on the base branch; **[this branch]** behaviour
added here; **(gap)** a known hole, see §5.

---

## 1. Data model (as is)

| Table | Lifecycle columns | Notes |
|---|---|---|
| `rides` | `status` (PG enum `ridestatus`: `active`, `completed`, `cancelled`), `departure_time timestamptz NOT NULL`, `available_seats int CHECK >= 0` | No `total_seats`, no `updated_at`, no closure timestamp. |
| `bookings` | `status` (PG enum `bookingstatus`: `pending`, `accepted`, `rejected`, `cancelled`) | Exactly one seat per booking. Partial unique index `uq_bookings_active_passenger_ride (ride_id, passenger_id) WHERE status IN ('pending','accepted')`. No `expires_at`, `cancelled_at`, `cancelled_by`. |

"Departed" is **not** a stored state. It is derived: `clock.has_departed(t)` is
`t <= clock.utc_now()` (inclusive boundary), compared as UTC instants.

Seat accounting is a mutable counter: a seat is debited only when a request is
**accepted** and restored only when that known debit is reversed (cancellation
of an accepted booking). Creating a request does not debit a seat.

Invariant that holds for every ride today (there is no edit endpoint):
`available_seats + count(accepted bookings) == seats at creation` while the ride
is `active` or `completed`; on a `cancelled` ride every accepted booking was
reversed, so `available_seats == seats at creation`.
[`test_mixed_concurrent_operations_conserve_seats`,
`test_concurrent_requests_and_acceptances_never_oversell`]

## 2. State-transition tables

Since **[this branch]** the allowed moves, their 409 messages and seat effects
live in one module, `lifecycle.py` (`RIDE_TRANSITIONS`, `BOOKING_TRANSITIONS`,
`SEAT_EFFECT`). The services call it after taking their row locks. Detail
strings and guard order are unchanged from PR #2 (API contract).

### 2.1 Ride

| From | Action / endpoint | Who | Guards, in order | To | Seat effect | Success | Failures |
|---|---|---|---|---|---|---|---|
| — | create `POST /rides/` | any ordinary account (driver capability) | role → `departure_time` aware (422 from schema) → departure strictly after now | `active` | `available_seats` = requested | 201 | 403 role; 422 naive/past/now |
| `active` | complete `PATCH /rides/{id}/complete` | owning driver | role → lock ride → exists → owner → transition | `completed` | none; pending bookings → `cancelled` (0 seats each) | 200 | 403, 404, 409 `Ride is not active` |
| `active` | cancel `PATCH /rides/{id}/cancel` | owning driver | role → lock ride → exists → owner → transition | `cancelled` | +1 per accepted booking; pending and accepted → `cancelled` | 200 | 403, 404, 409 `Ride is not active` |
| `completed` / `cancelled` | any | — | — | — | — | — | 409 `Ride is not active` (terminal; no reopen) |

Notes:
- Completion has **no departure check** (gap G1, audit B4): a driver can mark a
  ride completed the day before it leaves.
- Cancellation has **no departure cut-off**, by design: a driver who does not
  turn up can still release passengers. [`test_driver_may_still_cancel_a_departed_ride`]
- Nothing closes a ride automatically (gap G3). A departed ride stays `active`
  forever unless the driver acts. Search hides it (`departure_time > now`), but
  `GET /rides/me` and `GET /bookings/me` keep showing it as `active`.

### 2.2 Booking

| From | Action / endpoint | Who | Guards, in order | To | Seat effect | Success | Failures |
|---|---|---|---|---|---|---|---|
| — | request `POST /bookings/{ride_id}` | any ordinary account (passenger capability), not the ride's driver | role → lock ride → exists → not own ride (403) → ride `active` → not departed → `available_seats >= 1` → no live booking for this passenger | `pending` | none | 201 | 403; 404; 409 `Ride is not active` / `Ride has already departed` / `No seats available` / `You already booked this ride` (pre-check or unique-index race) |
| `pending` | accept `PATCH /bookings/{id}/respond?accept=true` | owning driver | role → booking exists (404) → lock ride → owner → lock booking → booking `pending` → ride `active` → not departed → seat available | `accepted` | −1 | 200 | 409 `Booking already responded to` / `Ride is not active` / `Ride has already departed` / `No seats available` |
| `pending` | reject `…/respond?accept=false` | owning driver | as accept, without departure and seat checks | `rejected` | none | 200 | 409 `Booking already responded to` / `Ride is not active` |
| `pending` | cancel `PATCH /bookings/{id}/cancel` | owning passenger | role → booking exists → lock ride → lock booking → owner → ride `active` → booking cancellable | `cancelled` | none | 200 | 403; 404; 409 `Ride is not active` / `Booking cannot be cancelled` |
| `accepted` | cancel (same endpoint) | owning passenger | as above, **then cut-off: not departed [this branch]** | `cancelled` | +1 | 200 | as above + 409 `Ride has already departed` [this branch] |
| `pending` | ride completed | system cascade | inside complete_ride | `cancelled` | none | — | — |
| `pending`/`accepted` | ride cancelled | system cascade | inside cancel_ride | `cancelled` | +1 if accepted | — | — |
| `rejected` / `cancelled` | any | — | — | — | — | — | 409 (terminal; a new request is allowed because the unique index covers only live statuses) |

Guard-order asymmetry (kept for contract stability, pinned by
`test_booking_matrix_every_ride_state_booking_state_and_action`): the driver's
respond checks **booking** state before **ride** state, the passenger's cancel
checks **ride** before **booking**. So a cancelled booking on a completed ride
answers `Booking already responded to` to the driver and `Ride is not active` to
the passenger.

Departure-related rules after this branch:

| Action at/after departure | Result |
|---|---|
| request a seat | 409 `Ride has already departed` [PR #2] |
| accept | 409 `Ride has already departed` [PR #2] |
| reject | allowed (driver clears stale requests) [PR #2] |
| passenger withdraws a pending request | allowed |
| passenger cancels an accepted booking | **409 `Ride has already departed`, seat not restored** [this branch] |
| driver cancels the ride | allowed |
| driver completes the ride | allowed (also allowed *before* departure — gap G1) |

## 3. Locking

- Isolation: PostgreSQL READ COMMITTED (default). Each statement sees data
  committed before it started, so a `SELECT … FOR UPDATE` issued after a lock
  wait sees the winner's committed state.
- **Lock order is always Ride → Booking(s)**. Every writer that changes a ride's
  bookings first takes `SELECT … FROM rides WHERE id = :ride FOR UPDATE`. Booking
  rows are then locked individually (respond, cancel) or as a set ordered by
  `bookings.id ASC` (ride complete/cancel). Booking creation locks only the ride
  and then inserts.
- Before the ride lock, services read only the immutable routing key
  (`bookings.ride_id`), never lifecycle state.
- All locked reads use `execution_options(populate_existing=True)` so ORM objects
  already in the session are overwritten with post-wait database state.
- Each operation commits once; any exception rolls back (releasing locks) before
  the session is closed. [`test_direct_conflicts_release_locks_before_session_closes`,
  `test_direct_failed_commit_rolls_back_all_writes`]
- Time-based guards run **after** the locks, so a request that queued behind
  another transaction is judged at the moment it proceeds.
  [`test_booking_waiting_on_lock_rechecks_departure`,
  `test_acceptance_waiting_on_lock_rechecks_departure`,
  `test_cancellation_waiting_on_ride_lock_rechecks_departure`]

Because every writer for one ride serialises on that ride's row, there is no
deadlock cycle between booking writers; different rides never block each other.

## 4. Races (verified)

All of the following are covered by tests that hold the ride lock in a
coordinator connection until every request is provably blocked in PostgreSQL
(`pg_blocking_pids`), then release it, exercising both serial orders
(`tests/test_booking_responses.py`, `contended_operations`), or by
`asyncio.gather` against real PostgreSQL (`tests/test_lifecycle.py`).

| Race | Behaviour | Tests |
|---|---|---|
| Two accepts of the same booking | one 200, one 409 `Booking already responded to`; one seat debited | `test_same_booking_concurrent_accepts_consume_one_seat` |
| Accept vs reject of the same booking | first wins; loser 409 | `test_same_booking_concurrent_accept_reject_preserves_winner` |
| Concurrent accepts of different bookings for the last seat(s) | exactly `available_seats` succeed, rest 409 `No seats available`; never negative | `test_different_bookings_compete_for_final_seat`, `test_concurrent_requests_and_acceptances_never_oversell` (8 requests, 3 seats) |
| Cancel (passenger) vs accept | accept first → both 200, seat debited then restored (net 0); cancel first → accept 409 | `test_accept_then_cancel_race_both_succeed_with_no_net_seat_change`, `test_cancel_then_accept_race_acceptance_conflicts` |
| Two cancels of one accepted booking | one restore only | `test_two_concurrent_cancellations_restore_only_one_seat` |
| Departure vs accept / request / accepted-cancel | guard evaluated after the lock with the then-current clock; a request that waited past departure gets 409 | the three `…waiting_on…lock_rechecks_departure` tests; exact-instant tests in `test_departure_rules.py` and `test_cancellation_cutoff_exact_departure_instant` |
| Ride cancel vs booking request | request first → its booking is cancelled by the cascade; ride cancel first → request 409 | `test_booking_creation_then_ride_cancellation_cancels_new_booking`, `test_ride_cancellation_then_booking_creation_refreshes_stale_ride` |
| Ride cancel vs accept / reject / passenger cancel | the debit is reversed exactly once whichever wins | `test_acceptance_then_ride_cancellation_reverses_known_debit`, `test_passenger_cancellation_then_ride_cancellation_does_not_double_restore`, `test_concurrent_cancel_accepted_and_ride_cancel_restore_once`, … |
| Ride complete vs ride cancel | exactly one terminal transition | `test_completion_then_ride_cancellation_conflicts_preserving_accepted_seat`, `test_ride_completion_vs_cancellation_has_one_terminal_winner` |
| Duplicate request by the same passenger | serialised by the ride lock (pre-check 409); the partial unique index is the backstop, mapped to 409 by constraint name only | `test_competing_writer_uniqueness_violation_returns_conflict_and_rolls_back` |
| Mixed storm (cancels, duplicate cancel, accepts, accept-vs-reject, cancel-vs-accept) | every response 200 or 409; `available_seats >= 0`; seat conservation holds | `test_mixed_concurrent_operations_conserve_seats` |

Residual windows (accepted, documented):
- The departure check and the commit are microseconds apart; an accept checked at
  T−1 µs and committed at T+1 ms is valid. Harmless.
- Time is the **application** clock, not `now()` in PostgreSQL. With several app
  instances, NTP skew moves the boundary by the skew. Acceptable for a
  minutes-granularity product; a worker (§6.2) should use the same `clock`.

## 5. Gaps

| ID | Gap | Impact | Fix | Kind |
|---|---|---|---|---|
| G1 | Completion allowed before departure (audit B4) | driver can close a ride early; cancels pending requests | 409 before departure (optional grace). Code-only but changes driver-UI behaviour and ~25 existing tests complete future rides | (a) deferred, needs decision |
| G2 | Accepted booking cancellable after departure; seat restored on a departed ride, no-show hidden | wrong history, misleading seat count | cut-off at departure | (a) **done on this branch** |
| G3 | Departed rides stay `active` forever (B1) | stale "active" in My rides/My bookings | worker closure (§6.2); interim: UI labels "Departed" from `departure_time` | (b) |
| G4 | Pending requests never expire (B7) | passengers wait indefinitely; only blocked at departure (accept 409) | `expires_at` + worker (§6.1) | (b) |
| G5 | One seat per booking (B2); no total capacity (B3) | can't book for 2; UI can't show "2 of 4"; no upper-bound invariant | §6.4 | (b) |
| G6 | No idempotency | a retried POST returns 409 `You already booked this ride` instead of the original 201; a retried accept returns 409 `Booking already responded to` and the client can't tell it won | §6.5 | (b) |
| G7 | `create_ride` / `search_rides` read `datetime.now` directly, unlike booking guards | inconsistent time source; tests could not freeze one clock | use `clock.utc_now()` | (a) **done on this branch** |
| G8 | Transition rules duplicated across five functions | drift risk | `lifecycle.py` tables | (a) **done on this branch** |
| G9 | No audit trail: who cancelled/completed, when (D2) | no dispute evidence, no late-cancellation flag | `booking_events`/`ride_events` (AST-021), cancellation columns (AST-019) | (b) |
| G10 | Booking 404 is checked before ownership, so 403 vs 404 reveals whether a booking ID exists | minor ID enumeration | return 404 for non-owners too (contract change) | (a) not done; low value |
| G11 | `updated_at` never updated | no change timestamps | `onupdate` / trigger | (b) small |

## 6. Target design

### 6.1 Pending-request expiry
Rule: a `pending` request expires at the earlier of `created_at + PENDING_TTL`
(proposal: 6 h, configurable) and the ride's departure (or departure − 1 h if the
product wants a confirmation buffer).

- **Now (no schema, already effective):** a pending request on a departed ride
  can't be accepted (409) and can be rejected or withdrawn. The UI should show it
  as "Expired" when `status == pending and departure_time <= now`. This is a
  client-side label; the API does not rewrite state on read (write-on-read under
  GET would take locks in read paths and break the read-only contract).
- **Schema (b):**
  1. Migration A: `ALTER TABLE bookings ADD COLUMN expires_at timestamptz NULL`
     (metadata-only, instant). New rows get `expires_at` in `book_ride`. Legacy
     rows stay NULL = "expires at departure".
  2. Migration B (separate revision, `autocommit_block`):
     `ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'expired'`. The new value
     can't be used in the same transaction that adds it, and enum values can't
     be dropped, so it ships one release **before** code writes it.
  3. `respond_to_booking` (accept) treats `expires_at <= now` like departure:
     409 `Booking request has expired`, so enforcement never depends on the
     worker running.
  4. Worker (AST-018) every minute: `SELECT id FROM rides WHERE status='active'
     AND EXISTS (pending past expiry) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT
     100`, then lock pending bookings by id, set `expired`. Same Ride → Booking
     order; SKIP LOCKED means user requests are never blocked by the worker.
     Idempotent reruns.
- **Rollback:** revert code first (stops writing `expired`). Downgrade A drops the
  column. For B, map rows back (`UPDATE bookings SET status='cancelled' WHERE
  status='expired'`) and leave the unused enum value (removing it means
  recreating the type with a table rewrite — not worth it). Frontend must
  tolerate an unknown status before B ships (render it as a neutral label).

### 6.2 Stale ride closure (departed → completed)
- Rule: an `active` ride whose `departure_time + RIDE_AUTO_CLOSE_AFTER`
  (proposal: 24 h) has passed is closed by the worker: pending → `expired` (or
  `cancelled` before 6.1 ships), ride → `completed` if it has at least one
  accepted booking, otherwise `cancelled`. Accepted bookings and seat counts are
  untouched, exactly like a manual completion.
- Needs: the worker process (FastAPI Cloud background-process support is **not
  verified**), and ideally `rides.closed_by` (`driver`/`system`) and
  `rides.closed_at` so the UI can say "closed automatically". Both nullable,
  additive; rollback drops the columns. No new ride enum value is needed.
- Code-only interim: G1 (complete only after departure). Recommend shipping it
  with a 30-minute grace before departure once Maaz confirms the driver UI
  behaviour; it requires updating the ~25 existing completion tests to freeze
  `clock.utc_now` after departure.

### 6.3 Cancellation cut-off
- **Implemented (code-only):** accepted bookings can't be cancelled at or after
  departure (409 `Ride has already departed`); pending requests can always be
  withdrawn; driver ride cancellation is unrestricted.
- **Target (b), AST-019:** a configurable window before departure (proposal 6 h)
  inside which an accepted cancellation is still **allowed** but recorded as late:
  `bookings.cancelled_at timestamptz NULL`, `cancelled_by varchar(16) NULL`
  (`passenger`/`driver`/`system`), `cancel_reason varchar(200) NULL`,
  `late_cancellation boolean NOT NULL DEFAULT false` (PG ≥ 11 fast default, no
  rewrite). No money is involved. Rollback: drop the columns after a code revert.
  Blocking inside the window instead of flagging would strand passengers whose
  plans change, so flagging is recommended.

### 6.4 Multi-seat bookings (AST-017)
- Schema, three revisions, each with downgrade:
  1. `ALTER TABLE rides ADD COLUMN total_seats integer NULL`;
     `ALTER TABLE bookings ADD COLUMN seats smallint NOT NULL DEFAULT 1` (fast
     default).
  2. Backfill in batches of 1 000 rides:
     `total_seats = available_seats + (SELECT count(*) FROM bookings b WHERE
     b.ride_id = r.id AND b.status = 'accepted')` for active/completed rides and
     `= available_seats` for cancelled rides (their debits were reversed).
  3. `CHECK (seats BETWEEN 1 AND 8) NOT VALID` and `CHECK (available_seats <=
     total_seats) NOT VALID`, then `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE,
     no write block). Production pre-flight query reports any row that would
     violate before validation. `SET NOT NULL` on `total_seats` only in a later
     release after the code always writes it.
- Code: `POST /bookings/{ride_id}` accepts an optional JSON body `{"seats": n}`
  (absent body = 1, so current clients keep working); request requires
  `available_seats >= seats`; `SEAT_EFFECT` becomes `±booking.seats`; responses
  gain `seats` (additive). The partial unique index stays: one live booking per
  passenger per ride, sized by `seats`. Locks unchanged.
- Rollback: code revert is safe while every booking has `seats = 1`. Downgrade
  pre-checks `SELECT count(*) FROM bookings WHERE seats <> 1` and aborts with a
  report if non-zero; otherwise drops constraints and columns.

### 6.5 Idempotency keys (AST-022, extended to bookings)
- Optional header `Idempotency-Key` (1–64 visible ASCII) on `POST /bookings/{id}`,
  `PATCH /bookings/{id}/respond`, `POST /rides/`. No header = today's behaviour.
- Table `idempotency_keys(user_id int, key varchar(64), method, path,
  request_hash bytea, status_code smallint, response jsonb, created_at,
  expires_at, PRIMARY KEY (user_id, key))`.
- In the **same transaction** as the business write, and **before** the ride
  lock (lock order becomes Idempotency → Ride → Booking, so two requests with the
  same key can't deadlock against each other):
  `INSERT … ON CONFLICT DO NOTHING`. A concurrent duplicate blocks on the unique
  index until the first commits, then sees the row: same hash → replay the stored
  status/body; different hash → 422. If the first rolls back, the key disappears
  with it, so there is never a stuck "in progress" row. The response body is
  built after `flush()` (ids known) and stored before `commit()`.
- Only 2xx responses are stored; 4xx are recomputed (state may legitimately
  change). Worker deletes `expires_at < now()` rows (24 h TTL).
- Rollback: code revert ignores the header; downgrade drops the table.

### 6.6 Timezones
- Storage: `timestamptz`, always written as UTC (`create_ride` converts). API
  input requires an offset (`AwareDatetime`; naive → 422). Responses serialise
  UTC.
- One clock: all lifecycle comparisons use `clock.utc_now()` /
  `clock.has_departed()` with an inclusive boundary — booking guards [PR #2],
  cancellation cut-off, ride creation and search [this branch].
- UI: render in the viewer's zone (Pakistan is UTC+05:00, no DST, but do not
  hard-code it); a "date" filter must send `departure_from=YYYY-MM-DDT00:00:00+05:00`
  and `departure_before=<next day>T00:00:00+05:00`, not UTC midnights, or a
  06:00 PKT ride lands on the previous day.
- Future worker uses the same `clock` (not PostgreSQL `now()`) so tests can freeze
  one source.

## 7. Delivery split

**(a) Code-only, low risk — on `feat/booking-lifecycle-guards`:**
1. `lifecycle.py` state tables; services use them; identical 409 strings and
   guard order. (AST-015)
2. Accepted-booking cancellation cut-off at departure. (B1 part, G2)
3. Ride creation and search use `clock.utc_now()`. (G7)
4. Exhaustive state × action matrix (unit + HTTP on PostgreSQL), departure
   boundary tests, `asyncio.gather` concurrency and seat-conservation tests.

**(a) Code-only, deferred pending a decision:** G1 completion-after-departure
(+ grace), G10 404-for-non-owners.

**(b) Schema-changing — design only, each its own PR with migration test,
pre-flight query and rollback:** 6.1 expiry (`expires_at`, enum `expired`), 6.2
closure columns + worker, 6.3 cancellation record (AST-019), 6.4 multi-seat
(AST-017), 6.5 idempotency table (AST-022), audit events (AST-021). Production
migrations remain behind Maaz's approval gate.
