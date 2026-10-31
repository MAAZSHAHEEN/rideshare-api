# Concurrency and migration evidence (RC-stress)

Branch `test/concurrency-stress`, from `origin/feat/admin-audit-log` (#21 @ `7cedddb`, which
contains #19 → #22 → #20 → #18 → #5+#12, #13, #10). Draft PR #27 (base `rideshare-v2`,
stacked on #21). Everything below ran on **disposable local PostgreSQL 17** databases named
`rs_stress*` (Redis db 8) on 31 Oct 2026. Nothing touched staging, Neon or production.

| Commit | What |
|---|---|
| `48f7580` | `tests/test_concurrency_stress.py` (stress suite) and `tests/tools/m3_db_checks.py` (Milestone 3 checker) |
| `75bec50` | **PRODUCT FIX** — `services/accounts.py`: suspension re-reads the clock after waiting for ride locks, plus its regression test |
| `4b692e7` | checker expectation fixes (test tooling only) |
| `fd52122` | test-bug fix in one probabilistic assertion (§3.2), test only |
| this doc | evidence |

## 1. Result summary

| Area | Result |
|---|---|
| Stress file, 20 iterations × 13 tests, random seed per iteration, `STRESS_ROUNDS=6` | Run 1 (`75bec50`): **19/20** iterations passed. The 1 failure was a test bug, fixed in `fd52122`. Run 2 (`fd52122`): **10/10** passed |
| Product defect found | 1, low severity, fixed in `75bec50` (§4) |
| Harness mutation checks (break the product, the suite must fail) | 9 of 10 killed; 1 survivor explained (§3.4) |
| Milestone 3 DB checks | **49/49** (35 + 14 interrupt cases) (§5) |
| Full backend suite, once | **415 tests, OK** (921 s, at `75bec50`) |
| CI on GitHub (PR #27) | ✅ both checks on `75bec50` (PostgreSQL integration tests, dependency audit); the final head is checked in the PR and in handover RC-stress.md |

## 2. How the stress suite works

`tests/test_concurrency_stress.py` (skipped without `TEST_DATABASE_URL`).

- Every test gets its own schema built from the models, an engine with a 30+20 connection
  pool, and real concurrent sessions: each operation opens its own `AsyncSession`, so its
  own PostgreSQL backend. HTTP paths use the httpx ASGI client against the real app; jobs
  use **separate engines** (separate pools).
- **Test-only tracking objects** in that schema (never in the app schema or migrations):
  - `stress_capacity`: each ride's seats at creation, from an `AFTER INSERT` trigger on
    `rides` (the app has no total-seats column);
  - `stress_history`: one row per committed insert or status change on rides, bookings and
    `users.account_status`, with `txid_current()` and `clock_timestamp()`. A rolled-back
    transaction takes its rows with it, so this is the committed history;
  - `stress_events`: the instant a test moved the frozen app clock past a ride's departure.
- **After every test** `asyncTearDown` runs `assert_invariants()`, which is pure SQL (it
  never looks at HTTP codes). Several tests also call it after every round. Each query
  must return no rows:

| Invariant | SQL check (abridged) |
|---|---|
| Seat bounds | `0 <= available_seats <= stress_capacity.seats` |
| Seat conservation (every status) | `SUM(seats_requested) FILTER (accepted) + available_seats = seats` |
| Cancelled ride | `available_seats = seats` and no pending/accepted booking |
| Completed ride | no pending booking |
| No duplicate live booking | `GROUP BY ride_id, passenger_id HAVING count(*) > 1` over pending/accepted; plus `uq_bookings_active_passenger_ride` exists, unique, valid, with the `pending/accepted` predicate |
| Multi-seat CHECK | no `seats_requested` outside 1..8 |
| Nothing accepted/created after cancel or completion | history: booking `INSERT` or `→accepted` later than the ride's `→cancelled/completed` |
| Nothing accepted/created after departure | history vs `stress_events` |
| No accepted seat handed back after departure | history: `accepted→cancelled` after departure by a passenger cancel or in a suspension's transaction (same `txid`) |
| Suspension never cancels a departed ride | history: ride `→cancelled` after departure in a transaction that also suspended a user |
| Nothing created after a suspension committed | history: `INSERT` of a ride/booking by the user between their `→suspended` and the next reinstate |
| Suspended user holds nothing live | no pending/accepted booking on an active future ride; no active future ride as driver |
| `token_version` | equals the number of `user.suspend` + `user.reinstate` audit rows for that user |
| `account_status` | matches the latest suspend/reinstate audit action |
| Audit rows | `count(*) GROUP BY action` equals the admin actions the test actually completed |
| Audit immutability | both `admin_audit_log_no_*` triggers present |
| Idempotency rows ↔ bookings | no row with NULL `status_code`/`response_body`; each live `booking.create` key points at a booking with the same passenger, ride and seats; no booking claimed by two keys; each `booking.respond` key matches a committed `pending→accepted/rejected` transition by that ride's driver |
| No stuck lock waits | no backend in the database left waiting on a lock |

### 2.1 Forcing interleavings

- **Paused COMMIT.** `hold_commit()` patches one session's `commit`, so the transaction
  stops just before COMMIT while still holding every row lock it took. The competing
  operations start only after `pg_stat_activity` shows N backends with
  `wait_event_type = 'Lock'` in this database (`wait_blocked(N)`). Then the gate opens.
- **Coordinator lock.** A separate connection holds `SELECT … FOR UPDATE` on a ride or
  booking row (`held_lock()`) while the operations under test queue behind it.
- **Frozen clock.** `clock.utc_now` is patched with a mutable value. It is moved
  **while** operations are confirmed waiting on a lock, so the time-based guard runs after
  the clock has crossed.

| # | Scenario (test) | Forced or probabilistic |
|---|---|---|
| 1 | 1/2/3-seat requests, duplicate requests, accepts, duplicate accepts, rejects and passenger cancels on 1–5-seat rides; then cancels racing new requests and accepts (`test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity`, 2×ROUNDS = 12 rounds per run) | **Probabilistic**, seeded (`SEED*1000+round`) |
| 2 | Accept vs passenger cancel vs driver ride cancel. 12 combinations: start pending/accepted × 1/2 seats × which op holds the lock first (`test_forced_accept_vs_passenger_cancel_vs_driver_cancel`) | **Forced** first holder. The two waiters queue on the ride lock in start order, but PostgreSQL does not guarantee that queue order, so the result must equal the serial model (the real `lifecycle` tables applied one op at a time) for one of the two waiter orders. The order observed is reported. |
| 3 | Multi-seat restoration: a 3-seat passenger cancel vs a ride cancel, both orders, then ROUNDS rounds of 3+2+2-seat cancels, a duplicate cancel and an accept (`test_multi_seat_cancellation_restoration`) | **Forced** (both orders) + **probabilistic** rounds |
| 4 | Ride cancel while passengers hold accepted bookings, racing new accepts, both orders; then ROUNDS HTTP rounds (`test_driver_cancel_with_accepted_holders_racing_new_accepts`) | **Forced** + **probabilistic** |
| 5 | Suspension of the driver (vs a passenger booking, the driver's accept, a passenger's accepted-cancel) and of the passenger (vs the driver's accept, the passenger's own cancel, a keyed booking request). Each runs both orders; the target is reinstated between cases (`test_driver_and_passenger_suspension_racing_book_accept_cancel`) | **Forced**, 12 interleavings |
| 6 | Two admins suspend one user at once (`test_concurrent_suspensions_of_one_user_apply_once`) | **Forced** |
| 7 | Idempotency, per round: 12 identical keyed creates; 6 same-key creates with different payloads (seat count, ride); 8 identical keyed accepts; 4 same-key accept/reject mixes (`test_concurrent_identical_and_conflicting_keys`) | **Probabilistic**, seeded |
| 8 | Idempotency waiters: the first keyed request paused before COMMIT, 5 duplicates confirmed blocked on the key's primary key; the first commits (all replay) or fails (one proceeds, 4 replay) (`test_forced_waiters_replay_after_commit_and_proceed_after_rollback`) | **Forced** |
| 9 | Expiry boundary: accept at `expires_at − 1 µs` / `expires_at` / `+1 µs`. Sweep vs accept forced both ways (A: the sweep holds the ride lock and waits on a booking lock, the accept queues behind it; B: the accept is paused at COMMIT and the sweep must SKIP LOCKED). ROUNDS rounds of accept ‖ sweep at ±1 µs (`test_expiry_boundary_and_sweep_racing_accept`) | Boundary exact + **forced** A/B + **probabilistic** rounds |
| 10 | Departure crossed while request / accept / accepted-cancel / pending-withdraw all wait on the ride lock. The clock moves to the departure instant (inclusive), or to 1 µs before it (`test_departure_crossed_while_waiting_on_ride_lock`) | **Forced** |
| 11 | Suspension waiting on a ride lock while that ride departs, driver and passenger (`test_suspension_waiting_on_ride_lock_rechecks_departure`, regression test for §4) | **Forced** |
| 12 | Expire sweep ×2 and idempotency purge ×2 at once, each on its own engine. Sweep A is paused before its first COMMIT while sweep B runs; purge A likewise. A third run must change nothing (`…_twice_concurrently_forced`) | **Forced** |
| 13 | The same, all four started together with `asyncio.gather` (`…_twice_concurrently_free_running`) | **Probabilistic** |

Each test appends one JSON line (`seed`, `rounds`, outcome counters) to `$STRESS_REPORT`
when that variable is set.

## 3. Stress results

### 3.1 Commands

```bash
source /workspace/work/rs-stress.env   # ci_env.sh + DATABASE_URL/TEST_DATABASE_URL=.../rs_stress, TEST_REDIS_URL=redis://127.0.0.1:6379/8
$PY -m alembic upgrade head
# one run (random seed printed on stderr as [stress] STRESS_SEED=...):
$PY -B -m unittest tests.test_concurrency_stress
# reproduce a run:
STRESS_SEED=<seed> STRESS_ROUNDS=6 $PY -B -m unittest tests.test_concurrency_stress
# 20 iterations (script /workspace/work/rs-stress-iter.sh): a fresh random seed each time,
# logs in /workspace/work/rs-stress-iter/, aggregated outcomes in report.jsonl
ITER=20 ROUNDS=6 /workspace/work/rs-stress-iter.sh
```

### 3.2 Iterations (code at `75bec50`)

**Run 1, code at `75bec50`: 19/20 iterations passed (259/260 test runs).** The single failure was a **test bug**, not a product defect; see below.

| Iter | Seed | Result | Wall time |
|---|---|---|---|
| 1 | `655995196` | Ran 13 tests in 53.998s OK | 59 s |
| 2 | `525239760` | Ran 13 tests in 65.516s OK | 74 s |
| 3 | `428176897` | Ran 13 tests in 63.939s OK | 72 s |
| 4 | `712462206` | Ran 13 tests in 68.802s OK | 76 s |
| 5 | `1032729658` | Ran 13 tests in 59.780s OK | 68 s |
| 6 | `516385675` | Ran 13 tests in 47.149s OK | 51 s |
| 7 | `809842925` | Ran 13 tests in 60.585s OK | 67 s |
| 8 | `273218062` | Ran 13 tests in 64.113s OK | 72 s |
| 9 | `394716163` | Ran 13 tests in 63.114s OK | 72 s |
| 10 | `784200565` | Ran 13 tests in 65.246s OK | 74 s |
| 11 | `855393261` | Ran 13 tests in 65.592s OK | 76 s |
| 12 | `1057978601` | Ran 13 tests in 58.484s OK | 68 s |
| 13 | `18642846` | Ran 13 tests in 65.326s OK | 72 s |
| 14 | `242154356` | Ran 13 tests in 58.255s OK | 67 s |
| 15 | `327156700` | Ran 13 tests in 59.389s OK | 65 s |
| 16 | `537456377` | Ran 13 tests in 62.262s OK | 68 s |
| 17 | `453942662` | Ran 13 tests in 52.787s OK | 58 s |
| 18 | `672518951` | Ran 13 tests in 52.564s OK | 57 s |
| 19 | `369829074` | Ran 13 tests in 55.768s FAILED (failures=1) | 61 s |
| 20 | `496685902` | Ran 13 tests in 57.712s OK | 64 s |

**Iteration 19 (seed `369829074`): `test_expiry_boundary_and_sweep_racing_accept` failed in a probabilistic round** with the booking still `pending` after `accept ‖ sweep` at `expires_at + 1 µs`.

- **Root cause:** the accept held the ride lock while it computed its 409, so the concurrent sweep skipped the ride. That is the documented `SKIP LOCKED` behaviour, and the request is still lazily expired: it cannot be accepted, and listings show `expired`. The test wrongly assumed the sweep always wins. Every SQL invariant passed.
- **Fix (test only, `fd52122`):** when the request is still pending, the test now requires `rides_skipped_locked == 1` and that the next sweep persists `expired`.
- **Verification:**
  - the expiry test alone ran 36 times with `STRESS_ROUNDS=12`: 36/36 passed, exercising the skipped-locked branch 3 times;
  - **Run 2 (confirmation), code at `fd52122`: 10/10 iterations passed** (130/130 test runs).

| Iter | Seed | Result | Wall time |
|---|---|---|---|
| 1 | `200779934` | Ran 13 tests in 42.201s OK | 47 s |
| 2 | `457356212` | Ran 13 tests in 50.461s OK | 57 s |
| 3 | `131734156` | Ran 13 tests in 65.153s OK | 72 s |
| 4 | `252172223` | Ran 13 tests in 50.204s OK | 58 s |
| 5 | `272698307` | Ran 13 tests in 47.733s OK | 52 s |
| 6 | `111780658` | Ran 13 tests in 37.092s OK | 40 s |
| 7 | `582269443` | Ran 13 tests in 33.590s OK | 36 s |
| 8 | `92133585` | Ran 13 tests in 32.198s OK | 35 s |
| 9 | `989729346` | Ran 13 tests in 41.187s OK | 44 s |
| 10 | `356639648` | Ran 13 tests in 39.984s OK | 43 s |

### 3.3 Outcome distribution across the 20 iterations

These counts come from `report.jsonl`. They show the races really happened: all
contention outcomes appear, not only happy paths.

| Test | Outcome (counter key) | Count |
|---|---|---|
| `test_departure_crossed_while_waiting_on_ride_lock` | `('crossed', ('accept', (409, 'Ride has already departed')), ('accepted_cancel', (409, 'Ride has already departed')), ('request', (409, 'R...` | 20 |
| `test_departure_crossed_while_waiting_on_ride_lock` | `('not_crossed', ('accept', (200, 'ok')), ('accepted_cancel', (200, 'ok')), ('request', (200, 'ok')), ('withdraw', (200, 'ok')))` | 20 |
| `test_expiry_boundary_and_sweep_racing_accept` | `('round', -1, 200, 'ok')` | 65 |
| `test_expiry_boundary_and_sweep_racing_accept` | `('round', 1, 409, 'Booking request has expired')` | 53 |
| `test_expiry_boundary_and_sweep_racing_accept` | `('boundary+forced', 'ok')` | 20 |
| `test_concurrent_identical_and_conflicting_keys` | `('create-mixed', 422)` | 480 |
| `test_concurrent_identical_and_conflicting_keys` | `('create-mixed', 201)` | 240 |
| `test_concurrent_identical_and_conflicting_keys` | `('respond-mixed', ((200, 2), (422, 2)))` | 120 |
| `test_forced_waiters_replay_after_commit_and_proceed_after_rollback` | `('first_commits', ((201, 'replay'), 5))` | 20 |
| `test_forced_waiters_replay_after_commit_and_proceed_after_rollback` | `('first_fails', ((200, 'ok'), 1), ((201, 'replay'), 4))` | 20 |
| `test_expire_sweep_and_purge_twice_concurrently_forced` | `('forced', 'skipped', 2, 'purge_split', 7, 16)` | 20 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 6, 'purge_split', 14, 9)` | 2 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 7, 'purge_split', 9, 14)` | 2 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 8, 'purge_split', 14, 9)` | 2 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 8, 'purge_split', 9, 14)` | 2 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 10, 'purge_split', 14, 9)` | 1 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 10, 'purge_split', 21, 2)` | 1 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 10, 'purge_split', 9, 14)` | 1 |
| `test_expire_sweep_and_purge_twice_concurrently_free_running` | `('free', 'skipped', 2, 'purge_split', 16, 7)` | 1 |
| `test_driver_cancel_with_accepted_holders_racing_new_accepts` | `('round', 409, 'Booking already responded to')` | 334 |
| `test_driver_cancel_with_accepted_holders_racing_new_accepts` | `('round', 200, 'ok')` | 151 |
| `test_driver_cancel_with_accepted_holders_racing_new_accepts` | `('round', 409, 'Ride is not active')` | 112 |
| `test_driver_cancel_with_accepted_holders_racing_new_accepts` | `('round', 409, 'No seats available')` | 3 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('accepted', 1, 'dcancel->accept->pcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('accepted', 2, 'dcancel->accept->pcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('accepted', 2, 'pcancel->accept->dcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('pending', 1, 'accept->pcancel->dcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('pending', 1, 'dcancel->accept->pcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('pending', 1, 'pcancel->accept->dcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('pending', 2, 'dcancel->accept->pcancel')` | 20 |
| `test_forced_accept_vs_passenger_cancel_vs_driver_cancel` | `('pending', 2, 'pcancel->accept->dcancel')` | 20 |
| `test_multi_seat_cancellation_restoration` | `('round', 200, 'ok')` | 480 |
| `test_multi_seat_cancellation_restoration` | `('round', 409, 'Booking cannot be cancelled')` | 120 |
| `test_multi_seat_cancellation_restoration` | `('forced', 'cancel_first')` | 20 |
| `test_multi_seat_cancellation_restoration` | `('forced', 'ride_cancel_first')` | 20 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('request', 201, 'ok')` | 1260 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('respond/cancel', 200, 'ok')` | 864 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('respond/cancel', 409, 'No seats available')` | 589 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('phase3-accept', 200, 'ok')` | 500 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('respond/cancel', 409, 'Booking already responded to')` | 463 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('phase3', 201, 'ok')` | 456 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('request', 409, 'You already booked this ride')` | 405 |
| `test_random_one_and_two_seat_requests_and_accepts_on_limited_capacity` | `('phase3', 200, 'ok')` | 396 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('op_first', 'driver_vs_accept', 200, 'ok')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('op_first', 'driver_vs_book', 200, 'ok')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('op_first', 'driver_vs_pcancel', 200, 'ok')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('op_first', 'passenger_vs_accept', 200, 'ok')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('op_first', 'passenger_vs_keyed_book', 200, 'ok')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('op_first', 'passenger_vs_own_cancel', 200, 'ok')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('suspend_first', 'driver_vs_accept', 409, 'Booking already responded to')` | 20 |
| `test_driver_and_passenger_suspension_racing_book_accept_cancel` | `('suspend_first', 'driver_vs_book', 409, 'Ride is not active')` | 20 |

