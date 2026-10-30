# Redis auth rate limiting: prerequisites for enabling in production

Code: `auth_rate_limit.py` (on `rideshare-v2`). Production state: **disabled**
(`AUTH_RATE_LIMIT_ENABLED=false`) by Maaz's decision. Enabling it, provisioning
Redis for production and setting the variables are approval gates. Until enabled,
`/auth/login` and `/auth/register` accept unlimited attempts (audit S3, High).

## 1. How the limiter decides who a client is

- Middleware `AuthRateLimitMiddleware` throttles only `POST /auth/login` and `POST /auth/register` (coarse per-IP buckets, before body parsing); `check_login_identity` adds a per-(IP, normalised email) bucket before the SQL lookup and bcrypt.
- The client is **`scope["client"][0]` only**. The app deliberately never parses `X-Forwarded-For`, `CF-Connecting-IP` or similar headers.
- `scope["client"]` is set by Uvicorn. Uvicorn rewrites it from `X-Forwarded-For` only when the TCP peer is in `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS` (default `127.0.0.1`).
- Production traffic passes Cloudflare (`server: cloudflare`, `cf-ray` observed) and FastAPI Cloud's ingress before reaching Uvicorn. How the platform starts Uvicorn and which peers it trusts is **not documented in this repo and not verified**.

Failure modes if enabled blind:

| What `scope["client"]` turns out to be | Effect |
|---|---|
| The platform proxy's IP (same for everyone) | 30 logins/min and 5 registrations/10 min **for the whole world**: self-inflicted outage |
| A value taken from a client-supplied `X-Forwarded-For` | Attacker rotates the header → limits bypassed |
| The real client IP, set by a trusted hop | Correct |

That is why the code refuses to start with `enabled=true` unless
`AUTH_RATE_LIMIT_CLIENT_IP_SOURCE=verified-asgi` is set as an explicit acknowledgement.

## 2. Verify the client-IP boundary (preview only, approval for the preview deploy)

1. Build a **temporary, preview-only** diagnostic (separate branch, never merged, never deployed to production): a `GET /__diag/client` route, enabled only when a `DIAG_CLIENT_IP=true` variable is set, returning `scope["client"][0]` and **whether** `x-forwarded-for` / `cf-connecting-ip` headers were present (not their values beyond the first hop).
2. From two different networks (e.g. home broadband and mobile data), call it and record the reported client. Pass: each call reports that network's public IP.
3. Spoof test: `curl -H 'X-Forwarded-For: 203.0.113.7' …/__diag/client`. Pass: the reported client is **not** `203.0.113.7`.
4. Repeat 2–3 over IPv6 if available.
5. Remove the diagnostic and delete the preview.
6. Optionally ask FastAPI Cloud support to confirm in writing how `X-Forwarded-For` is sanitised and which `--forwarded-allow-ips` the runtime uses.

Outcomes:

- **Pass** → `AUTH_RATE_LIMIT_CLIENT_IP_SOURCE=verified-asgi` is justified.
- **Proxy IP** → do not enable. Options: platform-side trusted-proxy configuration if FastAPI Cloud offers it; or a reviewed code change that trusts exactly one header (e.g. the right-most untrusted `X-Forwarded-For` hop) only from documented platform CIDRs, with tests.
- **Spoofable** → do not enable; report to the platform.

## 3. Redis provisioning

- Managed Redis with TLS (`rediss://`), in or near the FastAPI Cloud region (each auth request makes up to two round-trips under a 0.5 s budget).
- A dedicated database/user for this app. If ACLs are supported, allow only what the code uses: `EVAL`/`EVALSHA` (script runs `GET`, `PTTL`, `SET … PX`, `PEXPIRE`, `INCR`) and `PING` (`/readyz`, PR #7).
- Eviction: all keys have TTLs. Prefer `volatile-ttl`/`volatile-lru` with enough memory; with `noeviction`, a full instance makes logins fail closed (503).
- Free tiers with daily command quotas: exhaustion = login/registration outage while enabled. Size for peak: about 2 commands per login, 1 per registration.
- Persistence is not required (counters are short-lived); a failover that resets counters is acceptable.
- Separate `AUTH_RATE_LIMIT_HMAC_KEY` (≥ 32 random bytes, ≠ `SECRET_KEY`). All instances must share Redis, namespace, key and limits.

## 4. Configuration to apply (when approved)

```
AUTH_RATE_LIMIT_ENABLED=true
AUTH_RATE_LIMIT_CLIENT_IP_SOURCE=verified-asgi
AUTH_RATE_LIMIT_REDIS_URL=rediss://…        # secret
AUTH_RATE_LIMIT_HMAC_KEY=…                  # secret, separate
# limits/namespace/timeout: keep defaults (30/60s login per IP, 5/600s register per IP,
# 5/60s per IP+email, 0.5 s timeout)
```

## 5. Verification on the preview (then repeat lightly on production)

| # | Check | Expected |
|---|---|---|
| V1 | App starts (config validation passes) | no `RuntimeError` |
| V2 | `GET /readyz` (PR #7) | `{"status":"ready","checks":{"database":"ok","redis":"ok"}}` |
| V3 | 6 wrong-password logins for one **test** email within 60 s | 6th → `429`, `Retry-After`, `Cache-Control: no-store` |
| V4 | Another test email from the same IP | still allowed (until the 30/min IP bucket) |
| V5 | 6 registrations from one IP within 10 min (preview only; throwaway fake data) | 6th → `429` |
| V6 | Same steps from a second network | independent counters |
| V7 | Redis unreachable (preview: point URL at a closed port) | login/register → `503 "Authentication temporarily unavailable"`; other routes OK; `/readyz` → 503 |
| V8 | Redis keys | names are `rideshare:auth:v1:<bucket>:<hex>`; no raw IPs or emails; TTLs set |

On production, only V1, V2 and a single V3 run with a dedicated test account.

## 6. Operational consequences to accept

- Fail-closed: a Redis outage blocks all logins and registrations (V7). Alert on limiter 503s.
- If the platform gates traffic on `/readyz`, a Redis outage also marks instances not-ready (Redis becomes a readiness dependency when enabled). Decide whether that is wanted before pointing a traffic gate at `/readyz`.
- Known design gaps (audit S3), to schedule separately: no per-email bucket independent of IP (distributed guessing on one account); fixed windows allow boundary bursts; only `/auth/*` is throttled.

## 7. Rollback

`AUTH_RATE_LIMIT_ENABLED=false` + restart/redeploy. Counters in Redis expire on
their own; nothing to clean up. Changing `AUTH_RATE_LIMIT_NAMESPACE` resets all
counters without disabling the limiter.
