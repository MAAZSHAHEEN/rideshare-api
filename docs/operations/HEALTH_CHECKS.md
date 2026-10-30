# Health checks: `/healthz` and `/readyz`

Introduced by AST-010. Replaces the old `/test-db` diagnostic, which is
**removed** and now returns `404`.

| Endpoint | Purpose | Touches | Methods | Auth | Rate limited |
|---|---|---|---|---|---|
| `/healthz` | Liveness: "the process is up and serving HTTP" | nothing | GET, HEAD | no | no |
| `/readyz` | Readiness: "this instance can serve real traffic now" | PostgreSQL (`SELECT 1`); Redis only when `AUTH_RATE_LIMIT_ENABLED=true` | GET, HEAD | no | no |

Both responses carry `Cache-Control: no-store` and are hidden from the OpenAPI
schema (`/docs`). Any other method returns `405`.

## `/healthz` (liveness)

Always `200 {"status":"ok"}` while the event loop can answer HTTP. It performs
no I/O, so it stays `200` during a database or Redis outage. That is deliberate:
restarting an app container does not fix a database outage, and a liveness
probe that checks the DB turns one outage into a restart storm.

## `/readyz` (readiness)

Runs the checks concurrently, each under a hard deadline of
`READINESS_TIMEOUT_SECONDS` (default `2`, allowed `(0, 10]`, invalid values stop
startup). The deadline covers pool checkout, connect, TLS/auth and the query,
so a hung database or an exhausted pool yields `503`, not a hanging request.
At most two probes use database connections at once; extra concurrent probes
wait for a slot inside the same deadline. A probe flood therefore never takes
more than two connections from real traffic. The check uses the application's
own engine and pool, so "ready" means the pool real requests use works.

| Situation | Status | Body |
|---|---|---|
| DB ok, rate limiting off | 200 | `{"status":"ready","checks":{"database":"ok","redis":"disabled"}}` |
| DB ok, rate limiting on, Redis ok | 200 | `{"status":"ready","checks":{"database":"ok","redis":"ok"}}` |
| DB down, slow (> deadline) or pool exhausted | 503 | `{"status":"not_ready","checks":{"database":"unavailable","redis":"disabled"}}` |
| Rate limiting on, Redis down or slow | 503 | `{"status":"not_ready","checks":{"database":"ok","redis":"unavailable"}}` |

Values are a fixed set (`ok`, `unavailable`, `disabled`). The body never
contains exception text, DSNs, hostnames, ports, usernames or secrets; failure
details are intentionally dropped (see "Limitations").

Redis counts only when rate limiting is enabled because login and register then
fail closed (503) without it. While rate limiting is off (the current production
setting), Redis being unreachable doesn't affect readiness.

## How to use them

**FastAPI Cloud.** The platform's own health-check and restart settings were not
verified (see DEPLOYMENT.md), so nothing here has been configured there. When
the operator configures it: point a liveness/restart check at `/healthz` and a
readiness/traffic-gating check, if one is offered, at `/readyz`. Don't point a
restart check at `/readyz`.

**Load balancer / Kubernetes-style orchestrator.**
- Liveness: `GET /healthz`, period 10 s, timeout 2 s, failure threshold 3.
- Readiness: `GET /readyz`, period 10 s, timeout 3 s (more than
  `READINESS_TIMEOUT_SECONDS`), failure threshold 2, success threshold 1.
- Startup: today the app's lifespan runs `SELECT 1` *before* serving, with no
  deadline. If the DB is unreachable at boot the process doesn't start, so
  `/healthz` is unreachable too. Give startup probes a generous window (e.g.
  60 s) and treat a boot failure as a DB/config problem.

**External uptime monitor** (UptimeRobot, Better Stack, etc.).
- Monitor A, "app up": `GET https://<host>/healthz`, expect `200` and keyword `ok`, every 1 min.
- Monitor B, "app can serve": `GET https://<host>/readyz`, expect `200`, every 1 min, timeout 5 s.
- Use GET (or HEAD if the monitor needs it; both are supported).

## Alerting suggestions

- **Page:** `/healthz` fails 3 consecutive checks (about 3 min): process down, crash loop or platform/DNS/TLS problem.
- **Page:** `/readyz` returns 503 for 5+ minutes while `/healthz` is 200: database (or, with rate limiting on, Redis) unreachable or saturated. Check Neon status, connection limits and recent deploys/migrations.
- **Ticket/warn:** intermittent `/readyz` 503 (for example, more than 3 in 1 h): pool saturation or slow DB; check for long queries and pool size.
- **Warn:** `/readyz` latency p95 above 1 s.
- Read the `checks` field to tell which dependency failed.

## Operational notes

- Probes are exempt from auth and from the auth rate limiter (it only matches
  `POST /auth/login` and `POST /auth/register`).
- Security headers (AST-006, PR #6, not yet merged) are added by an outer ASGI
  middleware and apply to these JSON responses too; nothing here conflicts. The
  probes set only `Cache-Control`, which that middleware doesn't touch.
- Each `/readyz` call opens or uses one pooled DB connection. A 10 s probe
  interval per instance is negligible; don't probe more often than every 5 s.
- Rollback: revert the AST-010 commit. No schema, data or env change is needed;
  `READINESS_TIMEOUT_SECONDS` is optional.

## Migration from `/test-db`

`/test-db` returned `{"status":"Database connected!"}` (and didn't actually run
a query). It is removed; anything that polled it (monitors, scripts) must switch
to `/readyz`. No frontend code or e2e test referenced it; the backend test that
did now checks the `404` and the `/readyz` 503 path.

## Limitations

- No failure details are logged: the probe swallows the exception to keep the
  body clean. Use the platform's DB metrics and app logs for root cause.
- Startup `SELECT 1` in the lifespan has no deadline (unchanged by AST-010).
- Whether `/docs` should be public in production (from the AST-010 spec) is
  not decided here. It's left for the production-config checklist (M6).