### 3.4 Mutation checks of the harness

Each mutation was applied to product code, the stress file was run (`STRESS_SEED=4242`),
then the file was restored with `git checkout`. The worktree was verified clean after each.
Script: `/workspace/work/rs-stress-mut.sh`; log: `/workspace/work/rs-stress-mutations.log`.

| Mutation | Result |
|---|---|
| M1 `lifecycle.SEAT_EFFECT[(accepted, cancel)] = 0` (no seat restore) | **Killed**: 7 tests fail, each also on the SQL conservation invariant |
| M2 accept without the ride `FOR UPDATE` | **Killed**: 7 tests fail or error (forced tests never block; probabilistic rounds break conservation) |
| M3 request without `require_not_departed` | **Killed**: departure test plus the history invariant |
| M4 suspension without the `token_version` bump | **Killed**: 3 failures (token invariant) |
| M5 idempotent create without the response recorder | **Killed**: 5 failures + 1 error (NULL-response invariant, replays) |
| M6 `book_ride` without its own `lock_active_account` | **Survived**. The suspension race here uses the keyed router path, which takes the user lock before claiming, and the driver-suspension cases don't involve the passenger's user lock. The unkeyed path is covered by `tests/test_account_suspension.py::SuspensionRaceTests::test_suspension_first_rejects_concurrent_booking` |
| M7 suspension audited as `user.view` | **Killed**: audit-count and token invariants |
| M8 sweep `FOR UPDATE` without `SKIP LOCKED` | **Killed**: forced B blocks behind the paused accept until `lock_timeout` (2 errors); a whole-suite run exceeded 100 s |
| M9 purge without `SKIP LOCKED` | **Killed**: forced purge blocks (1 failure, 2 errors) |
| M10 accept without the lazy-expiry check | **Killed**: expiry boundary test |

