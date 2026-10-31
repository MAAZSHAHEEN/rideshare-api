# Staging smoke checklist (RC on a FastAPI Cloud preview + Neon branch)

Prepared 31 Oct 2026 (staging-ops). Replaces the one-paragraph list in SECURITY_REVIEW_RC §8 and
LAUNCH_BLOCKERS §2.5 with exact steps. **The preview deploy and the Neon branch are approval-gated.**
Run against the preview only; §6 lists the read-mostly subset that may later run against production.
Use synthetic accounts only (`smoke-<random>@example.com`, fake CNIC/phone formats from the tests).

## 1. What changed underneath (why this smoke exists)

| Area | RC state (PR) | Risk on the real platform | Covered by |
|---|---|---|---|
| Starlette 0.52 → **1.3.1**, FastAPI 0.136.3 (#10) | major; `on_startup`/`on_shutdown` gone (app uses `lifespan`), TestClient on httpx (`follow_redirects`), middleware stack built once at startup | lifespan on FastAPI Cloud's runner, static `/assets`, `redirect_slashes` 307 `Location` behind the proxy, proxy headers | S-1…S-6 |
| **Middleware order** (`main.py`): added first runs innermost → `SafeErrorMiddleware` (inner) → `AuthRateLimitMiddleware` → `RequestBodyLimitMiddleware` → `CORSMiddleware` → `SafeErrorMiddleware` (outer) → `RequestIdMiddleware` → `SecurityHeadersMiddleware` (outermost) | headers must be on **every** response incl. 413/422/500/CORS preflight; `X-Request-ID` on every response; CORS headers even on generic 500 | S-7…S-12 |
| Request body limit `MAX_REQUEST_BODY_BYTES` default 65 536 (#15) | declared `Content-Length` over the limit → 413 before the app; chunked bodies counted while streaming | the platform proxy may impose its own limit first (different body) | S-9 |
| python-jose → **PyJWT 2.15.1** (#10); `tv` claim (#19) | `jwt.decode(..., algorithms=[ALGORITHM], options={"require": ["sub","role","exp"]})`; missing `tv` read as 0; tokens issued by the old (jose) production build must still validate (golden tests `tests/test_jwt_compat.py`) | a forced logout of every user at deploy, or acceptance of `alg=none`/HS512 | S-13…S-18 |
| CORS (`FRONTEND_ORIGINS`, methods GET/POST/PATCH, headers Authorization/Content-Type/Idempotency-Key, exposes Retry-After/X-Request-ID/X-Total-Count/Idempotent-Replayed) | wrong origin list on the preview | SPA can't call the API, or any origin can | S-10, S-11 |
| Probes `/healthz` `/readyz`, `/test-db` retired (#7) | monitor targets | S-3, S-4 |

## 2. Setup (operator)

```bash
export BASE="https://<preview-host>"          # never the production host for §3–§5
export ORIGIN="https://<preview-frontend-origin>"   # must be in FRONTEND_ORIGINS
export PW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"  # local only, not logged
EMAIL_D="smoke-d-$(date +%s)@example.com"; EMAIL_P="smoke-p-$(date +%s)@example.com"
h() { curl -sS -o /dev/null -D - "$@" | tr -d '\r'; }      # headers only
```

Old-token step needs one token issued by the **currently deployed** production build for a
**staging** account — i.e. issue it from a preview of `e8da425` (old build) against the same branch
and the same staging `SECRET_KEY`, then switch the preview to the RC build. If that is not possible,
rely on the golden-token unit tests and mark S-17 "not run".

## 3. Platform and HTTP layer

| # | Step | Expect |
|---|---|---|
| S-1 | `h "$BASE/"` | 200, `text/html`, CSP + `X-Frame-Options: DENY` + `X-Content-Type-Options: nosniff` + `Referrer-Policy`; `X-Request-ID` present |
| S-2 | `h "$BASE/rides/some/deep/link-that-is-spa"` (a frontend route, e.g. `/driver`) and `h "$BASE/assets/<file from index.html>"` | SPA deep link 200 HTML; asset 200 with long `Cache-Control` (immutable) |
| S-3 | `curl -sS "$BASE/healthz"`; `curl -sS "$BASE/readyz"` | `{"status":"ok"}`; `{"status":"ready","checks":{"database":"ok","redis":"disabled"}}`, `Cache-Control: no-store` |
| S-4 | `h "$BASE/test-db"` | 404 (retired) |
| S-5 | `h "$BASE/docs"`, `h "$BASE/openapi.json"` | 200; docs page has its own relaxed CSP and renders (check in a browser, console free of CSP errors) |
| S-6 | `h "$BASE/rides"` (no trailing slash) | 307 with `Location` on the **preview's https host** (not `http://`, not an internal host) |
| S-7 | `h -H 'X-Request-ID: smoke-0123456789abcdef' "$BASE/healthz"` and with `X-Request-ID: bad id!` | first echoed verbatim; second replaced by a fresh UUID |
| S-8 | `curl -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d '{"email":"x"}' -D -` | 422, sanitised body (no input echo of the password field), security headers present |
| S-9 | `head -c 70000 /dev/zero \| curl -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' --data-binary @- -D - -o /dev/null` | **413** from the app with JSON body and security headers; if the platform answers first, record its status/body and size threshold |
| S-10 | `h -X OPTIONS "$BASE/bookings/1" -H "Origin: $ORIGIN" -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: authorization,content-type,idempotency-key'` | 200, `access-control-allow-origin: $ORIGIN`, allow-headers include `idempotency-key`; no `allow-credentials` |
| S-11 | same with `-H 'Origin: https://evil.example'` | no `access-control-allow-origin` header |
| S-12 | a response that is a 500 is hard to force safely — skip on the preview; covered by `tests/test_safe_errors.py` | — |

## 4. Auth (PyJWT) and the core flow

```bash
reg() { curl -sS -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Smoke $2\",\"email\":\"$1\",\"password\":\"$PW\",\"cnic\":\"$3\",\"phone_number\":\"$4\",\"role\":\"$2\"}" -w ' %{http_code}\n'; }
login() { curl -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$PW\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
```

| # | Step | Expect |
|---|---|---|
| S-13 | `reg "$EMAIL_D" driver <fake CNIC> <fake mobile>`; `reg "$EMAIL_P" passenger …` | 201 each; response has no CNIC/phone |
| S-14 | register `$EMAIL_P` again in UPPER CASE | 409 (AST-005), not 500 |
| S-15 | `TD=$(login "$EMAIL_D")`; decode the payload locally: `python3 -c "import sys,json,base64;p=sys.argv[1].split('.')[1];print(json.loads(base64.urlsafe_b64decode(p+'=='*2)))" "$TD"` | header `alg=HS256`; claims `sub`, `role`, `exp` ≈ now+60 min, `iat`, `tv`=0; `curl -H "Authorization: Bearer $TD" $BASE/me` 200 |
| S-16 | tampered tokens: (a) same token with last signature char changed; (b) header `{"alg":"none"}` + payload, empty signature; (c) token without `exp` signed with a wrong key | 401 each, generic body |
| S-17 | old-build token (§2) → `GET /me` | 200 (no forced logout); after a suspend of that account (if an admin exists on the branch) → 401 |
| S-18 | expired token (from the old build after 60 min, or skip) | 401 |
| S-19 | driver: `POST /rides/` (future departure, 3 seats); passenger: `POST /bookings/<ride_id>` with `Idempotency-Key: smoke-<uuid>` and `{"seats_requested":2}` (if `BOOKING_MAX_SEATS` ≥ 2) twice | 201 then 201 replay with `Idempotent-Replayed: true`, one booking |
| S-20 | driver: `PATCH /bookings/<id>/respond` accept → `GET /rides/<id>` | seats reduced by the requested count |
| S-21 | passenger cancel → seats restored; ride cancel → 200; booking after departure → 409 | per lifecycle |
| S-22 | `GET /rides/?limit=5` | `X-Total-Count` exposed |
| S-23 | jobs: `DATABASE_URL=<branch direct> python -m jobs.expire_bookings; echo $?` | last line `job=expire_bookings status=ok expired=… closed=… duration_ms=…`, exit 0; second concurrent copy prints `job_lock=skipped`, exit 0 |

## 5. Browser (Playwright) against the preview

The Playwright config reads `PLAYWRIGHT_BASE_URL` (no dev server is started when it is set) and the
live specs read the API from `PLAYWRIGHT_API_ORIGIN` (`frontend/playwright.config.ts`,
`frontend/e2e/fixtures.ts` on the RC). The SPA is served by the API, so both are the preview host:

```bash
cd frontend && npm ci
PLAYWRIGHT_BASE_URL="$BASE" PLAYWRIGHT_API_ORIGIN="$BASE" npx playwright test e2e/live-backend.spec.ts e2e/passenger-live.spec.ts \
  e2e/driver-live.spec.ts e2e/multi-seat-live.spec.ts e2e/a11y.spec.ts --reporter=line
```

Expect 0 failures, axe 0 serious/critical, and no CSP violations in the console.

## 6. Production subset (after the RC deploy; separate approval)

S-1, S-3, S-4, S-5, S-6, S-7, S-10, S-11 (read-only), S-15/S-17 with a staff test account only.
No registrations or bookings on production without a cleanup plan.

## 7. Record

| Step | Result | Notes (status codes, header names — never tokens or passwords) |
|---|---|---|
| S-1 … S-23 | | |
