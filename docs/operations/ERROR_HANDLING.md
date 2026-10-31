# Error handling, request IDs and request body limit

Status: implemented in `fix/safe-errors-body-limit` (AST-007b, stacked on PR #7).
Code: `http_safety.py`, wired in `main.py`; setting in `config.py`.

## Sanitised 500 responses and logs

Any exception that is not an `HTTPException` or a request validation error is
answered with:

```
HTTP/1.1 500
Content-Type: application/json
X-Request-ID: <id>

{"detail":"Internal server error"}
```

and produces exactly one log line on the `rideshare.errors` logger at `ERROR`,
a JSON object with only these fields:

| Field | Example | Notes |
|---|---|---|
| `event` | `unhandled_exception` | constant |
| `request_id` | `3f0c…` | same value as the `X-Request-ID` response header |
| `method` | `POST` | |
| `route` | `/rides/{ride_id}` | route **template**; `<unmatched>` when no route matched (e.g. a middleware failed). Raw paths and query strings are never logged. |
| `exc_type` | `sqlalchemy.exc.IntegrityError` | module + class name |
| `location` | `routers/auth.py:95:register` | deepest frame inside the repository: file, line, function only |
| `sqlstate` | `23505` | DB errors only; must match `[0-9A-Z]{5}` or it is logged as `null` |
| `constraint` | `uq_users_phone` | DB errors only; must be a plain SQL identifier or it is logged as `<invalid>` |
| `db_error` | `UniqueViolationError` | DB errors only; driver exception class name |

The app configures no logging handlers yet, so these lines reach stderr through
Python's last-resort handler (WARNING and above, message only), which FastAPI
Cloud collects. When the structured-logging design in `LOGGING_MONITORING.md`
lands, attach its JSON formatter to `rideshare.errors`; never add `exc_info`.

Never logged: the exception message, its chained causes (`__cause__`/`__context__`),
the traceback, PostgreSQL `DETAIL`/`Failing row contains (…)` text, SQL bound
parameters, request bodies or headers, CNIC, phone numbers, passwords or hashes,
JWTs, DSNs. The handler does not call `str(exc)` and does not pass `exc_info`.

### Why a middleware and not `add_exception_handler(Exception, …)`

Starlette's `ServerErrorMiddleware` (where an `Exception` handler is installed)
always **re-raises** after sending the 500 so the server can log it. Uvicorn then
logs `Exception in ASGI application` with the full exception chain, which for
database errors includes `DETAIL: Key (cnic)=(…)`. `SafeErrorMiddleware` sits
inside `ServerErrorMiddleware`, sends the 500 itself and does **not** re-raise,
so neither Starlette nor Uvicorn ever sees the exception. A test starts a real
Uvicorn process and asserts its output has no `Traceback` and no fake markers.

### Layering (outermost first)

1. `ServerErrorMiddleware` (Starlette, now never reached by app exceptions)
2. `RequestIdMiddleware` – accepts an inbound `X-Request-ID` only if it matches
   `^[A-Za-z0-9-]{8,64}$`, otherwise generates a UUID4; sets it on every response.
3. `SafeErrorMiddleware` (outer) – catches failures in the middleware layers below
   (CORS, rate limiter), logged with route `<unmatched>`.
4. `CORSMiddleware` – so route 500s and 413s still carry CORS headers and the
   browser can read them (`X-Request-ID` is exposed).
5. `RequestBodyLimitMiddleware`
6. `AuthRateLimitMiddleware`
7. `SafeErrorMiddleware` (inner) – catches route/dependency exceptions.
8. `ExceptionMiddleware` (FastAPI) – `HTTPException` and `RequestValidationError`
   handlers, unchanged.

### Existing 409 mappings are unchanged

Routes still translate known constraint violations before anything reaches the
generic layer: `ix_users_email` and `users_cnic_key` (registration; and
`uq_users_email_lower` once AST-005 lands) and `uq_bookings_active_passenger_ride`
(booking) return 409. Only *unmapped* database errors (e.g. `users_pkey`, CHECK
violations, connection errors) become a 500.

### Behaviour change for tests

Unhandled exceptions no longer propagate out of the ASGI app, so tests that used
`assertRaises` around an HTTP call now assert a 500 response and the safe log
record (see `assert_internal_error` in `tests/test_booking_responses.py`).

### Using it in an incident

Ask the user for the `X-Request-ID` from the failing response (the frontend can
read it), then search logs for `"request_id": "<id>"`. `location`, `sqlstate` and
`constraint` usually identify the cause; reproduce locally for the full detail.

## Request body size limit

- `MAX_REQUEST_BODY_BYTES`, default `65536` (64 KiB). Integer in `[1024, 10485760]`;
  any other value (empty, negative, `64k`, `1e5`, non-ASCII digits) stops startup
  with `RuntimeError`. API payloads are small JSON objects; 64 KiB is generous.
- A declared `Content-Length` above the limit is answered `413
  {"detail":"Request body too large"}` before the app or the database is touched.
- Chunked bodies, or bodies longer than their declared length, are counted while
  streaming; the first chunk that crosses the limit stops the read and the
  response becomes 413 (FastAPI would otherwise turn the failed read into a 400;
  the limiter replaces whatever the app tries to send).
- Exactly `MAX_REQUEST_BODY_BYTES` bytes is accepted; one more byte is rejected.
- Responses are not limited: static frontend assets, `/healthz` and `/readyz`
  (bodyless GET/HEAD) are unaffected.
- A 413 is not logged as an error.

### Rollout and rollback

- No env change is required; the default applies. Setting the variable on FastAPI
  Cloud is an env change and needs Maaz's approval.
- If a legitimate client is rejected, raise `MAX_REQUEST_BODY_BYTES` (needs approval)
  or revert the commit; there is no schema or data change.