## 4. Product defect found and fixed (`75bec50`, labeled PRODUCT FIX)

- **Defect.** `services/accounts.suspend_user` took `now = clock.utc_now()` *before* locking
  the rides it changes. A ride can depart while the suspension waits on its lock (behind
  a booking, accept or cancel). The "re-check under the locks" then used that stale time.
- **Effect.**
  - Driver suspension: the departed ride is **cancelled** and its accepted seats are handed
    back. It should have been left alone and reported in `departed_active_ride_ids`.
  - Passenger suspension: the passenger's **accepted** booking on the departed ride is
    cancelled and its seats restored. Both break the departure rule that every other
    writer enforces after its locks (BOOKING_LIFECYCLE.md §3).
  - A pending request that expired during the wait is recorded as `cancelled` instead of
    `expired`.
- **Reproduction (forced).** A coordinator holds the ride lock. The suspension is confirmed
  waiting. The frozen clock moves to the departure instant, then the lock is released.
  - On `48f7580`: ride `(cancelled, 4/4)`, booking `cancelled`. The SQL invariant
    "accepted seat handed back after departure by a passenger cancel or a suspension"
    fires.
  - On `75bec50`: ride `(active, 2/4)`, booking `accepted`, `departed_active_ride_ids=[ride]`.
- **Severity: Low.** The window is the suspension's lock wait (milliseconds to seconds). It
  needs a suspension on a ride departing at that moment, and the result is a cancelled
  ride or refunded seat, not overselling.
