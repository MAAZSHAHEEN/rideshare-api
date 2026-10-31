# RideShare RC launch blockers and staging prerequisites

Prepared 31 Oct 2026 (PKT). Source of each item: `docs/release/SECURITY_REVIEW_RC.md` (S-numbers),
`docs/release/BACKGROUND_JOBS.md`, `handover/MERGE_READINESS_FINAL.md`, PR bodies #18–#24.
"Launch" = production with real users. Staging (a FastAPI Cloud preview + Neon branch with fake or
copied data) is *not* blocked by these, except where the staging checklist says so.

Owners: **Maaz** (decisions, dashboards, secrets, production, merges), **Hark** (docs, PRs,
rehearsals on disposable DBs, preview checks once approved), **Codex Astra** (independent review).

## 1. Unresolved launch blockers

| ID | Blocker | Root cause | Fix | Effort | Owner | Approval needed |
|---|---|---|---|---|---|---|
| LB-1 | **RC not merged** — 23 draft PRs, `rideshare-v2` still `e8da425` | Stacked PR sprint; merge is Maaz's gate | Merge in MERGE_READINESS_FINAL order (#2,#3,#4,#6,#11 → #5 → #7,#8,#10,#9 → #12,#15,#13 → #14,#16 → #17 → #18 → #20 → #22 → #19 → #21 → #23 → #24), applying C1–C7 | M (½ d Maaz + CI) | Maaz (Hark prepares) | Merge approval |
| LB-2 | **Historical secrets / stale default branch (AST-002)** | `.env` + copies committed Feb–Mar 2026; `main` still default and still carries them | Read-only confirmations → rotate `SECRET_KEY` at the RC deploy → (conditional) Neon role rotation → default branch to `rideshare-v2` after checking FastAPI Cloud auto-deploy → secret scanning + push protection | S (1–2 h) | Maaz | Secret rotation, default-branch change (AST-002 gate), GitHub settings |
| LB-3 | **Auth rate limiting off** | Client-IP boundary behind Cloudflare/FastAPI Cloud unverified; no Redis provisioned | Preview client-IP test → provision Redis (TLS, ACL) → set 4 env vars → V1–V8 → decide fail-closed/open for login | M (½–1 d) | Maaz (Redis, env), Hark (test plan) | Preview deploy, Redis provisioning, env change, **enabling rate limiting** |
| LB-4 | **Backups unverified** | Neon plan / PITR window unknown (Free = 6 h); no logical dump; no drill | Confirm window ≥ 24 h or add daily encrypted `pg_dump`; run restore drill D1–D9 once | S (1–2 h) | Maaz | Neon settings / plan; drill on a branch |
| LB-5 | **Production migration chain not rehearsed on real data shape** | 8 revisions, two ordering traps (#5 code-vs-index; #18/#20/#22/#19/#21 migrate-first) and two lossy downgrades (#19 suspended state, #21 audit history) | Neon branch of prod: preflight → `upgrade head` → smoke → reverse-order downgrade with exports → re-upgrade (SECURITY_REVIEW_RC §10, STAGING_MIGRATION_PLAN S1–S12 extended to `d5e1a0b00002`) | M (½ d) | Hark (runbook), Maaz (run/approve) | Neon branch, preview deploy, prod read-only preflight |
| LB-6 | **Email case-twin preflight not run** | `prod_email_preflight.sql` approved but needs Maaz's DB access | Maaz runs it; `case_duplicate_groups` must be 0 before `f6b2` | S (10 min) | Maaz | Already approved (read-only) |
| LB-7 | **Starlette 1.x / PyJWT untested on FastAPI Cloud** | #10 major upgrade only exercised locally/CI | Staging smoke list in SECURITY_REVIEW_RC §8 on the preview | S (2 h) | Hark (run), Maaz (approve preview) | Preview deploy |
| LB-8 | **No monitoring/alerting; `/test-db` public** | Probes from #7 not deployed; no uptime monitor; errors only visible in FastAPI Cloud logs | Deploy #7/#15; external monitor on `/readyz` (1 min, alert after 2 fails) + 5xx/`rideshare.errors` alert to Maaz; move any monitor off `/test-db` | S | Maaz (monitor), Hark (config doc) | Monitor account (vendor = Maaz's choice) |
| LB-9 | **Admin process undefined (D1) and no admin hardening (D8)** — only if `/admin` is usable in prod | No grant script; manual SQL would be unaudited; admin tokens 60 min, no 2FA; admin-on-admin suspension allowed | Either: launch with **zero admin accounts** (endpoints then always 403) — or: D1 decision + `scripts/grant_admin.py` with audit row + 15-min admin tokens; TOTP before broader use | S (no admins) / M (hardened) | Maaz (D1, D8), Hark (script + token PR) | Product decision; prod write to grant an admin |
| LB-10 | **CNIC/phone plaintext at rest** | Encryption designed (PR #8 doc §4–§7), not built | Either written acceptance for MVP with compensating controls (SECURITY_REVIEW_RC §1) — or build expand/dual-write/backfill/switch | S (accept) / L (build, 5–8 d) | Maaz (decide), Hark (build), Codex Astra (review) | Product/legal decision; key creation (env/secret change) |
| LB-11 | **Open product decisions that change behaviour at launch** | D5 auto-cancel on suspension (visible to passengers), D6 suspended-login message, D7 audit retention, seats cap (`BOOKING_MAX_SEATS` start at 1 or 8), pending TTL (360 min) and auto-close (24 h) defaults | Maaz confirms defaults or sets env values | S | Maaz | Decisions; env change if not default |
| LB-12 | **Background jobs not scheduled** (soft blocker) | No platform scheduler (BACKGROUND_JOBS §2); GH Actions schedules run only on the default branch | After LB-2 default branch: dedicated `rideshare_jobs` role, direct-host secret, workflow from BACKGROUND_JOBS §2.1, heartbeat. *Correctness does not depend on it* — launch can proceed with manual weekly runs if Maaz accepts | S (1–2 h) | Maaz (secret, approve), Hark (workflow PR) | Prod DB credential in GitHub; scheduling |
| LB-13 | **Required CI checks not enforced** | Branch protection unchanged | Require `test`, `frontend`, `e2e`, `dependency-audit` on `rideshare-v2` (and default branch) | S (5 min) | Maaz | Repo settings |

Not launch blockers (tracked): audit-log role split (S5 — blocker only if claiming tamper-evidence),
HSTS (gated, `.dev` already preloaded), JWT in `sessionStorage` / no server logout (S3), password reset
(AST-012), register 409 enumeration (S13), transitive dependency lock (L-1), `/docs` public (decision),
CSP report endpoint.

## 2. Staging deployment prerequisites checklist

Staging = FastAPI Cloud **preview** app pointed at a **Neon branch** (from production, or a fresh
database with fake data). Every item marked ⛔ needs Maaz's explicit approval before it is done.

### 2.1 Code and CI
- [ ] RC integration branch built from all 23 PRs in merge order (agent integ-rc, `integration/rc-local`); backend suite, Vitest, typecheck, build, Playwright green on it.
- [ ] `alembic heads` = single head `d5e1a0b00002`; `alembic check` clean.
- [ ] Optional: `feat/job-advisory-lock` (BACKGROUND_JOBS §8) included or explicitly deferred.
- [ ] ⛔ Decide whether staging deploys from a merged `rideshare-v2` (needs LB-1) or from the integration branch (needs it pushed — currently local only).

### 2.2 Data
- [ ] ⛔ Neon branch created (`staging-rc-<date>`), its own connection string, **direct** host for migrations/jobs, pooled host allowed for the app.
- [ ] ⛔ Preflight on the branch: `prod_email_preflight.sql` (`case_duplicate_groups = 0`), CNIC/phone shape counts (PR #8 doc §3, counts only).
- [ ] If the branch copies production: treat it as production data (real CNIC/phone) — restricted access, delete after (S12). Prefer fake data for UI testing.
- [ ] `alembic current` = `d924a6e38f10` (record actual value).

### 2.3 Configuration (preview env vars; values never pasted in chat/docs)
- [ ] ⛔ `DATABASE_URL` → Neon branch; `SECRET_KEY` → **new** random ≥ 32 bytes, different from production.
- [ ] `FRONTEND_ORIGINS` = the preview origin only (or empty if same-origin).
- [ ] `SECURITY_HEADERS_ENABLED=true`, `HSTS_ENABLED=false`.
- [ ] `READINESS_TIMEOUT_SECONDS=2`; body-limit default from #15.
- [ ] `AUTH_RATE_LIMIT_ENABLED=false` for the first pass; second pass with Redis for LB-3 (⛔ Redis + enabling).
- [ ] `BOOKING_MAX_SEATS`, `BOOKING_PENDING_TTL_MINUTES`, `RIDE_AUTO_CLOSE_AFTER_HOURS`, `IDEMPOTENCY_KEY_TTL_HOURS` = the values Maaz picks for launch (LB-11).
- [ ] No admin account on staging unless testing `/admin` (then a fake one via the documented process).

### 2.4 Migrations (on the Neon branch)
- [ ] Off-peak not required on a branch, but check no long transactions (`pg_stat_activity` query in STAGING_MIGRATION_PLAN).
- [ ] `alembic upgrade head` → `d5e1a0b00002`; `indisvalid` true for all new indexes (`ix_rides_driver_id_departure_time_id`, `ix_bookings_passenger_id_id`, `ix_bookings_ride_id_id`, `uq_users_email_lower`, `ix_bookings_pending_expires_at`, `ix_idempotency_keys_expires_at`, audit indexes).
- [ ] Downgrade rehearsal in reverse with exports (audit CSV, suspended ids), stop at each revision, re-upgrade (LB-5).

### 2.5 Smoke on the preview (⛔ preview deploy)
- [ ] `/healthz` 200, `/readyz` 200 `database: ok`; `/test-db` 404.
- [ ] Headers on `/`, `/docs`, `/redoc`, `/healthz`, an API 401/422/413: CSP (app vs docs), nosniff, XFO DENY, Referrer-Policy, Permissions-Policy, COOP, `X-Request-ID`; no HSTS.
- [ ] Browser: SPA, `/login`, `/register`, `/docs`, `/redoc` with 0 CSP violations (the `/docs` check the sandbox could not do).
- [ ] Starlette 1.x checks (SECURITY_REVIEW_RC §8): deep links, `/assets` caching, `/rides` 307 `Location`, jose-era token accepted.
- [ ] Journeys with fake users: register (12-char password), login, case-variant login, publish ride, request 1 and 2 seats, accept, `Not enough seats available`, cancel restores N seats, idempotent replay (`Idempotent-Replayed: true`), pending expiry 409, complete-after-departure rule.
- [ ] Jobs: run `python -m jobs.expire_bookings` and `jobs.purge_idempotency_keys` once by hand against the branch; run two copies at once → one prints `job_lock=skipped` (if #job-advisory-lock is included).
- [ ] Logs: send an oversized body (413) and an invalid registration (422) and confirm neither response echoes input and the FastAPI Cloud log shows no CNIC, phone, password or token.

### 2.6 Exit criteria for staging → production
- [ ] All ⛔ items approved and done; LB-1 … LB-11 closed or formally accepted in writing by Maaz.
- [ ] Production runbook written with the chosen migration option (SECURITY_REVIEW_RC §10 Option A or B), restore point, rollback order, and who watches the monitors for the first hour.
- [ ] Neon staging branch deleted; preview app torn down or left with fake data only.
