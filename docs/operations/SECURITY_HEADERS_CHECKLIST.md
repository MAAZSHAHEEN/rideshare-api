# Security headers checklist (AST-006 / PR #6)

Middleware: `security_headers.py` (branch `feat/security-headers`), added last in
`main.py` so it is outermost and also covers CORS preflights, rate-limit 429/503,
FastAPI 404/405/422 and static assets. A header already set by a route is never
overwritten. Switches: `SECURITY_HEADERS_ENABLED` (default on), `HSTS_ENABLED`
(default **off**, approval gate), `HSTS_MAX_AGE` (default 31536000).

## 1. Expected headers

| Header | SPA / API / assets | `/docs`, `/redoc` (and sub-paths) |
|---|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'` | adds `'unsafe-inline'` + `https://cdn.jsdelivr.net` for scripts/styles, `fonts.googleapis.com`/`fonts.gstatic.com`, images from `fastapi.tiangolo.com` and `cdn.redoc.ly`, `worker-src 'self' blob:` |
| `X-Content-Type-Options` | `nosniff` | same |
| `X-Frame-Options` | `DENY` | same |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | same |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` | same |
| `Cross-Origin-Opener-Policy` | `same-origin` | same |
| `Strict-Transport-Security` | **absent** until approved; then `max-age=<HSTS_MAX_AGE>; includeSubDomains` | same |

Note: `geolocation=()` must be relaxed deliberately if a future feature (GPS, P2) needs location.

## 2. Validation before merge (local, done for PR #6)

- ☐ `tests/test_security_headers.py` passes (headers on 200/404/405/422/429/CORS preflight, docs CSP, kill switch, HSTS off by default).
- ☐ Real Chromium against a local build: SPA `/`, `/login`, `/register`, `/docs`, `/redoc` render with **0 CSP violations** (recorded in the Wave 1 merge-readiness report).

## 3. Validation on preview, then production (after deploy approval)

```sh
BASE=https://<preview-or-prod-host>
for p in / /login /assets/<a-hashed-file>.js /docs /redoc /openapi.json /me /does-not-exist; do
  echo "== $p"; curl -s -o /dev/null -D - "$BASE$p" | grep -iE '^(HTTP|content-security|x-frame|x-content|referrer|permissions|cross-origin-opener|strict-transport)'
done
curl -s -o /dev/null -D - -X OPTIONS "$BASE/auth/login" -H 'Origin: https://example.invalid' -H 'Access-Control-Request-Method: POST'
```

- ☐ Every path shows the §1 headers; `/docs` and `/redoc` show the docs CSP, all others the app CSP.
- ☐ No `strict-transport-security` yet.
- ☐ Cloudflare/the platform does not strip or duplicate headers (one CSP header per response).
- ☐ Browser check (Chrome and Firefox, desktop + mobile emulation): open `/`, log in with a **test** account, navigate passenger/driver/account pages, open `/docs` and `/redoc`. DevTools console: **no** `Refused to …` CSP messages. Network: fonts load from same origin (self-hosted DM Sans/Manrope), no request to `fonts.googleapis.com` from the SPA.
- ☐ Clickjacking: an `<iframe src="$BASE/">` on a local page is refused.
- ☐ Kill switch on the preview only: `SECURITY_HEADERS_ENABLED=false` → headers gone; restore `true`.

## 4. CSP report review (before HSTS, and before tightening the docs CSP)

The middleware has **no `report-uri`/`report-to`** today, so the only evidence is
the manual console review above. Before enabling HSTS, collect real-browser data:

1. Choose a collector (Sentry's CSP endpoint if Sentry is adopted, or a hosted report service). Its URL is not secret but add it via config, not hard-coded.
2. Small code change (separate PR): add `report-to`/`report-uri` to both policies, plus a `Reporting-Endpoints` header. Optionally ship a stricter candidate policy as `Content-Security-Policy-Report-Only` alongside the enforced one.
3. Burn-in **≥ 7 days** of real traffic, covering mobile browsers.
4. Review reports: ignore browser-extension noise (`chrome-extension://`, `moz-extension://`); any violation from our own origin or from the docs CDNs is a bug to fix before going further.
5. Sign-off: zero first-party violations for 7 days → eligible for HSTS.

## 5. HSTS enablement (approval gate)

Prerequisites:

- ☐ §3 and §4 complete.
- ☐ Every page and subresource is HTTPS (true today: same-origin assets; docs CDNs are HTTPS).
- ☐ Domain facts understood: `rideshare-api.fastapicloud.dev` is under `.dev`, which is on the browsers' HSTS preload list as a whole TLD, so browsers already refuse HTTP there. The header adds value mainly for a future custom domain and non-preloading clients.
- ☐ `includeSubDomains` is always sent. On a custom **apex** domain this forces HTTPS for every subdomain; confirm all of them serve HTTPS first. Never add `preload` or submit the domain to the preload list from this app.

Ramp (each step an env change + restart, approval for the first step):

| Step | `HSTS_MAX_AGE` | Hold | Check |
|---|---|---|---|
| 1 | `300` (5 min) | 1 day | header present on all paths; no user reports |
| 2 | `86400` (1 day) | 1 week | same |
| 3 | `31536000` (1 year) | — | final |

Rollback: `HSTS_ENABLED=false` stops sending it, but browsers keep enforcing for the
last `max-age` they saw; to clear sooner, keep HSTS enabled with `HSTS_MAX_AGE=0`
until clients have revisited.