- **Fix.** Re-read `now` once the ride locks are held, 4 lines including the comment. The
  pre-lock id snapshot used an earlier time, so it is a superset, and the existing re-check
  narrows it.
- **Verification.**
  - `tests.test_concurrency_stress.SuspensionClockRaceTests` plus
    `tests.test_account_suspension`: 28 tests OK.
  - The regression test fails on the parent commit (2 failures: the assertion plus the SQL
    invariant).

No other defect was found. Everything else in §2 held in every iteration.

## 5. Milestone 3 database checks

Checker: `tests/tools/m3_db_checks.py`. It is test tooling; `tests/` is excluded from the
deploy upload. It refuses any host other than localhost and any database name not
starting with `rs_stress`.

```bash
cd /workspace/work/rs-stress && source /workspace/work/rs-stress.env
export M3_PG="postgresql://rideshare_ci@localhost:5432" M3_PYTHON=$PY   # PGPASSWORD from the env file
$PY -B tests/tools/m3_db_checks.py legacy constraints audit tokens downgrade   # 35/35
$PY -B tests/tools/m3_db_checks.py interrupt                                    # 14/14
```

Logs: `/workspace/work/rs-stress-m3-final-rest.log` and
`/workspace/work/rs-stress-m3-final-interrupt.log` (one JSON line per result).

