# Neon staging plan for the RC rehearsal

Prepared 31 Oct 2026 (staging-ops). **Plan only. Creating any Neon branch, connecting to it, and
creating a FastAPI Cloud preview are approval-gated actions for Maaz.** Neon facts below come from
Neon's docs read on 31 Oct 2026 (URLs inline); the production project's plan, region, roles and
history window are **unknown** to Hark.

## 1. Choice: branch with data vs schema-only

| Option | What it is (Neon docs) | Good for | Risk | Verdict |
|---|---|---|---|---|
| **A. Schema-only branch + synthetic data** | "replicate only the database schema from a source branch, without copying any of the actual data" ([schema-only branches](https://neon.com/docs/guides/branching-schema-only)); new root branch; Console/API only | Smoke tests, Starlette/PyJWT behaviour, job runs, migration *mechanics* | Does not exercise the real data shape (case twins, padded emails, 0-seat rides, legacy tokens, row counts for lock timing) | **Use for everything except the migration rehearsal** |
| **B. Branch from production (data) + immediate sanitization** | copy-on-write clone at a point in time; "does not increase load on the parent branch" ([branching](https://neon.com/docs/introduction/branching)) | The one thing A cannot do: `alembic upgrade head` from the real baseline with the real data shape, real lock timings, the real preflight result | Real CNIC/phone/email/password hashes exist on the branch until sanitized; anyone with Neon project access can read them meanwhile | **Use only for the migration rehearsal (M-1…M-8 below), operator-only, sanitized before any app connects** |
| C. Neon "Anonymized data" branch (beta) | static masking with PostgreSQL Anonymizer; "the branch is unavailable for connections while anonymization is in progress"; cannot reset/restore; FK columns cannot be masked; masking "does not fully enforce database constraints" ([data anonymization](https://neon.com/docs/workflows/data-anonymization)) | Same as B with masking done by Neon | Beta; unique `cnic` and `lower(email)` constraints need care ("Random Unique Email"); availability on Maaz's plan unverified | Acceptable alternative to B's SQL if Maaz prefers the Console; our SQL (§3) is tested and constraint-safe |

Recommendation: **B for one rehearsal run, then A for the preview smoke.** Sanitization rewrites
`email`, which is exactly what the AST-005 preflight inspects, and makes every email unique, so the
f6b2 preflight would then pass trivially. Order on the B branch is therefore: run the read-only
preflight on the **unsanitized** branch first (operator-only; outputs ids/counts only) and record it,
then sanitize, then run the migration. Everything else in the data shape (row counts, legacy statuses,
0-seat rides, NULL `expires_at`, lock timings) is untouched by sanitization.

## 2. Data isolation and access

- Branch name `staging-rc-<yyyymmdd>`, created from the production branch at "now" (or a recorded
  timestamp), **with an expiration**: Console default auto-delete is 1 day; API/CLI branches have no
  expiration unless `expires_at` is set (max 30 days) ([branch expiration](https://neon.com/docs/guides/branch-expiration)). Use 1 day.
- Who can connect before sanitization: **Maaz only** (operator). Hark never receives the branch
  connection string until §3 has committed and its verification printed `0` leftovers.
- Who can connect after: Maaz, Hark's test runner (through a vault-stored secret, never pasted in
  chat), the FastAPI Cloud preview. Nobody else; no sharing of the Neon project.
- Roles on the branch: the same as production (copied). Prefer a fresh branch-only role for the
  preview (`rideshare_app_staging`, non-owner, non-superuser) so the production password never sits
  in a preview environment. If that is not possible, **rotate the copied app role's password on the
  branch** (branch-local change; does not touch production).
- Network: Neon branches get their own hostnames; the preview gets only the **branch** URL. Jobs and
  migrations use the direct host; the app may use the `-pooler` host.
- History: the branch's own history still contains the pre-sanitization rows (copy-on-write); a
  point-in-time child of the branch could resurrect them. Deleting the branch is the real cleanup (§5).

## 3. Sanitization (runs first, in one transaction)

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

## 4. Migration rehearsal on the branch (operator: Maaz; runbook: Hark)

| # | Step | Expected |
|---|---|---|
| M-1 | `SELECT version_num FROM alembic_version` | `d924a6e38f10` (MIGRATION_READINESS §2) |
| M-2 | preflight rev 2 (unsanitized branch) | record counts; `case_duplicate_groups` = 0 or a per-group decision |
| M-3 | sanitize (§3) | 0 leftovers |
| M-4 | `alembic upgrade head` (direct host), timed | `d5e1a0b00002`, exit 0; note duration per revision |
| M-5 | `alembic upgrade head` again | no-op, exit 0 |
| M-6 | 0 invalid indexes, 0 NOT VALID constraints, `alembic check` clean | as in MIGRATION_READINESS §3 |
| M-7 | preview deploy + STAGING_SMOKE_CHECKLIST.md | all green |
| M-8 | reverse rehearsal: downgrade one revision at a time to `d924a6e38f10` with exports, then upgrade head again | matches local evidence; record anything different |

## 5. Deletion after the test

1. Delete the FastAPI Cloud preview (or point it away) first, so nothing reconnects.
2. Delete the Neon branch (Console → Branches → Delete, or let `expires_at` do it). Deletion is
   permanent and removes the branch's computes ([branch expiration](https://neon.com/docs/guides/branch-expiration)).
3. Remove the branch connection string from the vault and any CI/preview secret.
4. Log date/time of creation and deletion (not the connection string) in the ops log.

## 6. Costs and plan limits (Neon pricing/docs, 31 Oct 2026)

| | Free | Launch | Scale |
|---|---|---|---|
| Branches per project | 10 | 10 included, extra $1.50/branch-month | 25 included, extra $1.50/branch-month |
| Compute | 100 CU-hours/project/month | $0.106/CU-hour | $0.222/CU-hour |
| Storage | 0.5 GB/project ([pricing](https://neon.com/pricing)); FAQ says 1 GB ([free plan FAQ](https://neon.com/faqs/free-plan-limits-and-quotas)) — the two Neon pages disagree | $0.35/GB-month | $0.35/GB-month |
| Instant-restore history | 6 h, 1 GB cap | up to 7 days, $0.20/GB-month | up to 30 days |
| Scale to zero | after 5 min, cannot be disabled | can be disabled | configurable |

Sources: [neon.com/pricing](https://neon.com/pricing), [plans](https://neon.com/docs/introduction/plans).
A one-day branch of a small database costs at most a few CU-hours (it scales to zero after 5 min
idle); copy-on-write storage only grows with the branch's own writes (sanitization rewrites every
`users` row once). On Free, the branch counts against the 10-branch limit and the shared 100 CU-hours.

## 7. Environment for a FastAPI Cloud preview (values live in the preview's env, never in chat/docs)

| Variable | Staging value | Notes |
|---|---|---|
| `DATABASE_URL` | branch URL, `postgresql+asyncpg://`, `-pooler` host OK for the app | branch-only role (§2) |
| `SECRET_KEY` | **new random staging key** (`secrets.token_urlsafe(48)`) | never the production key: staging tokens must not work in production and vice versa |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | as production |
| `FRONTEND_ORIGINS` | the preview's own origin only | CORS test in the smoke list |
| `MAX_REQUEST_BODY_BYTES` | default `65536` | 413 test |
| `READINESS_TIMEOUT_SECONDS` | default `2` | |
| `BOOKING_MAX_SEATS` | value planned for launch (LB-11), e.g. `1` | test both 1 and 8 if time allows |
| `BOOKING_PENDING_TTL_MINUTES` / `RIDE_AUTO_CLOSE_AFTER_HOURS` | defaults (360 / 24) | |
| `AUTH_RATE_LIMIT_ENABLED` | `false` (rate limiting is a separate approval, LB-3) | |
| `SECURITY_HEADERS_ENABLED` | default (on) | |
| Jobs | run manually against the branch's **direct** URL: `python -m jobs.expire_bookings` | expect `job=expire_bookings status=ok …` and exit 0 |
