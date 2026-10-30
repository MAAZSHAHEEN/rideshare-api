# Logging and monitoring design

Status: **design only**. Today the application has no logging of its own (audit O1:
no `logging` use outside `alembic/env.py`); Uvicorn's default access log and the
platform's log view are all there is. PR #8 adds `hide_parameters=True` to the
SQLAlchemy engine so bound values (CNIC, phone, password hash) no longer appear in
DB exception text. Implementing this design touches `main.py` (shared file) and
should be sequenced after PRs #6/#7/#8 merge.

## 1. What must never be logged

Applies to application logs, access logs, error trackers, traces, metrics labels,
CI output and incident notes.

| Never log | Where it could leak today / how to prevent |
|---|---|
| CNIC (any format) | Registration body; PostgreSQL `DETAIL: Key (cnic)=(…)` on unique violations; asyncpg `DataError` text. → never log request bodies; log only exception class, SQLSTATE and constraint name. |
| Phone numbers | Registration body; `Failing row contains (…)` details. Same rule. |
| Passwords and password hashes | Login/register bodies; row dumps. |
| JWTs / `Authorization` header | Request header dumps, error-tracker request context. → drop the header in middleware and in the tracker's `before_send`. |
| `SECRET_KEY`, `AUTH_RATE_LIMIT_HMAC_KEY` | Config dumps / `repr(settings)`. Never log settings objects. |
| `DATABASE_URL`, `AUTH_RATE_LIMIT_REDIS_URL` (contain passwords) | Startup "connecting to…" messages, exception text. Log host class at most. |
| Rate-limiter keys and raw client IP + email pairs | Limiter keys are already HMAC'd; do not log the inputs. |
| Full email addresses | Use `user_id` once authenticated. For failed logins, log an HMAC of the normalised email (same key family as the limiter, never the JWT key) if correlation is needed. |
| Raw request/response bodies, query strings with personal data | Ride search query strings (`origin`, `destination`) are not personal; keep path + status, not bodies. |

PII stays in the database; logs carry IDs.

## 2. Structured logging

- Python `logging`, one JSON object per line to stdout (FastAPI Cloud collects stdout). A small `logging.Formatter` subclass or `python-json-logger`; no `print`.
- Configure once in an app factory/`main.py`; set `uvicorn.access` to the same formatter, or disable it and emit our own access line from middleware (preferred, so headers can be filtered).
- Fields on every line:
  `ts` (UTC ISO-8601), `level`, `logger`, `msg`, `request_id`, `method`, `route` (template, e.g. `/rides/{ride_id}`, not the raw path), `status`, `duration_ms`, `user_id` (if authenticated), `deployment` (`FASTAPICLOUD_DEPLOYMENT_ID`), and for errors `exc_type`, `sqlstate`, `constraint`.
- Request ID middleware: accept an inbound `X-Request-ID` only if it matches `^[A-Za-z0-9-]{8,64}$`, else generate `uuid4`; store in a `contextvars.ContextVar`; return it in the response header so users can quote it in bug reports.
- Levels: `INFO` access lines and business events; `WARNING` 4xx that indicate abuse (401 bursts, 409 seat conflicts are normal → `INFO`), rate-limit 429/503; `ERROR` unhandled exceptions. No `DEBUG` in production.
- Generic 500 handler (open item from AST-007): catch unhandled exceptions, log `exc_type`, SQLSTATE and constraint only (no `str(exc)` for DB errors), return `{"detail":"Internal Server Error"}`.

Events worth logging (with IDs only): login success/failure (reason class), registration success/409, ride created/cancelled/completed, booking created/accepted/rejected/cancelled, 429 and 503 from the limiter, readiness failures.

## 3. Error monitoring options

| Option | Fit | Notes |
|---|---|---|
| **Sentry** (free developer tier) — recommended | Best signal for a solo developer: grouped exceptions, release tracking, alerts | `sentry-sdk[fastapi]`, `send_default_pii=False`, `before_send` that strips `request.headers.authorization`, `request.data`, cookies; `traces_sample_rate` ≤ 0.1. DSN is a config secret (new env var `SENTRY_DSN`, optional). |
| FastAPI Cloud built-in logs/metrics | Zero setup | Per FastAPI's docs, request count / error rate / latency metrics appear on Pro plans; check the dashboard. Logs only as good as what we emit. |
| OpenTelemetry → Grafana Cloud / Honeycomb | More setup; traces + logs + metrics | FastAPI supports `FASTAPI_OTEL_AUTO_CONFIGURE=true` with `OTEL_EXPORTER_OTLP_ENDPOINT`; apply the same scrubbing. |

## 4. Uptime and alerts

- External uptime monitor (UptimeRobot, Better Stack, etc.), 1–5 min interval, from outside Pakistan and inside if possible:
  - `GET /healthz` (after PR #7) → 200; alert after 2 consecutive failures.
  - `GET /readyz` (after PR #7) → 200; alert after 3 failures (DB/Redis).
  - Until PR #7 is deployed: `GET /test-db` → 200, and `GET /` → 200.
- Alerts (email/push to Maaz):
  - 5xx rate > 2 % over 5 min, or any burst of 10+ 5xx;
  - p95 latency > 2 s over 10 min (Neon cold start shows here);
  - `/readyz` 503;
  - limiter 503s (Redis down) once rate limiting is enabled;
  - spike in 401s on `/auth/login` (credential stuffing; there is no throttling today).
- Neon: enable usage/compute alerts in the console; Free plan limits can stop the database.

## 5. Retention and access

- Keep app logs 14–30 days; error-tracker events 30–90 days.
- Only Maaz (and named collaborators) can read logs. Logs are not shared in public issues; paste request IDs instead.