### 5.1 Legacy upgrade from `d924a6e38f10` (7/7)

**Seed at `d924a6e38f10`:**
- 5 users, including the mixed-case twins `Twin.Case@Example.test` / `twin.case@example.test`
  and one admin;
- 5 rides: two with 0 seats (one active, one completed), one departed but still active,
  one cancelled;
- 9 bookings: 3 accepted, 3 pending (one on the departed ride), 2 cancelled, 1 rejected.

**Results:**
- `alembic upgrade head` is **blocked by the f6b2d8e0c413 preflight**:
  `CaseDuplicateEmails: 1 email group(s) differ only by case; … Affected user IDs: [[3, 4]]`.
  It prints ids, never addresses. Exit 1, `alembic_version = e3a7c41b9f52`, no
  `uq_users_email_lower`, and an md5 fingerprint of every row is unchanged.
- After the operator renames one twin, the upgrade reaches `d5e1a0b00002`. The legacy-row
  fingerprint is unchanged.
- Row shape after the upgrade:
  - every `seats_requested = 1`;
  - every `expires_at` NULL;
  - both 0-seat rides intact;
  - every user `active` with `token_version` 0;
  - 0 audit and 0 idempotency rows;
  - 0 INVALID indexes and 0 NOT VALID constraints.
- `alembic check`: "No new upgrade operations detected".
- The legacy pending request on the departed ride counts as due under the NULL = departure
  rule.

