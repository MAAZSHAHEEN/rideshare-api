# Neon staging plan for the RC rehearsal — synthetic data first

Prepared 31 Oct 2026 (staging-ops), **rewritten 31 Oct 2026 (a1-docs)** after Maaz's rule: *no
production-linked Neon branch without his separate approval.* **Plan only.** Creating a Neon project or
branch, connecting to it, and creating a FastAPI Cloud preview are approval-gated actions for Maaz. Neon
facts come from Neon docs read on 31 Oct 2026 (URLs inline). The production project's plan, region,
roles and history window are **unknown** to Hark.

## 0. Summary

| Phase | Data | Where | Approval | Proves |
|---|---|---|---|---|
| **1 (default, required)** | **synthetic only** — schema from `alembic upgrade`, rows from [`tools/seed_synthetic.py`](tools/seed_synthetic.py) | **1a** local Docker/PostgreSQL (Hark, no approval) → **1b** a **new standalone Neon project** `rideshare-staging-synth` (not branched from production; separate root branch, separate roles, separate connection string) | 1a none; 1b **A2** (Maaz creates the project) | migration mechanics d924 → head incl. the f6b2 abort path, smoke (STAGING_SMOKE_CHECKLIST), jobs, PyJWT, CORS, preview deploy, restore drill, forward-recovery scripts |
| **2 (optional, separately approved)** | production-derived, sanitized | Neon branch of production, operator-only until sanitized | **A2-P** (new, separate) — only if phase 1 left a question that needs the real data shape | real lock timings, the real preflight result on the branch, the real row counts |

