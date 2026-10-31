# Smoke tests

Non-destructive checks after a deploy. Staging = a FastAPI Cloud preview pointed
at a Neon branch (both approval-gated). Production checks are **read-only**: no
registration, no ride or booking creation, no load. Use obviously fake data on
staging only; never real CNIC/phone numbers.

```sh
BASE=https://<host>          # staging preview or https://rideshare-api.fastapicloud.dev
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
```

## 1. Staging (preview + Neon branch)

Infrastructure

- ☐ `alembic current` on the branch = expected head (Wave 1: `f6b2d8e0c413`).
- ☐ `code $BASE/healthz` → `200`; `curl -s $BASE/readyz` → `{"status":"ready",…}` (PR #7).
- ☐ `code $BASE/test-db` → `404` once PR #7 is in.
- ☐ Headers per [SECURITY_HEADERS_CHECKLIST](SECURITY_HEADERS_CHECKLIST.md) §3; no HSTS.

Static and routing

- ☐ `/`, `/login`, `/register`, `/passenger`, `/driver`, `/account` → `200` HTML with `Cache-Control: no-cache`.
- ☐ A hashed asset from `index.html` → `200`, `Cache-Control: public, max-age=31536000, immutable`; a missing `/assets/x.js` → `404` (not HTML).
- ☐ `/does-not-exist` → `404` JSON; `/docs`, `/redoc`, `/openapi.json` → `200` (unless docs are disabled by decision).

API (fake data, staging only)

- ☐ Unauthenticated `GET /me`, `/rides/`, `/bookings/me` → `401`.
- ☐ Register a fake user (`smoke+<ts>@example.com`, fake CNIC `00000-0000000-1`-style test value, fake `+923000000000`-style number) → `201`; re-register with different email case → `409` (AST-005).
- ☐ Login with different email case → `200` and a token; `GET /me` → `200` with `allowed_modes`.
- ☐ Invalid CNIC/phone → `422` whose body does **not** echo the input (AST-007).
- ☐ Driver: create a ride departing in ~3 min → `201`; it appears in `/rides/me`.
- ☐ Passenger (second fake user): search finds it; request a seat → pending; driver accepts → accepted.
- ☐ After departure: new booking or accept → `409 "Ride has already departed"`; reject still works (AST-001).
- ☐ Passenger cancel works before departure; seat count restored.
- ☐ Browser: SPA journeys (register/login → search → book → status → cancel; driver publish → accept/reject), console free of CSP errors, mobile viewport.
- ☐ If rate limiting is enabled on staging: [REDIS_RATE_LIMIT_PREREQUISITES](REDIS_RATE_LIMIT_PREREQUISITES.md) §5.

Rollback rehearsal: per [MIGRATION_REHEARSAL](MIGRATION_REHEARSAL.md) R9–R10.

## 2. Production (read-only)

Run immediately after a deploy and after any rollback. Stop and roll back on any ✗ in the first block.

```sh
BASE=https://rideshare-api.fastapicloud.dev
for p in / /login /register /docs /openapi.json /healthz /readyz; do echo "$p $(code $BASE$p)"; done
for p in /me /rides/ /bookings/me; do echo "$p $(code $BASE$p)"; done      # expect 401
echo "/nope $(code $BASE/does-not-exist)"                                   # expect 404
curl -s $BASE/readyz
curl -s -o /dev/null -D - $BASE/ | grep -iE 'content-security|x-frame|strict-transport'
```

| Check | Expected now (31 Oct 2026, pre-Wave-1) | Expected after Wave 1 deploy |
|---|---|---|
| `/`, `/login`, `/register` | 200 HTML | 200 HTML |
| `/docs`, `/openapi.json` | 200 | 200 (unless docs disabled) |
| `/healthz` | 404 | 200 `{"status":"ok"}` |
| `/readyz` | 404 | 200 `ready`, `database: ok`, `redis: disabled` |
| `/test-db` | 200 | 404 |
| `/me`, `/rides/`, `/bookings/me` (no token) | 401 | 401 |
| unknown path | 404 JSON | 404 JSON |
| CSP / X-Frame-Options | absent | present (app CSP) |
| HSTS | absent | absent (until approved) |

Optional, with a dedicated **test account** created on purpose for monitoring
(approval once): log in, `GET /me` → 200, `GET /rides/` → 200 list, `GET /bookings/me` → 200.
Do not create rides or bookings in production for smoke purposes.

Browser (2 minutes): open `/`, sign in with the test account, open passenger and
driver pages, open `/docs`; DevTools console shows no CSP errors and no requests to
`localhost` or other origins.

Watch the 5xx rate and logs for 30 minutes after a release.