### 5.2 Constraints and FK index coverage (10/10)

- `uq_users_email_lower` rejects `LEGACY.DRIVER@example.test`.
- Partial unique index:
  - pending + pending: rejected;
  - pending next to accepted: rejected;
  - new pending after cancelling the old one: allowed.
- Multi-seat CHECK: 0, 9 and −1 rejected; 1 and 8 accepted.
- FK coverage query (every FK's first column must be the first key of a valid index):
  **no uncovered FK**. FKs checked: `rides.driver_id`, `bookings.ride_id`,
  `bookings.passenger_id`, `idempotency_keys.user_id`, `admin_audit_log.actor_id`.

### 5.3 Audit-log immutability (9/9)

The test uses a non-superuser, non-owner role `rs_stress_app_role`, granted **ALL** on all
tables (the worst case). The role and its database are dropped afterwards.

- **Blocked for the role:**
  - INSERT works;
  - UPDATE, DELETE and TRUNCATE are rejected with `admin_audit_log is append-only (… blocked)`;
  - `ALTER TABLE … DISABLE TRIGGER` fails with `must be owner`;
  - `SET session_replication_role` fails with `permission denied`.
- **Owner/superuser:** a plain DELETE is also rejected by the trigger.
- **Superuser bypass (expected, documented):** `SET LOCAL session_replication_role = replica`
  skips the triggers, so DELETE succeeds (run inside a rolled-back transaction). The
  production app role must therefore be neither the owner nor a superuser. That role
  change is approval-gated and was not made.
- The row count stays intact.

### 5.4 Token invalidation after suspend, over HTTP on a migrated legacy DB (1/1)

| Step | Status |
|---|---|
| Legacy token, no `tv` claim | 200 |
| Suspend | 200 |
| Old token | 401 |
| `tv=1` token while suspended | 401 |
| Reinstate | 200 |
| `tv=0` token | 401 |
| `tv=1` token | 401 |
| `tv=2` token | 200 |

DB afterwards: `token_version = 2`; audit `[user.suspend, user.reinstate]`; the user's
legacy live bookings on future active rides are cancelled.

### 5.5 `kill -9` mid-migration at each revision (14/14)

For each revision after `d924a6e38f10`:

1. Seed legacy data and upgrade to the revision before it.
2. A psql coordinator holds `ACCESS EXCLUSIVE` on the table that revision needs (rides,
   users or bookings).
3. Start `alembic upgrade head` and wait until its backend shows in `pg_stat_activity` as
   lock-waiting.
4. `SIGKILL` the alembic process.
5. Variant **terminated**: `pg_terminate_backend` the orphan before releasing the lock.
   Variant **orphan_finishes**: release the lock and let the orphan run its statement.
6. Read `alembic_version`, count INVALID indexes, retry `alembic upgrade head`, and compare
   `pg_dump --schema-only` with a clean head build.

| Revision | Stamp after kill (both variants) | Retry | Schema vs clean head |
|---|---|---|---|
| e3a7c41b9f52 | d924a6e38f10 | OK → d5e1a0b00002 | identical |
| f6b2d8e0c413 | e3a7c41b9f52 | OK | identical |
| c1a7e0b00001 | f6b2d8e0c413 | OK | identical |
| c1a7e0b00002 | c1a7e0b00001 | OK | identical |
| c1a7e0b00003 | c1a7e0b00002 | OK | identical |
| d5e1a0b00001 | c1a7e0b00003 | OK | identical |
| d5e1a0b00002 | d5e1a0b00001 | OK | identical |

`alembic_version` always named the last fully committed revision
(`transaction_per_migration=True`). There were 0 INVALID indexes after each kill, and
every retry was idempotent.

### 5.6 Downgrade to each supported revision and re-upgrade (8/8)

- **Multi-seat refusal:** with one 2-seat booking present, `downgrade c1a7e0b00002` raises
  `MultiSeatDowngradeRefused`. The stamp stays `c1a7e0b00003` and the row is untouched.
- **Each target**, from head to `d5e1a0b00001`, `c1a7e0b00003`, `c1a7e0b00002`,
  `c1a7e0b00001`, `f6b2d8e0c413`, `e3a7c41b9f52`, `d924a6e38f10`, with legacy data and one
  swept `expired` booking:
  - `alembic_version` equals the target;
  - the schema equals a clean build at that target. The one difference ignored is the
    documented leftover `bookingstatus` label `'expired'`, which PostgreSQL cannot drop;
  - legacy rows are preserved, with `expired` remapped to `cancelled` below `c1a7e0b00002`;
  - re-upgrade to head gives a schema identical to a clean head.

## 6. Full backend suite

```bash
source /workspace/work/rs-stress.env
export DATABASE_URL=postgresql+asyncpg://rideshare_ci:…@localhost:5432/rs_stress_full TEST_DATABASE_URL=$DATABASE_URL
$PY -m alembic upgrade head
STRESS_SEED=20261031 $PY -B -m unittest discover -s tests
```

**Result at `75bec50`, which includes the product fix: `Ran 415 tests in 921.360s — OK`.**
Log: `/workspace/work/rs-stress-full.log`.

- The run was slow because it shared the CPU with the 20-iteration stress loop and the
  Milestone 3 checker.
- What changed after `75bec50`:
  - `4b692e7`: test tooling outside discovery;
  - `fd52122`: one assertion in the stress file, re-run 36 times alone and in the 10-run
    confirmation loop.
- No product file changed after `75bec50`.
- CI on PR #27 runs the full suite on every push.

## 7. Limitations

- **Waiter order.** In forced 3-way races only the **first** lock holder is forced. When both
  waiter orders give the same outcome, the start order is reported.
- **App-clock window.** A guard reads the app clock under the lock, but the UPDATE happens at
  flush. The history check for "after departure" therefore uses `clock_timestamp()` at
  UPDATE time. Forced tests only move the clock while the operation is still waiting, so
  this doesn't affect them.
- **Interrupt test.** `orphan_finishes` does not list which objects the orphaned statement
  left behind. It shows only that the retry converges to the clean head schema.
- **Audit app role.** Checked with a disposable local role. The production role setup is
  approval-gated and was not touched.
- **CPU load.** Iterations ran on a shared sandbox alongside other work. Run times are
  noisy, not benchmarks.
