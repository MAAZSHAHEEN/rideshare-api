# Production configuration checklist

For `rideshare-api.fastapicloud.dev` (FastAPI Cloud, Neon PostgreSQL). Full variable
reference: [docs/ENVIRONMENT.md](../ENVIRONMENT.md). Changing any production
variable is an approval gate (Maaz). This list is for review, not execution.

Observed live state (passive unauthenticated GETs, 31 Oct 2026 ~02:30 PKT):
served through Cloudflare; `/`, `/docs`, `/redoc`, `/openapi.json` and `/test-db`
return 200; `/healthz` and `/readyz` 404 (PR #7 not deployed); only
`x-content-type-options` and `cache-control` headers on `/` (PR #6 not deployed);
`/me`, `/rides/`, `/bookings/me` return 401 without a token.

## 1. Variables

Check each in the FastAPI Cloud dashboard **without copying values anywhere**.

| ✓ | Variable | Prod required | Default | Secret | Expected production value |
|---|---|---|---|---|---|
| ☐ | `DATABASE_URL` | yes | — | yes | Neon **production root branch**, `postgresql+asyncpg://…?ssl=require`, application role (not the Neon owner role if a lesser role exists). Pooled (`-pooler`) host is fine for the app; use the direct host for `pg_dump` and long migrations. |
| ☐ | `SECRET_KEY` | yes | — | yes | ≥ 48 random URL-safe chars, unique to production, never the `main` branch placeholder (see [AST-002 plan](../security/AST-002_REMEDIATION_PLAN.md)). |
| ☐ | `ALGORITHM` | no | `HS256` | no | `HS256` (or unset). |
| ☐ | `ACCESS_TOKEN_EXPIRE_MINUTES` | no | `60` | no | `60` or lower. No revocation exists, so do not raise it. |
| ☐ | `FRONTEND_ORIGINS` | no | empty | no | Empty if the SPA is only served same-origin. Otherwise exact `https://` origins; never localhost in production. |
| ☐ | `AUTH_RATE_LIMIT_ENABLED` | no | `false` | no | `false` until every item in [REDIS_RATE_LIMIT_PREREQUISITES](REDIS_RATE_LIMIT_PREREQUISITES.md) passes and Maaz approves. |
| ☐ | `AUTH_RATE_LIMIT_CLIENT_IP_SOURCE` | when enabled | `unverified` | no | `unverified` now; `verified-asgi` only after the proxy test. |
| ☐ | `AUTH_RATE_LIMIT_REDIS_URL` | when enabled | empty | yes | unset now; later `rediss://` managed Redis. |
| ☐ | `AUTH_RATE_LIMIT_HMAC_KEY` | when enabled | empty | yes | unset now; later a separate ≥ 32-byte random key ≠ `SECRET_KEY`. |
| ☐ | `AUTH_RATE_LIMIT_*` limits, `_NAMESPACE`, `_TIMEOUT_SECONDS` | no | see ENVIRONMENT | no | defaults. |
| ☐ | `SECURITY_HEADERS_ENABLED` (PR #6) | no | `true` | no | unset/`true`. |
| ☐ | `HSTS_ENABLED` (PR #6) | no | `false` | no | `false` until [SECURITY_HEADERS_CHECKLIST](SECURITY_HEADERS_CHECKLIST.md) §4 is done and approved. |
| ☐ | `HSTS_MAX_AGE` (PR #6) | no | `31536000` | no | When HSTS is first enabled: `300`, then `86400`, then `31536000`. |
| ☐ | `READINESS_TIMEOUT_SECONDS` (PR #7) | no | `2` | no | `2`. |
| ☐ | No stray variables | — | — | — | Remove variables the code no longer reads; none of `TEST_*`, `POSTGRES_*`, `VITE_*` belong in production. |

## 2. Secrets hygiene

- ☐ No `.env` in the deployed bundle (verify the CLI upload preview; `.fastapicloudignore` excludes it).
- ☐ Production `SECRET_KEY`, DB password and (later) Redis/HMAC keys are not reused in any other environment, including preview/staging branches. A preview pointed at a Neon branch gets its own `SECRET_KEY`.
- ☐ Owners of the FastAPI Cloud team, Neon project and GitHub repo use 2FA.
- ☐ Rotation procedure known: [AST-002 plan §6](../security/AST-002_REMEDIATION_PLAN.md).

## 3. `/docs`, `/redoc`, `/openapi.json`

Current: public (FastAPI defaults; no `docs_url` override in `main.py`). The audit
rates this Low (S9): the schema reveals nothing that the open-source repo does not.

Decision for Maaz (not implemented):

| Option | When | Change |
|---|---|---|
| A. Keep public (recommended **for the MVP/portfolio phase**) | No real users yet; repo is public anyway | none. PR #6 already gives `/docs` and `/redoc` their own narrower CSP. |
| B. Disable in production | Before onboarding real passengers/drivers | add a `DOCS_ENABLED` flag (default `false` in prod) passing `docs_url=None, redoc_url=None, openapi_url=None` to `FastAPI(...)`; update smoke tests and PR #6 CSP tests. |

Whichever is chosen, `/openapi.json` must never include admin-only or debug routes
that are not otherwise protected.

## 4. CORS

- `main.py`: `allow_credentials=False`, methods `GET, POST, PATCH`, headers `Authorization, Content-Type`, exposes `Retry-After`.
- ☐ Production SPA is served from the same origin, so no CORS origin is needed. Keep `FRONTEND_ORIGINS` empty unless a separate frontend origin exists.
- ☐ Never add `*`, `null`, `http://` origins, or preview/localhost origins in production.
- ☐ If cookies are ever introduced (see §5), `allow_credentials=True` must be paired with an exact-origin list and CSRF protection.

## 5. Cookies and HSTS prerequisites

- Today the app sets **no cookies**: JWTs live in frontend memory and are sent as `Authorization: Bearer`. A reload signs the user out (M2 "session persistence" may change this).
- If M2 introduces a cookie session: `Secure`, `HttpOnly`, `SameSite=Lax` (or `Strict`), `Path=/`, no `Domain` attribute (host-only, so it is not sent to other `*.fastapicloud.dev` apps), CSRF token or double-submit for `POST/PATCH`, and short lifetime with rotation. Do **not** store tokens in `localStorage` without a CSP review.
- HSTS prerequisites (details in [SECURITY_HEADERS_CHECKLIST](SECURITY_HEADERS_CHECKLIST.md)):
  - the whole `.dev` TLD is on browsers' HSTS preload list, so `rideshare-api.fastapicloud.dev` is already HTTPS-only in modern browsers; the header matters mostly for a future custom domain;
  - the middleware always sends `includeSubDomains`; on a custom apex domain that would force HTTPS on every subdomain;
  - never submit to the preload list from this app.

## 6. Platform settings to confirm (read-only, in dashboards)

- ☐ FastAPI Cloud: which repo/branch the app deploys from, whether auto-deploy is on (DEPLOYMENT.md says keep it off until verified), health-check path (move to `/healthz` after PR #7; `/test-db` is removed by PR #7), number of instances.
- ☐ Neon: plan, **history window** (Free = 6 h max), compute autosuspend (cold-start adds latency to the first request and to `/readyz`), region, which roles exist, IP allow-list if available.
- ☐ GitHub: default branch (still `main`, see AST-002), branch protection on `rideshare-v2`, required checks (needs Maaz), secret scanning and push protection enabled.
