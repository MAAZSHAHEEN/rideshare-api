# Environment variables

Every variable the code reads, taken from the source (`config.py`, `database.py`,
`auth_rate_limit.py`, `alembic/env.py`, `Dockerfile`, `docker-compose.yml`, the
frontend client and the tests) on `rideshare-v2` plus the open Wave 1 PRs. Rows
marked **(PR #n)** only exist once that PR is merged.

`config.py` and `database.py` call `load_dotenv()`, so a `.env` file in the working
directory is read in addition to the process environment (process values win).
Production must not ship a `.env`: `.fastapicloudignore`, `.dockerignore` and
`.gitignore` all exclude it. Configuration errors stop the process at import time
with a `RuntimeError` that names the variable but never echoes its value.

## Application runtime

| Variable | Read by | Purpose | Required | Default | Secret | Validation |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | `database.py`, `alembic/env.py` | SQLAlchemy async URL for PostgreSQL (`postgresql+asyncpg://…`). Also the database Alembic migrates. | Yes | none (startup `SELECT 1` fails) | **Yes** (contains DB password) | none in code; CI checks the driver. Neon requires TLS: with asyncpg use `?ssl=require` (libpq's `sslmode=` is not an asyncpg argument). |
| `SECRET_KEY` | `config.py` | HMAC key that signs and verifies JWT access tokens. | Yes | none | **Yes** | ≥ 32 / 48 / 64 UTF-8 bytes for HS256 / HS384 / HS512; rejects `your-secret…`, `replace-with…`, `change-me…`, `changeme…` prefixes. |
| `ALGORITHM` | `config.py` | JWT algorithm. | No | `HS256` | No | `HS256`, `HS384` or `HS512` only. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `config.py` | Access-token lifetime. There is no refresh token or revocation, so this is the maximum time a stolen token stays valid. | No | `60` | No | positive integer. |
| `FRONTEND_ORIGINS` | `config.py` | Comma-separated exact origins allowed by CORS. Empty disables cross-origin access. Same-origin production (SPA served by the API) needs none. | No | empty | No | `http(s)://host[:port]` only; no path, wildcard, credentials or whitespace. |
| `AUTH_RATE_LIMIT_ENABLED` | `auth_rate_limit.py` | Turns on Redis-backed throttling of `POST /auth/login` and `POST /auth/register`. **Off in production** (see [REDIS_RATE_LIMIT_PREREQUISITES](operations/REDIS_RATE_LIMIT_PREREQUISITES.md)). | No | `false` | No | exactly `true` or `false`. |
| `AUTH_RATE_LIMIT_CLIENT_IP_SOURCE` | `auth_rate_limit.py` | Operator acknowledgement that the ASGI client address is the real, unspoofable client IP. | When enabled | `unverified` | No | must be `verified-asgi` to enable. |
| `AUTH_RATE_LIMIT_REDIS_URL` | `auth_rate_limit.py` | Redis for the counters. | When enabled | empty | **Yes** (usually contains a password) | `rediss://` (TLS), or plain `redis://` on loopback only; no query/fragment. |
| `AUTH_RATE_LIMIT_HMAC_KEY` | `auth_rate_limit.py` | Keys the HMAC that turns IPs/emails into Redis key names. | When enabled | empty | **Yes** | ≥ 32 bytes, non-placeholder, **must differ from `SECRET_KEY`**. |
| `AUTH_RATE_LIMIT_NAMESPACE` | `auth_rate_limit.py` | Redis key prefix. Changing it resets all counters. | No | `rideshare:auth:v1` | No | `[A-Za-z0-9:_-]{1,80}`. |
| `AUTH_RATE_LIMIT_LOGIN_LIMIT` / `_LOGIN_WINDOW_SECONDS` | `auth_rate_limit.py` | Login requests per client IP per window. | No | `30` / `60` | No | integer 1–86400. |
| `AUTH_RATE_LIMIT_REGISTER_LIMIT` / `_REGISTER_WINDOW_SECONDS` | `auth_rate_limit.py` | Registrations per client IP per window. | No | `5` / `600` | No | integer 1–86400. |
| `AUTH_RATE_LIMIT_LOGIN_IDENTITY_LIMIT` / `_LOGIN_IDENTITY_WINDOW_SECONDS` | `auth_rate_limit.py` | Login attempts per (IP, normalised email) per window. | No | `5` / `60` | No | integer 1–86400. |
| `AUTH_RATE_LIMIT_TIMEOUT_SECONDS` | `auth_rate_limit.py` | Total deadline per Redis call; on timeout the request fails closed with 503. | No | `0.5` | No | `0 < x ≤ 5`. |
| `SECURITY_HEADERS_ENABLED` **(PR #6)** | `security_headers.py` | Kill switch for the CSP / X-Frame-Options / Referrer-Policy / Permissions-Policy / COOP middleware. | No | `true` | No | `1/true/yes/on` or `0/false/no/off`; empty = default. |
| `HSTS_ENABLED` **(PR #6)** | `security_headers.py` | Adds `Strict-Transport-Security: max-age=<HSTS_MAX_AGE>; includeSubDomains`. Approval gate. | No | `false` | No | as above. |
| `HSTS_MAX_AGE` **(PR #6)** | `security_headers.py` | HSTS lifetime in seconds. | No | `31536000` | No | non-negative integer. |
| `READINESS_TIMEOUT_SECONDS` **(PR #7)** | `health.py` | Deadline for each `/readyz` dependency check. | No | `2` | No | `0 < x ≤ 10`. |

## Process / platform (not read by application code)

| Variable | Used by | Notes |
|---|---|---|
| `PORT` | `Dockerfile` `CMD` | Uvicorn port in the container image; default `8000`. FastAPI Cloud uses its own start command. |
| `FORWARDED_ALLOW_IPS` | Uvicorn | Which peer IPs may set `X-Forwarded-For`/`-Proto`. Default `127.0.0.1`. Decides what `scope["client"]` is, and therefore what the rate limiter keys on. Not set by this repo; on FastAPI Cloud it is platform-controlled (unverified). |
| `FASTAPICLOUD_DEPLOYMENT_ID`, `FASTAPI_PUBLIC_URL` | FastAPI Cloud | Set by the platform at runtime ([docs](https://fastapicloud.com/docs/builds-and-deployments/environment-variables/)). Useful as a log field; not read today. |

## Development, CI and tests only

| Variable | Used by | Notes |
|---|---|---|
| `TEST_DATABASE_URL` | tests | Disposable PostgreSQL; integration tests skip without it. Migration tests need `CREATEDB`. Never production. |
| `TEST_REDIS_URL` | tests | Disposable loopback Redis, e.g. `redis://127.0.0.1:6379/0`. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `docker-compose.yml` | Initialise a **new** local volume only. `POSTGRES_PASSWORD` is required by Compose. |
| `VITE_API_BASE_URL` | `frontend/src/api/client.ts` | Vite dev server only. Production builds force same-origin (`''`); never put secrets in `VITE_*`, they are compiled into public JS. |

CI (`.github/workflows/ci.yml`) supplies disposable values for `DATABASE_URL`,
`TEST_DATABASE_URL`, `SECRET_KEY`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`,
`AUTH_RATE_LIMIT_ENABLED=false` and `TEST_REDIS_URL`; it uses no repository secrets.

## Generating secrets

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY, AUTH_RATE_LIMIT_HMAC_KEY (separate runs)
```

Store them only in the platform's environment settings (FastAPI Cloud dashboard
or CLI) and a password manager. Never in the repo, an issue, a PR, a log or a chat.

See also: [PRODUCTION_CONFIG_CHECKLIST](operations/PRODUCTION_CONFIG_CHECKLIST.md).