Phase 2 is **not** needed to release: A1 already gives the real revision, schema fingerprint, counts and
preflight result read-only; phase 1 reproduces that shape synthetically (`seed_synthetic.py --target d924
--email-anomalies`, row counts scaled to A1's numbers with `--users`).

## 1. Why a standalone project, not a branch

| Option | Isolation from production | Verdict |
|---|---|---|
| **New Neon project** (phase 1b) | Separate root branch, compute, roles, passwords and hostnames; no copy-on-write link to production data; deleting it cannot touch production. Free plan allows 100 projects, 1 GB storage/project, 100 CU-hours/project/month ([plans](https://neon.com/docs/introduction/plans)) | **Use** |
| Schema-only branch of production | "replicate only the database schema … without copying any of the actual data" ([schema-only branches](https://neon.com/docs/guides/branching-schema-only)) — but it is still created *from* the production branch, inside the production project, visible to anyone with project access | Not without approval (production-linked) |
| Data branch of production (+ sanitize) | copy-on-write clone; real PII present until sanitized; branch history keeps pre-sanitization rows | Phase 2 only, approval A2-P |
| Local Docker (phase 1a) | complete; never leaves the sandbox | **Use** first (already done by Hark: §6) |

## 2. Phase 1a — local (Hark, no approval)

```bash
docker run -d --name rs-synth -e POSTGRES_PASSWORD=<local-only> -p 5432:5432 postgres:17   # or any local PG
createdb rs_synth_d924 && createdb rs_synth_head
DATABASE_URL=postgresql+asyncpg://…/rs_synth_d924 alembic upgrade d924a6e38f10
DATABASE_URL=postgresql+asyncpg://…/rs_synth_head alembic upgrade head
python3 docs/release/tools/seed_synthetic.py --target d924 --users 400 --email-anomalies > d924.sql
python3 docs/release/tools/seed_synthetic.py --target head --users 400 > head.sql
psql …/rs_synth_d924 -X -v ON_ERROR_STOP=1 -f d924.sql; psql …/rs_synth_head -X -v ON_ERROR_STOP=1 -f head.sql
```

The seed refuses any database whose name does not start with `rs_`, `synthetic` or `staging_synth`, and
refuses a non-empty `users` table, so it cannot be pointed at production by mistake (production is
`neondb`) and never mixes with real rows. Same `--seed` → byte-identical SQL.

What the generator produces (all obviously synthetic):

| Data | Values |
|---|---|
| Users | `Synthetic Driver 0007` / `Synthetic Passenger 0123`, emails `synthetic.<role>.<n>@example.com` (RFC 2606 reserved), CNIC stored as 13 digits starting `00000` (no real district code starts with 0), phones `+92300 00nnnnn`; one public test password `Synthetic-Only-Pass-1` (bcrypt cost 12) so smoke tests can log in; 1 admin; ~20 % drivers |
| Rides | Lahore ↔ Islamabad mostly, plus Rawalpindi, Faisalabad, Multan, Gujranwala; bare city names (search matches whole names) with ~15 % carrying a landmark; fares PKR 2,500–4,500 Lahore↔Islamabad; departures −14 d … +21 d around load time; past → completed/cancelled/some still `active` (stale-closure work), future → active/cancelled; `available_seats` = capacity − accepted seats |
| Bookings | pending / accepted / rejected / cancelled; at head also `expired`, multi-seat 1–4, `expires_at` NULL (legacy) or TTL-based, some already past (expiry-sweep work); one live booking per passenger/ride |
| Trust | head: ~3 % suspended with `token_version = 1` and a matching `admin_audit_log` row |
| Preflight anomalies (`--email-anomalies`) | d924: 2 case-twin groups + padded (space/NBSP/ideographic) twins + a ZWSP near-twin; head: padded/NBSP/ZWSP twins only. Expected A1 counts printed to stderr |

## 3. Phase 1b — standalone Neon project (approval A2; operator Maaz, runbook Hark)

1. Neon Console → **New project** `rideshare-staging-synth`, same region and PostgreSQL major version as
   production (A1 row 906 gives the version). Not "branch", not "import".
2. Create role `rideshare_app_staging` (non-owner) for the preview; the owner role is used only for
   migrations and seeding. Passwords are generated by Neon and go straight into the vault / FastAPI Cloud
   preview env — never into chat or docs.
3. Database name must start with `staging_synth` (e.g. `staging_synth`) so the seed guard accepts it.
4. Maaz runs (or lets a vault-backed runner run) `alembic upgrade d924a6e38f10`, the d924 seed, then the
   rehearsal (§4). Hark may receive the **staging** connection string through the vault only (it holds
   no real data), never the production one.
5. Optional: set the project's history window to the minimum; nothing in it is worth restoring.

## 4. Migration rehearsal on synthetic data (phase 1)

| # | Step | Expected (local evidence §6) |
|---|---|---|
| S-1 | A1 script on the d924 DB | revision `d924a6e38f10`, blocking groups 2 (seeded) |
| S-2 | `alembic upgrade head` | aborts at `f6b2d8e0c413` with `CaseDuplicateEmails` (ids only), stamped `e3a7c41b9f52`, nothing else changed |
| S-3 | resolve the 2 synthetic twin groups (stand-in for Maaz's per-group decision) | `forward_recovery_detect.sql` shows 0 twin groups |
| S-4 | `alembic upgrade head`, timed per revision | `d5e1a0b00002`, exit 0, 0 invalid indexes |
| S-5 | `alembic upgrade head` again | no-op |
| S-6 | A1 on the migrated DB | head column of A1_EXECUTION_INSTRUCTIONS §5.1 |
| S-7 | preview deploy + STAGING_SMOKE_CHECKLIST.md, login as synthetic users | all green |
| S-8 | jobs `python -m jobs.expire_bookings` twice, `purge_idempotency_keys` once | first run expires/closes > 0, second run 0, exit 0 |
| S-9 | FORWARD_RECOVERY_PLAN drills (invalid index, export → downgrade → re-apply, dump → restore → verify) | `tools/forward_recovery_selftest.sh` 25/25 |
| S-10 | delete the project (§7) | — |

## 5. Phase 2 — production-derived data (optional; separate approval A2-P)

Only if phase 1 + A1 leave a concrete question that synthetic data cannot answer (e.g. lock timing on the
real `users` size). Preconditions: phase 1 green; sanitization (below) already proven on phase 1 data;
A1 run and recorded. Order: branch from production (1-day expiry) → operator-only → **sanitize first** →
verification prints 0 leftovers → only then any other connection. The branch's history still holds the
unsanitized rows until the branch is deleted.

### 5.1 Sanitization (phase 2 only; runs first, in one transaction)

File: [`sql/staging_sanitize.sql`](sql/staging_sanitize.sql). Tested on a disposable local DB at head
`d5e1a0b00002` with 500 synthetic users + an idempotency row: refuses to run without
`SET rideshare.sanitize_branch = '<branch>'` (and on any database whose name contains `prod`);
afterwards 0 rows with original email/CNIC/phone/hash, 500 distinct `lower(email)`, 500 distinct
CNICs (unique constraints hold), 0 idempotency rows, 0 NOT VALID constraints.

What it does, all under `LOCK TABLE users IN ACCESS EXCLUSIVE MODE`:

| Column | Becomes | Why |
|---|---|---|
| `email` | `user<id>@example.com` | unique, passes `uq_users_email_lower`, reserved domain |
| `name` | `User <id>` | — |
| `phone_number` | `+92000<id, 7 digits>` | NOT NULL kept; operator code 000 is not allocated (inferred) |
| `cnic` | `00000-<id, 7 digits>-0` | NOT NULL + UNIQUE kept; obviously not a real CNIC |
| `password` | `!sanitized-no-login` | not a bcrypt hash → `verify_password` returns False cleanly (routers/auth.py), so **no copied account can log in**; test accounts are registered fresh through the API |
| `idempotency_keys` | deleted | replay bodies echo booking payloads; disposable by design |
| `admin_audit_log` | kept | no PII by design; append-only triggers block changes anyway |

Run order on the branch: (1) preflight (read-only, on unsanitized data) → (2) **sanitize** →
(3) verification query prints 0 → only then hand the connection string to anyone else.
On a **schema-only** branch, skip (1)–(2) and seed synthetic data with the API (register/book flows)
or the legacy seed from `tests/tools/m3_db_checks.py`.


## 6. Local evidence for phase 1 (Hark, 31 Oct 2026; PostgreSQL 17.11, disposable DBs `rs_a1_*`)

- `seed_synthetic.py` deterministic (two runs, identical sha256), guards tested: refuses `rideshare_ci`
  (name) and a second run on a seeded DB (non-empty users).
- d924 seed: 412 users, 320 rides, 801 bookings; head seed: 407 users (12 suspended, 12 audit rows),
  320 rides, 806 bookings — both load with `ON_ERROR_STOP` under every constraint (partial unique live
  booking, seats CHECK, account_status CHECK, FKs).
- Smoke prerequisites on a copy of the head seed with the RC app (`integration/rc-final` @ 7d66964,
  uvicorn local): `/readyz` 200; synthetic passenger and driver log in (200, token issued); suspended
  synthetic user 401; search Lahore→Islamabad `min_seats=2` → `X-Total-Count: 30`; 2-seat booking with
  `Idempotency-Key` 201, replay 201 (same response), second live booking 409; `/bookings/me` and
  `/rides/me` 200; `jobs.expire_bookings` → `expired=34 closed=12`, rerun `expired=0 closed=0`.
- A1 self-test 29/29 and forward-recovery self-test 25/25 on seeded DBs (A1_EXECUTION_INSTRUCTIONS §7,
  FORWARD_RECOVERY_PLAN §8).

## 7. Deletion after the test

1. Delete the FastAPI Cloud preview (or point it away) first, so nothing reconnects.
2. Phase 1b: delete the Neon **project** `rideshare-staging-synth` (Settings → Delete project). Phase 2:
   delete the branch (or let `expires_at` do it) ([branch expiration](https://neon.com/docs/guides/branch-expiration)).
3. Remove the staging connection string from the vault and any CI/preview secret.
4. Log date/time of creation and deletion (not the connection string) in the ops log.

## 8. Costs and plan limits (Neon [plans](https://neon.com/docs/introduction/plans), read 31 Oct 2026)

| | Free | Launch | Scale |
|---|---|---|---|
| Projects | 100 | 100 | 1,000 |
| Branches per project | 10 | 10 included, extra $1.50/branch-month | 25 included, extra $1.50/branch-month |
| Compute | 100 CU-hours/project/month | $0.106/CU-hour | $0.222/CU-hour |
| Storage | 1 GB/project, 20 GB account total | $0.35/GB-month | $0.35/GB-month |
| Instant-restore history | 6 h, ≤ 1 GB-month | up to 7 days, $0.20/GB-month | up to 30 days |
| Manual snapshots | 1 | 100 | 100 |
| Scale to zero | after 5 min, cannot be disabled | can be disabled | configurable |

(The earlier "0.5 GB vs 1 GB" discrepancy is resolved: the plans page now states 1 GB/project.) A
synthetic project of a few thousand rows is far below every Free limit and, being its own project, uses its
own 100 CU-hours, not production's.

## 9. Environment for a FastAPI Cloud preview (values live in the preview's env, never in chat/docs)

| Variable | Staging value | Notes |
|---|---|---|
| `DATABASE_URL` | staging project URL (phase 1b) or branch URL (phase 2), `postgresql+asyncpg://`, `-pooler` host OK for the app | `rideshare_app_staging` (§3) |
| `SECRET_KEY` | **new random staging key** (`secrets.token_urlsafe(48)`) | never the production key: staging tokens must not work in production and vice versa |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | as production |
| `FRONTEND_ORIGINS` | the preview's own origin only | CORS test in the smoke list |
| `MAX_REQUEST_BODY_BYTES` | default `65536` | 413 test |
| `READINESS_TIMEOUT_SECONDS` | default `2` | |
| `BOOKING_MAX_SEATS` | value planned for launch (LB-11), e.g. `1` | test both 1 and 8 if time allows |
| `BOOKING_PENDING_TTL_MINUTES` / `RIDE_AUTO_CLOSE_AFTER_HOURS` | defaults (360 / 24) | |
| `AUTH_RATE_LIMIT_ENABLED` | `false` (rate limiting is a separate approval, LB-3) | |
| `SECURITY_HEADERS_ENABLED` | default (on) | |
| Jobs | run manually against the staging **direct** URL: `python -m jobs.expire_bookings` | expect `job=expire_bookings status=ok …` and exit 0 |
