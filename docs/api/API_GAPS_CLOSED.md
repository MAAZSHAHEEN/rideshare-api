# Milestone B — API gaps closed (branch `feat/api-gaps`)

Stacked on `feat/booking-lifecycle-guards` (#12), which is stacked on
`fix/booking-departure-rules` (#2). All changes are **additive**: no response
body changes shape, no migration. Contract tests that pinned old *behaviour*
(exact place matching, one 409 string) were updated deliberately and are listed below.

## 0. What the API already did (verified before changing anything)
| Behaviour | Status on the base (#12 @ 57cda1e) | Evidence |
|---|---|---|
| Discovery hides departed rides (`departure_time <= now`, lifecycle clock) | **already done** (#12 moved search to `clock.utc_now()`) | `tests/test_rides.py::test_only_future_active_rides_with_seats_are_searchable`; new `test_discovery_excludes_departed_inactive_and_full_rides_from_rows_and_total` |
| Discovery hides non-active and sold-out rides | **already done** | same tests |
| `Retry-After` exposed over CORS (G6) | **already done** (`main.py` `expose_headers=["Retry-After"]`) | was not tested; now pinned in `tests/test_cors.py` |
| `/bookings/me?status=`, `/rides/me?status=`, `/rides/{id}/bookings?status=` + `limit/offset` | already done | `tests/test_frontend_reads.py` |
| My booking for a ride (G2), case-insensitive places (G3), totals (G7), `/rides/me` time filters/order (D1), request counts (D2), distinct withdrawn 409 (D7) | **missing** → implemented here | below |
| Deep links `/passenger/*`, `/driver/*` (G1) | fixed on `feat/driver-journey` (#16, commit 89f532a), **not on this branch** | — |

## 1. Gap → endpoint → contract → tests

### G2 — passenger booking lookup / history
- `GET /bookings/me?ride_id=<int>` (new optional query param; combine with existing `status`, `limit`, `offset`).
- 200 `PassengerBookingResponse[]` (unchanged shape), newest first (`id DESC`), plus `X-Total-Count`.
- The `passenger_id = caller` condition always applies: another user's booking or ride gives `[]` / `X-Total-Count: 0`, never 403/404, so the endpoint can't be used to probe IDs.
- `ride_id` must be an integer in 1..2^31−1, otherwise 422 (FastAPI validation list). 401 without a token; 403 `Only passengers can list their bookings` for admin.
- "My live booking for this ride": `?ride_id=X&status=pending` and `?ride_id=X&status=accepted`, or `?ride_id=X&limit=5` and pick the first pending/accepted (at most one exists: partial unique index `uq_bookings_active_passenger_ride`).
- Tests: `test_api_gaps.py::test_g2_my_booking_for_a_ride_with_history_and_status`, `test_g2_ride_filter_never_reveals_other_passengers_bookings` (IDOR, both legacy roles, missing ride), `test_g2_validation_auth_and_role`.

### G3 — case-insensitive, trimmed origin/destination search
- `GET /rides/?origin=&destination=`: same params. Matching is now `lower(btrim(column)) = lower(<input stripped>)`, a whole-value match. Both sides are case-folded by PostgreSQL, so the folding rules are identical. `%` and `_` are literal (no LIKE).
- **Deliberate behaviour change:** `peshawar` and ` Peshawar` now match `Peshawar` (they returned nothing before). A value that is blank after trimming → 422 `{"detail": "origin must not be blank"}` / `"destination must not be blank"` (previously `"   "` was an exact-match filter that matched nothing).
- Index check (PR #3 / AST-003 adds `ix_rides_driver_id_departure_time_id`, `ix_bookings_*`; the search index is `ix_rides_status_departure_time_id (status, departure_time, id)` from b83d12f7a906). **No existing index covers origin/destination**, and that was already true for the old exact match, so `lower()` causes **no regression**: the place predicate was a filter before and still is.
- **Expression index: recommended later, not added here** (no migrations on this branch). Measured on a 200 000-row synthetic table (temp table, rolled back): Lahore → Quetta, `LIMIT 20`: **seq scan 101 ms** with the current index vs **bitmap index scan 8.8 ms** with the index below. At today's data volume it isn't needed; add it before discovery traffic grows. Proposed migration (next in the chain, after the planned `d5e1a0b00003`; same autocommit/CONCURRENTLY/re-runnable pattern as `e3a7c41b9f52`):
  ```sql
  CREATE INDEX CONCURRENTLY ix_rides_active_route_departure
    ON rides (lower(btrim(origin)), lower(btrim(destination)), departure_time, id)
    WHERE status = 'active';
  -- downgrade: DROP INDEX CONCURRENTLY IF EXISTS ix_rides_active_route_departure;
  ```
  The query must keep using exactly `services.rides.place_key()` (`lower(btrim(col))`) for the index to apply. No data rewrite, no constraint, and legacy rows can't violate anything. Rollback is a drop.
- Tests: `test_g3_search_trims_and_case_folds_both_sides`, `test_g3_blank_place_after_trimming_is_422`, `test_g3_wildcards_are_literal`; `tests/test_rides.py::test_location_filters_trimmed_case_insensitive_individually_and_combined` (renamed from `test_exact_location_filters_individually_and_combined`, expectations updated deliberately).

### G7 — accurate pagination totals (and G6 header exposure)
- Response header **`X-Total-Count: <int>`** on `GET /rides/` (search), `GET /bookings/me`, `GET /rides/me`, `GET /rides/{id}/bookings`. It counts rows matching all filters, ignoring `limit`/`offset`, and is present (as `0`) on empty pages. It is absent on error responses. Bodies stay JSON arrays (contract tests unchanged). The header is documented in OpenAPI (`responses.200.headers`).
- CORS `expose_headers = ["Retry-After", "X-Total-Count"]` (`main.py`), so split-origin dev builds can read both.
- The count is a second statement in the same request session (READ COMMITTED). A write that commits between the page query and the count can make them differ by the rows it touched; treat the count as a pagination hint, not a lock. Search uses one `now` for both statements, so the departure boundary is identical.
- Tests: `test_g7_search_total_ignores_limit_and_offset`, `test_g7_driver_lists_and_ride_bookings_totals`, `test_g7_errors_carry_no_total`, `test_discovery_excludes_departed_inactive_and_full_rides_from_rows_and_total`; `tests/test_cors.py::test_explicit_origins_preflight_and_http_error_headers` (expose list).

### Discovery excludes departed / non-active rides
- Already the default (see §0); now also pinned for the total: departed (`== now` included), cancelled, completed and sold-out rides are excluded from both rows and `X-Total-Count`, even with a `departure_from` in the past.

### D1 — `/rides/me` filtering
- `GET /rides/me` new optional params:
  - `departure_from`, `departure_before`: aware ISO-8601 datetimes, `[from, before)`, same semantics and 422s as search (naive → 422, `from >= before` → 422 `departure_from must be before departure_before`).
  - `when=upcoming|departed`: lifecycle clock and boundary. `upcoming` = `departure_time > now`, `departed` = `departure_time <= now`. A time filter only: combine with `status=active` for the UI's Upcoming/Departed tabs.
  - `order=asc|desc`: default **`desc`** (departure DESC, id DESC, unchanged). `asc` = departure ASC, id ASC (soonest first).
  - Plus `X-Total-Count`. Invalid `when`/`order` → 422. Admin → 403, no token → 401.
- Tests: `test_d1_upcoming_and_departed_split_at_the_lifecycle_boundary` (ride at exactly now is departed, now+1 µs is upcoming), `test_d1_status_window_order_and_pagination_combine` (PKT offset input, exclusive end, id tie-break, other driver excluded), `test_d1_validation_and_role`.

### D2 — pending request count per ride
- Chosen: **new endpoint** (the least breaking option; `RideResponse` is pinned and is also nested in passenger bookings, so a per-owner field there would leak or need two shapes).
- `GET /rides/me/booking-counts?ride_id=1&ride_id=2…` (1..100 ids, each 1..2^31−1) → 200 `[{"ride_id": 1, "pending": 2, "accepted": 1, "rejected": 0, "cancelled": 1}, …]`.
- Only the caller's own rides are returned. Unknown ids and other drivers' rides are **silently omitted** (no existence oracle). Order follows the request, duplicates removed. Owned rides with no bookings return zeros.
- 0 or more than 100 ids, or a non-integer → 422. Admin → 403 `Only drivers can list their rides`. No token → 401. The route sits under `/rides/me/…` and does not shadow `/rides/{id}` or `/rides/{id}/bookings` (tested).
- Tests: `test_d2_counts_by_status_for_own_rides_only`, `test_d2_validation_auth_and_role` (100 accepted, 101 rejected), `test_d2_route_does_not_shadow_ride_paths`.

### D7 — distinct 409s for "passenger withdrew" vs "already answered"
- `PATCH /bookings/{id}/respond?accept=true|false`:
  - booking `cancelled` **and** ride still `active` → 409 **`Booking was cancelled by the passenger`** (new string). On an active ride, only the passenger can have cancelled it.
  - booking `accepted` or `rejected` → 409 `Booking already responded to` (**unchanged**).
  - booking `cancelled` on a cancelled/completed ride (the cascade) → 409 `Booking already responded to` (**unchanged**, booking-first guard order kept).
- Guard order, lock order (Ride → Booking) and every other string are unchanged. If a future flow cancels single bookings on an active ride (M5 admin moderation, pending expiry), `lifecycle.respond_target` must be revisited (comment in code).
- Tests: `test_d7_withdrawn_vs_already_answered` (5 states × accept/reject), `test_d7_passenger_withdraws_then_driver_answers_through_the_api`, `test_d7_withdrawal_racing_an_accept_reports_the_withdrawal` (forced order on the Ride lock); `tests/test_lifecycle.py` HTTP matrix expectation updated deliberately for (active, cancelled, accept|reject).

### Also on this stack (from #12, for the frontend)
- `PATCH /rides/{id}/complete` before departure → 409 `Ride cannot be completed before its departure time` (AST-016).
- `PATCH /bookings/{id}/cancel` on an accepted booking at/after departure → 409 `Ride has already departed`.

## 2. Exact frontend changes required (for the lead to dispatch; frontend not touched here)
Files are on `feat/driver-journey` (#16) / `feat/passenger-journey` (#14).

1. **`src/api/client.ts`** — add a list variant that returns the total:
   - In `request`, keep the body behaviour and add `requestPage<T>(path, …): Promise<{ items: T[]; total: number | null }>` that reads `response.headers.get('X-Total-Count')` (`Number.parseInt`, `null` if missing or invalid, so older backends still work).
   - `myBookings(offset, signal, options: { status?; limit?; rideId?: number })` → append `ride_id` when `rideId` is set; return `{ items, total }` (or add `myBookingsPage` to keep existing callers).
   - `myRides(offset, signal, options: { status?; limit?; when?: 'upcoming' | 'departed'; order?: 'asc' | 'desc'; departureFrom?: string; departureBefore?: string })` → map to `when`, `order`, `departure_from`, `departure_before`.
   - `searchRides` and `rideBookings` → page variants with `total`.
   - New `rideBookingCounts(rideIds: number[], signal)` → `GET /rides/me/booking-counts?ride_id=…&ride_id=…` (chunks of 100) → `RideBookingCounts[]`. Add `RideBookingCounts` to `types.ts`: `{ ride_id: number; pending: number; accepted: number; rejected: number; cancelled: number }`.
2. **Passenger `RideDetails.tsx`** — replace the "newest 100 of `/bookings/me`" scan with `myBookings(0, signal, { rideId, limit: 5 })` and pick the first `pending`/`accepted`. Delete the "best effort" limitation copy.
3. **Passenger `SearchRides.tsx`** — use `total` for the result count ("N rides") and for Next/Prev (`offset + items.length < total`) instead of the page-size heuristic. The exact-match hint can be removed or softened: casing and surrounding spaces no longer matter. Keep client-side trimming, and keep rejecting blank values (the server now returns 422 for them).
4. **Passenger `MyBookings.tsx`** — use `total` for tab counts and pagination. "All" no longer needs `limit=100` to count.
5. **Driver `MyRides.tsx`** — Upcoming tab: `myRides(offset, signal, { status: 'active', when: 'upcoming', order: 'asc', limit: 20 })`. Departed tab: `{ status: 'active', when: 'departed' }` (default desc). Drop the client-side split over 100 rows. Use `total` for pagination/counts. Cards: one `rideBookingCounts(ids)` call per page to show "N pending".
6. **Driver overview (`Dashboard.tsx`)** — optional: a pending badge per ride via `rideBookingCounts`.
7. **Driver `DriverRide.tsx`** — use `rideBookings` `total` for the "N pending · M accepted" summary when more than one page exists, or `rideBookingCounts([id])`.
8. **`src/driver/errors.ts`**:
   - `ACCEPT_CONFLICTS['Booking was cancelled by the passenger'] = 'The passenger withdrew this request, so it can’t be answered. The list has been refreshed.'`, and the same key in `REJECT_CONFLICTS`.
   - Narrow `'Booking already responded to'` copy to "This request was already answered. The list has been refreshed with its current status." (keep the key; the server still sends it for accepted/rejected and for cascades).
   - `RIDE_CONFLICTS['Ride cannot be completed before its departure time'] = 'This ride can be marked completed only after its departure time.'` (from #12; today it falls back to the generic "Something changed…" copy).
9. **`src/passenger/errors.ts`** — `CANCEL_CONFLICTS['Ride has already departed'] = 'This ride has already departed, so an accepted seat can no longer be cancelled.'` (from #12; currently generic).
10. **Tests** — Vitest for the client header parsing (missing, invalid, `0`), the new error keys (unknown details still generic), and the Upcoming/Departed queries. Playwright mocks should add `X-Total-Count` and `Access-Control-Expose-Headers` in the dev-server mode.
No existing string the UI matches on was removed. `Booking already responded to`, `Ride is not active`, `No seats available`, `Ride has already departed` and `You already booked this ride` are all still sent.
