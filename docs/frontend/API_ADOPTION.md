# Frontend adoption of the Milestone B API gaps (PR #17) and tolerance for #18/#20

Branch `feat/frontend-api-adoption` (stack: #16 → #14 → #9 + #4, merged `--no-ff` with #17 → #12 → #2).

## API usage
| Gap | Endpoint / header | Where the UI uses it |
|---|---|---|
| G7 totals | `X-Total-Count` on `GET /rides/`, `/rides/me`, `/bookings/me`, `/rides/{id}/bookings` | `api.*Page()` return `{ items, total }`; search summary ("24 rides found. Showing 1–20."), "Page X of Y", Next disabled at `offset + rows >= total`; My bookings tab counts (one `limit=1` read per status); My rides summary; driver ride footnote ("newest 100 of 130"). `total = null` (header missing or not a plain integer) falls back to the old page-size heuristic. |
| G2 my booking for a ride | `GET /bookings/me?ride_id=<id>&limit=5` | Ride details: the first pending/accepted booking, replacing the scan of the newest 100 bookings. |
| G3 search | case-insensitive, trimmed whole-value place match | Copy changed to "Capital letters don’t matter"; the client still trims and still rejects identical places (case-insensitive). |
| D1 driver tabs | `GET /rides/me?status=active&when=upcoming&order=asc` / `…&when=departed` | My rides Upcoming/Departed, paged on the server (no 100-row client split). |
| D2 counts | `GET /rides/me/booking-counts?ride_id=…` (≤100 ids per call) | One call per page: My rides cards ("2 pending" / "No pending"), driver overview badge, ride page summary (exact even beyond 100 requests). Failure hides counts only. |
| D7 / AST-016 / #12 | new 409 strings | Fixed copy in `driver/errors.ts` and `passenger/errors.ts`; unknown strings still get generic copy, exact-key match only. |

## Passenger routes
`/passenger`, `/passenger/search`, `/passenger/bookings`, `/passenger/rides/:id`. The backend already serves `index.html` for `/passenger/*` (`frontend_serving.CLIENT_AREAS`, #16). Old query links (`?view=search|bookings`, `?ride=`) redirect with `replace`, carrying only known parameters to a fixed path. There were no hash (`#/…`) routes to keep. The post-login return allow-list lists the new paths exactly.

## Tolerance for #18 (idempotency) and #20 (pending expiry), not merged here
- `expired` is a `BookingStatus`: passenger groups (`Expired (n)`, read-only, no cancel) and driver request groups (read-only). The Expired *filter tab* appears only when the server accepts `status=expired` (its count read succeeds), so it never shows a 422 on stacks without #20.
- 409 `Booking request has expired` → fixed copy (accept, reject, passenger cancel).
- `Idempotency-Key`: `crypto.randomUUID()` (getRandomValues fallback), one key per user action and scope (`book:<ride>`, `respond:<booking>:accept|reject`), kept when the attempt failed with network/429/5xx so a retry reuses it, dropped after success or any other answer. Keys failing PR #18's format (1–64 visible ASCII) are never sent.
- **Feature flag:** CORS `allow_headers` on this stack is `["Authorization", "Content-Type"]` (main.py), so a cross-origin preflight would fail with the header. The header is therefore sent only for same-origin requests (production bundle; no CORS involved) unless `VITE_SEND_IDEMPOTENCY_KEY=true`. Turn the flag on for split-origin dev once #18 (which adds `Idempotency-Key` to `allow_headers`) is merged. Servers without #18 ignore the header.
