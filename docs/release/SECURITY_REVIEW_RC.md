# RideShare release-candidate security & SRE review

Prepared 31 Oct 2026 (PKT) for the RC built from PRs #2–#24. **Documentation only.**
Nothing here was executed against production, Neon, staging or FastAPI Cloud. The only
production contact was passive, unauthenticated `GET`/`HEAD` requests to
`https://rideshare-api.fastapicloud.dev/` (31 Oct 2026 ~10:41 PKT). No secret value appears
in this document; secrets are referred to by file and commit only.

Code references are to the branch that carries the code under review:

| Short name | Branch @ head | PR |
|---|---|---|
| RC backend | `feat/admin-audit-log` @ `7cedddb` (contains #5, #10, #12, #13, #18, #19, #20, #22) | #21 |
| headers | `feat/security-headers` @ `ec5bc67` | #6 |
| health | `feat/health-readiness` @ `364d8ad` | #7 |
| validation | `fix/input-validation` @ `7f397ac` | #8 |
| deps | `chore/dependency-security` @ `5fb3d01` | #10 |
| ops docs | `docs/operational-readiness` @ `61b915b` | #11 |
| safe errors | `fix/safe-errors-body-limit` @ `22a4040` | #15 |
| frontend | `feat/frontend-multi-seat` @ `32b476c` | #24 |

Severity scale: **Critical** (exploitable now, data or account takeover), **High** (must be
fixed or explicitly accepted before real users), **Medium** (fix before scale / within the
first release cycle), **Low** (hardening). *Launch blocker* = must be closed (or formally
accepted by Maaz in writing) before the RC is promoted to production with real users.

Owners: **Maaz** (decisions, dashboards, secrets, production), **Hark** (docs, code PRs,
rehearsals on disposable DBs), **Codex Astra** (review / second implementation, where the
roadmap assigns it).

---

## 0. Summary table

| # | Finding | Severity | Launch blocker | Owner | Effort |
|---|---|---|---|---|---|
| S1 | CNIC + phone stored in plaintext; DB dumps/branches/PITR expose them | High | **Yes, or written acceptance** (Maaz) — see §1 | Maaz decide → Hark build | L (5–8 d) |
| S2 | Historical `.env` + copies in git history; `main` still the default branch and still contains them (AST-002) | High | **Yes** (rotation confirmation + default branch) | Maaz | S (1–2 h Maaz) |
| S3 | JWT in `sessionStorage` (XSS-readable), 60-min lifetime, no refresh, no server logout | Medium | No (accepted MVP trade-off, documented) | Hark (follow-up D/E) | M |
| S4 | Admin creation process undefined (D1); admin tokens same lifetime, no 2FA (D8) | High | **Yes** before mounting `/admin` in prod | Maaz decide → Hark | S–M |
| S5 | Audit-log immutability relies on triggers; app role is owner (can drop triggers) | Medium | No (blocker only for "tamper-evident" claims) | Maaz (role split) | S |
| S6 | Auth rate limiting disabled; client-IP boundary unverified | High | **Yes** for public launch (credential stuffing) | Maaz (Redis + proxy check) | M |
| S7 | Production serves **no** CSP / XFO / Referrer-Policy / HSTS today | Medium | Fixed by deploying #6 (part of RC) | Hark/Maaz deploy | — |
| S8 | Starlette 0.52→1.3 + python-jose→PyJWT unexercised on FastAPI Cloud | Medium | **Yes** — staging smoke required | Maaz approve, Hark run | S |
| S9 | Backups: Neon PITR window / plan unverified; no logical dump; no restore drill done | High | **Yes** — one verified restore before real data | Maaz | S |
| S10 | Migration rollback has ordering traps and two lossy downgrades | High | **Yes** — staging rehearsal of the full chain | Hark (rehearse) / Maaz approve | M |
| S11 | No structured app logging, no alerting; `/test-db` live in prod, `/healthz` `/readyz` 404 | Medium | **Yes** — uptime monitor + alert before real users | Maaz (dashboards), Hark (#15 follow-up) | S–M |
| S12 | Password policy: 12-char min for new accounts; no breach check; no reset flow | Low | No | Hark | S |
| S13 | Login enumeration: generic 401 + dummy hash (good); register 409 reveals "in use" | Low | No | — | — |

---

## 1. CNIC and phone number handling

**Evidence**
- Storage: `models.User.cnic` (`String, unique=True, nullable=False`) and `models.User.phone_number`
  (`String`) — plaintext columns created in `alembic/versions/50872040220b_initial_tables.py`.
  Unique constraint `users_cnic_key` is on the raw string (RC backend `models.py`).
- Normalisation (new rows only): `schemas.normalize_cnic` → 13 digits, `schemas.normalize_pk_mobile`
  → `+923XXXXXXXXX` (validation `schemas.py`, PR #8). Legacy rows are verbatim
  (`docs/privacy/CNIC_PHONE_PROTECTION.md` §3, PR #8).
- Duplicate check: `routers/auth.register` uses `User.cnic == …` on the RC backend branch;
  PR #8 changes it to a digit-normalised `regexp_replace(cnic,'[^0-9]','','g')` comparison
  (sequential scan, not index-backed).
- Masks: `privacy.mask_cnic` → `*****-****NNN-*` (hides region prefix and gender digit),
  `privacy.mask_phone` → `+92 3** *** **NN`, `privacy.mask_email` → `a***@domain`. All fail closed
  (unrecognised → fully masked). (`privacy.py`, PR #13/#19.)
- API exposure: `schemas.UserResponse` = `id, name, email, role, allowed_modes` — no CNIC/phone.
  `PassengerSummary` = `id, name`. Registration responses are tested to exclude both
  (`tests/test_registration.py`, PR #8).
- Admin view: `GET /admin/users/{id}` returns `AdminUserResponse` with only `email_masked`,
  `cnic_masked`, `phone_masked` (`routers/admin.AdminUserResponse.from_user`) and writes a
  `user.view` audit row in the same transaction (`routers/admin.get_user`).
- Admin reason text: `AccountStatusChange._reason_is_meaningful_and_pii_free` rejects any run of
  ≥ 7 digits (CNIC/phone) and control characters. Audit metadata keys matching
  `cnic|phone|email|name|…` are rejected by `audit.validate_metadata`.
- Logging: app had no logging calls before #15. `database.create_async_engine(..., hide_parameters=True)`
  lands with #8. #15 `http_safety.log_unhandled` logs exception class, SQLSTATE, constraint name
  and app frame — never `str(exc)` — so PostgreSQL `DETAIL: Key (cnic)=(…)` / `Failing row contains (…)`
  cannot reach logs. **On the RC backend branch alone (#21 without #8/#15), `database.py` has no
  `hide_parameters`** — the protection exists only once #8 and #15 are merged (merge-order dependency).
- JWT: claims are `sub, role, tv, iat, exp` — no PII (`routers/auth.create_access_token`).
- Frontend: CNIC/phone are sent once in the register request; `sessionStorage` holds only
  `{token, expiresAt, mode}` (`frontend/src/auth/sessionStore.ts`, `docs/frontend/SESSION_PERSISTENCE.md`, #24).

**Risk.** Anyone with database credentials, a Neon branch, a PITR restore, a `pg_dump`, or a
leaked connection string reads every user's CNIC and phone in clear. CNIC is a national
identity number (gender digit, region) and is high-value for fraud. Uvicorn access logs
include query strings; no PII travels in queries today (ride search only).

**Severity:** High. **Launch blocker:** Yes for real users *unless* Maaz accepts in writing
"plaintext at rest for MVP, encryption within N weeks", with the compensating controls below
in place. Encryption itself is L effort and not required to ship the RC to staging.

**Fix (target, already designed).** `docs/privacy/CNIC_PHONE_PROTECTION.md` §4–§7 (PR #8):
AES-256-GCM ciphertext columns + HMAC-SHA256 blind index (separate keys), `PII_MODE`
`plaintext → dual → encrypted → encrypted_only`, expand → backfill → switch → contract,
key escrow before contract.

**Compensating controls for launch (S effort, Maaz):**
1. Confirm only Maaz holds the Neon owner credential; no shared DB URLs in chats/CI.
2. Neon branches made for staging are deleted after use (STAGING_MIGRATION_PLAN S12).
3. Do not collect CNIC at all until D2/D3 (verification route, document storage) if the
   product can live without it at launch — the smallest risk is not storing it. (Product decision.)
4. Merge #8 and #15 before any traffic so `hide_parameters` + safe error logging are live.

**Effort:** encryption L (5–8 days incl. backfill rehearsal); compensating controls S.
**Owner:** Maaz (decision, keys), Hark (implementation PR), Codex Astra (review).

---

## 2. Historically exposed secrets in git history (AST-002)

**Evidence** (from `docs/security/AST-002_REMEDIATION_PLAN.md`, PR #11; no values reproduced):
- `.env` committed in `6e861fa` (26 Feb 2026), removed in `d0ed9fe` (5 Mar 2026). It held a local
  `DATABASE_URL` (localhost, user `postgres`, a real-looking 9-char dev password) and a
  placeholder-looking 26-char `SECRET_KEY`.
- Byte-identical copy `routers/__pycache__/Untitled`; same values in `routers/auth.py`
  (hard-coded key), `alembic.ini`, `docker-compose.yml`, `__pycache__/database.cpython-314.pyc`.
  All still in **`main`'s current tree** (`main` @ `e700ddb`, default branch, public repo);
  removed on `rideshare-v2` in `f9e607b` / `02ef842`. Every branch carries them in history.
- Scan of 295 unique blobs found **no** Neon host / `npg_` password / credentialed Redis URL /
  cloud keys.
- Current code refuses short/placeholder keys at import (`config.py`: `SECRET_KEY` ≥ 32 bytes for
  HS256 and not starting with `your-secret`, `change-me`, …). Live OpenAPI includes `allowed_modes`
  (added after the key check), so prod is very likely not on the placeholder — **inferred, unverified**.

**Risk.** If production ever ran `main`'s code with the committed key (≈ Jun–Oct 2026), any
token from that period was forgeable by anyone who read the public repo. If the dev DB
password is reused anywhere, it is public. `main` as the default branch also means GitHub
Actions `schedule:` workflows run **only** from `main` (see BACKGROUND_JOBS.md §2).

**Severity:** High. **Launch blocker:** Yes — items 1–3 below.

**What rotation requires (in order, each approval-gated):**
1. Maaz confirms, read-only: prod `SECRET_KEY` is not the committed value and is ≥ 32 bytes;
   the committed dev DB password is not reused anywhere; whether `main` was ever deployed;
   which branch FastAPI Cloud auto-deploys.
2. Rotate `SECRET_KEY` in FastAPI Cloud env (new 32+ random bytes, e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`
   run on Maaz's machine). Effect: **every JWT is invalidated; all users re-login.** No DB change.
   Do it once, in the same window as the RC production deploy, so users are logged out once.
   (The RC itself does not force a logout: pre-#19 tokens without `tv` read as 0 and stay valid.)
3. Neon role password rotation only if any doubt about reuse (role swap so the app never sees
   a window of failed auth).
4. Default branch: fast-forward `main` to `rideshare-v2` after the RC merges (or switch default
   to `rideshare-v2`) **after** confirming FastAPI Cloud's auto-deploy branch, so a branch change
   does not trigger an unintended deploy.
5. GitHub secret scanning + push protection; gitleaks CI job.
6. No history rewrite (rationale in AST-002 plan §7: secrets are non-production / rotated,
   rewrite breaks every open PR and clone, and the public copies are already cached).

**Effort:** S (≈ 1–2 h of Maaz's time). **Owner:** Maaz.

---

## 3. JWT, authentication and session risks

**Evidence**
- Algorithm: HS256 by default; `config.py` allows only HS256/384/512 and enforces key length
  per algorithm. Decoder pins the algorithm from configuration: `jwt.decode(..., algorithms=[ALGORITHM])`
  (`routers/dependencies.decode_access_claims`) — no `alg=none`/RS-HS confusion.
- Required claims `sub, role, exp`; strict types (`type(exp) is int`), `sub` regex
  `[1-9][0-9]{0,9}` ≤ 2³¹−1, `iat` sanity, `nbf` honoured; role re-read from DB and must equal the
  signed claim (`get_current_user`) — a demoted admin's token fails with 401.
- Expiry: `ACCESS_TOKEN_EXPIRE_MINUTES` default 60, positive-int validated (`config.py`).
- `tv` claim (PR #19): tokens carry `tv = users.token_version`; suspend and reinstate both bump it
  (`services/accounts.suspend_user` / `reinstate_user`), so all prior tokens die. Missing `tv` = 0 so
  pre-#19 tokens keep working until expiry (`decode_access_claims`). `TOKEN_VERSION_MAX` bounds it.
- Storage: `sessionStorage` key `rideshare.session.v1` holding `{token, expiresAt, mode}`; tab-scoped;
  expiry re-derived from the token; `localStorage` never written (asserted in Vitest + Playwright).
  Client uses `credentials: 'omit'`; CORS `allow_credentials=False` (`docs/frontend/SESSION_PERSISTENCE.md`, #24).
- Logout: `AuthProvider.logout` → `endSession()` clears memory + sessionStorage only. **No server-side
  logout**; a copied token stays valid until `exp` (≤ 60 min) unless the account's `token_version`
  is bumped (today only via admin suspend/reinstate).
- No refresh token, no revocation list, no "log out all devices", no password change/reset endpoint.
- Password policy: new accounts `min_length=12`, ≤ 72 UTF-8 bytes (bcrypt limit), not blank, no
  truncation/normalisation (`schemas.UserCreate`, `validate_password_input`); bcrypt cost 12
  (`routers/auth.hash_password`). Login accepts legacy shorter passwords (`UserLogin min_length=1`).
- Enumeration: login returns the same `401 Invalid email or password` for unknown user, wrong password
  and (default D6) suspended account; unknown users still pay a bcrypt check against a lazily generated
  dummy hash (`_dummy_password_hash`, `verify_password`) so timing is uniform. Register returns
  `409 Registration details already in use` for duplicate email **or** CNIC — this reveals that
  *some* identifier is registered (accepted; unavoidable without email verification, AST-011).
- Brute force: per-IP and per-IP+email limits exist (`auth_rate_limit.check_login_identity`) but are
  **disabled** (§6).

**Risks & severity**
| Risk | Severity | Blocker |
|---|---|---|
| XSS anywhere on the SPA origin can read the token from `sessionStorage` and use it for ≤ 60 min. Mitigated by strict CSP `script-src 'self'` (once #6 is deployed), React escaping, no `dangerouslySetInnerHTML` (not re-audited here). | Medium | No |
| Logout does not revoke; stolen token valid until expiry. | Medium | No |
| No password reset → support burden, account lockout; no breach-password check. | Low | No |
| Online guessing unthrottled while rate limiting is off. | High | **Yes** (see §6) |
| Register 409 reveals an email/CNIC is registered. | Low | No |

**Fix.** (a) Ship #6 so CSP is live (S7). (b) Follow-up per SESSION_PERSISTENCE option D/E:
httpOnly `SameSite=Lax` cookie + CSRF, or short access token + rotating refresh cookie; add
`POST /auth/logout` that bumps `token_version` ("log out everywhere") — S, reuses #19.
(c) Lower `ACCESS_TOKEN_EXPIRE_MINUTES` to 30 once refresh exists. (d) AST-011/012 email
verification + reset. **Effort:** logout-everywhere S (½ d); cookie/refresh M (3–4 d).
**Owner:** Hark (PRs), Maaz (decision on D vs E).

---

## 4. Admin authorization

**Evidence**
- Guard: every `/admin` route is built by `routers/admin.admin_router()` which injects
  `Depends(require_admin)`; `tests/test_admin_guard.py` fails if any `/admin` route is reachable
  without it. `require_admin` → 401 for bad tokens, 403 `Admin access required` for non-admins
  (`routers/dependencies.require_admin`).
- Admin role cannot be self-assigned: `UserCreate.role: Literal[passenger, driver]` (`schemas.py`);
  `permissions.allowed_modes(admin) == []`.
- **How admins are created (D1): undefined.** TRUST_AND_SAFETY §4 item 6 *proposes* an operator
  script `python -m scripts.grant_admin --user-id N` writing an audit row (`actor=NULL`,
  `action=admin_granted`). **That script does not exist on any branch**, and `audit.ACTIONS` contains
  only `user.view`, `user.suspend`, `user.reinstate`. Today the only way is a manual
  `UPDATE users SET role='admin'` on production — unaudited.
- Token lifetime / 2FA (D8): admins use the same 60-min HS256 token as everyone; no TOTP, no
  step-up, no IP allow-list.
- Self-action: `services/accounts._lock_target` → 403 `Admins cannot change their own account status`.
- Admin-on-admin: allowed (an admin may suspend another admin, intended for a compromised admin;
  TRUST_AND_SAFETY §5.4). Combined with no 2FA, one stolen admin token can suspend every other admin
  and cancel every driver's future rides (D5 auto-cancel) within its 60-minute life. Suspension bumps
  the victim's `tv`, so the victims cannot undo it without another admin.
- `GET /admin/audit-log` is readable by every admin (no finer role).

**Risk / severity:** High (privilege is all-or-nothing, unaudited grant, no second factor).
**Launch blocker:** Yes **if** the admin router is reachable in production with any admin
account. Not a blocker if no admin account exists at launch (the endpoints then always 403).

**Fix (in order):**
1. D1 decision (Maaz): named admin accounts, separate from personal passenger/driver accounts.
2. `scripts/grant_admin.py` + `revoke_admin`, writing `admin.grant` / `admin.revoke` audit rows with
   `actor_id = NULL` and an operator note code; add both to `audit.ACTIONS` (Hark, S ½ d).
3. D8: admin tokens 15 min (`ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES`, issued at login when
   `role == admin`) — S; TOTP before admin endpoints go live — M (2 d).
4. Optional: forbid suspending an admin except by ≥ 2 admins or via the operator script (D1/D8). S.
**Owner:** Maaz (D1, D8), Hark (code).

---

## 5. Audit-log permissions

**Evidence** (`alembic/versions/d5e1a0b00002_admin_audit_log.py`, `audit.py`, PR #21):
- Single write path `audit.record()` — flushes in the caller's transaction, never commits; action
  and target-type allow-lists; metadata key/value validation.
- Immutability: `admin_audit_log_immutable()` trigger function raises SQLSTATE 42501 on
  `BEFORE UPDATE OR DELETE … FOR EACH ROW` and `BEFORE TRUNCATE … FOR EACH STATEMENT`.
- **Limits of triggers:** the table **owner** (today the same role the app connects as, inferred
  from the single `DATABASE_URL`) can `ALTER TABLE admin_audit_log DISABLE TRIGGER ALL`, `DROP TRIGGER`,
  `DROP TABLE`, or run the migration downgrade (which drops the table and all history). A superuser
  can also `SET session_replication_role = replica` to skip triggers. So triggers stop *accidents*
  and app-level bugs, not a compromised app credential.
- Downgrade of `d5e1a0b00002` deletes all audit history (export first, PR #21 Rollback).
- Retention: grows forever (D7 open).

**Severity:** Medium. **Launch blocker:** No — unless the product claims a tamper-evident audit
trail; then Yes.

**Recommended least-privilege role split — PROPOSAL ONLY, NOT EXECUTED.** Needs Maaz's approval
and a Neon change; rehearse on a Neon branch first. Role names are placeholders; passwords are set
in the Neon console, never in SQL files or chat.

```sql
-- Run as the current owner role (call it rideshare_owner) on a staging Neon branch first.
-- 1. Roles
CREATE ROLE rideshare_migrator LOGIN;   -- used only by `alembic upgrade`; owns all objects
CREATE ROLE rideshare_app      LOGIN;   -- used by the API (DATABASE_URL in FastAPI Cloud)
CREATE ROLE rideshare_jobs     LOGIN;   -- used by jobs.expire_bookings / jobs.purge_idempotency_keys
CREATE ROLE rideshare_readonly LOGIN;   -- optional: support / analytics

-- 2. Ownership moves to the migrator (owner of schema objects == only role that can ALTER/DROP)
ALTER SCHEMA public OWNER TO rideshare_migrator;
REASSIGN OWNED BY rideshare_owner TO rideshare_migrator;   -- review the object list first

-- 3. Nobody else may create objects in public
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT  USAGE  ON SCHEMA public TO rideshare_app, rideshare_jobs, rideshare_readonly;

-- 4. App: DML on business tables, append-only on the audit log
GRANT SELECT, INSERT, UPDATE, DELETE ON users, rides, bookings, idempotency_keys TO rideshare_app;
GRANT SELECT, INSERT ON admin_audit_log TO rideshare_app;
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON admin_audit_log FROM rideshare_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rideshare_app;
GRANT SELECT ON alembic_version TO rideshare_app;           -- /readyz or diagnostics may read it

-- 5. Jobs: only what the two jobs touch (bookings/rides status, idempotency purge)
GRANT SELECT, UPDATE ON rides, bookings TO rideshare_jobs;
GRANT SELECT, DELETE ON idempotency_keys TO rideshare_jobs;
-- pg_try_advisory_lock needs no grant.

-- 6. Read-only (optional): exclude raw PII columns
GRANT SELECT ON rides, bookings, admin_audit_log TO rideshare_readonly;
GRANT SELECT (id, name, role, account_status, created_at) ON users TO rideshare_readonly;

-- 7. Future objects created by migrations get the same grants
ALTER DEFAULT PRIVILEGES FOR ROLE rideshare_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rideshare_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rideshare_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO rideshare_app;
-- …then re-apply step 4's REVOKE for any new append-only table in its own migration.

-- 8. Defence in depth: none of these roles may bypass triggers
ALTER ROLE rideshare_app  NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
ALTER ROLE rideshare_jobs NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
```

Notes: `session_replication_role` can only be set by superusers, so the app role cannot skip
triggers. Neon's default `neondb_owner` is not a true superuser but owns objects — hence the
ownership move. After the split, `DATABASE_URL` (app) and the migration URL differ; the
deploy runbook must run `alembic upgrade` with the migrator URL. Verify with
`SET ROLE rideshare_app; DELETE FROM admin_audit_log;` → `permission denied` (privilege check
fires before the trigger). **Effort:** S (½ d incl. rehearsal). **Owner:** Maaz (execute),
Hark (rehearsal script on a disposable DB + runbook).

---

## 6. Redis auth rate limiting

**Evidence** (`auth_rate_limit.py`, already on `rideshare-v2` since `e8da425` "feat(auth): add Redis-backed authentication rate limiting"; prerequisites in
`docs/operations/REDIS_RATE_LIMIT_PREREQUISITES.md`):
- Disabled by default: `AUTH_RATE_LIMIT_ENABLED` defaults `"false"` (`RateLimitSettings.from_environment`).
- Enabling **refuses to start** unless `AUTH_RATE_LIMIT_CLIENT_IP_SOURCE=verified-asgi` and a separate
  ≥ 32-byte non-placeholder `AUTH_RATE_LIMIT_HMAC_KEY` (`RateLimitSettings.__post_init__`), and
  `config.py` refuses an HMAC key equal to `SECRET_KEY`.
- Client identity: `scope["client"][0]` only — never parses `X-Forwarded-For` (module docstring,
  `client_ip`). Behind Cloudflare → FastAPI Cloud (envoy, seen in live `x-envoy-upstream-service-time`
  header) that is the proxy address unless the platform's Uvicorn runs with trusted
  `--forwarded-allow-ips` / `--proxy-headers`. **Unverified** for FastAPI Cloud.
- Keys: `HMAC-SHA256(hmac_key, bucket|identifiers)` under namespace `rideshare:auth:v1`
  (`AUTH_RATE_LIMIT_NAMESPACE`); identifiers are client IP and IP+casefolded email — no raw email
  or IP stored in Redis.
- Buckets/defaults: login 30/60 s per IP, register 5/600 s per IP, login identity 5/60 s per IP+email;
  Redis timeout 0.5 s.
- Failure mode: **fail closed** — a Redis error returns the limiter's "unavailable" response
  (`consume` comment "Fail closed at the Redis boundary"); `/readyz` reports `redis` status (#7).

**Risk.** Today login and register are unthrottled → credential stuffing / password spraying
and registration spam. Turning it on wrongly is worse: if `scope["client"]` is the proxy IP,
every user shares one bucket and 30 logins/min across the whole site lock everyone out;
fail-closed + a Redis outage = nobody can log in.

**Severity:** High. **Launch blocker:** Yes for a public launch with real accounts.

**Prerequisites (all Maaz-approved):**
1. Preview-only client-IP test (REDIS_RATE_LIMIT_PREREQUISITES §2): deploy a preview, call it from
   two distinct networks, confirm `scope["client"]` differs and a spoofed `X-Forwarded-For` is ignored.
   If it is the proxy IP, the platform must expose trusted proxy headers — ask FastAPI Cloud support;
   do **not** parse XFF in app code without a fixed trusted-proxy list.
2. Provision Redis: FastAPI Cloud documents a Redis Cloud integration
   (https://fastapicloud.com/docs/integrations/redis-integration — listed in their docs nav, not read in
   detail this session). TLS (`rediss://`), ACL user limited to `EVALSHA/EVAL/GET/PTTL/…`, `noeviction`
   or `volatile-ttl`, a dedicated DB/namespace.
3. Decide fail-closed vs fail-open. Recommendation: **keep fail-closed for register, consider
   fail-open for login with an alert** — losing all logins during a Redis blip is a full outage.
   That is a code change (S) behind a flag; until then fail-closed + Redis uptime SLO.
4. Set `AUTH_RATE_LIMIT_ENABLED=true`, `…_CLIENT_IP_SOURCE=verified-asgi`, `…_REDIS_URL`,
   `…_HMAC_KEY` (new random secret) in FastAPI Cloud; verify V1–V8; rollback = flag false.
**Effort:** M (½–1 d including preview test). **Owner:** Maaz (provision, env), Hark (test plan,
optional fail-open flag PR).

---

## 7. Security headers and CSP

**Live production, passive check (31 Oct 2026 ~10:41 PKT, `curl -D -`):**

| Path | Status | Headers seen (security-relevant) |
|---|---|---|
| `/` | 200 | `x-content-type-options: nosniff` only; `server: cloudflare`; `x-envoy-upstream-service-time` |
| `/docs` | 200 | **none** (no CSP, no XFO, no nosniff) |
| `/healthz`, `/readyz` | 404 | — (#7 not deployed) |
| `/test-db` | **200** | — (DB probe publicly reachable; removed by #7) |
| `/openapi.json` | 200 | 14 paths; `info.title` "FastAPI" 0.1.0 |

No `Content-Security-Policy`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`,
`Cross-Origin-Opener-Policy` or `Strict-Transport-Security` in production.

**Branch config (#6 `security_headers.py`):** pure ASGI middleware, never overwrites a route's header;
`SECURITY_HEADERS_ENABLED` default true (kill switch); `APP_CSP` = `default-src 'self'; script-src 'self';
style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none';
base-uri 'self'; form-action 'self'; frame-ancestors 'none'`; `DOCS_CSP` for `/docs*` `/redoc*` allows
`'unsafe-inline'` + `cdn.jsdelivr.net` + Google Fonts; base headers nosniff, `X-Frame-Options: DENY`,
`Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` (camera/mic/geo/payment/usb off),
`COOP: same-origin`. HSTS gated: `HSTS_ENABLED` default false, `max-age=HSTS_MAX_AGE; includeSubDomains`
(hard-coded `includeSubDomains`). Integrated build verified by curl + Chromium (0 CSP violations on SPA)
— MERGE_READINESS_FINAL §5.5/5.5a.

**Gaps:** (a) prod lacks everything above until #6 deploys; (b) `/docs` CSP was not browser-verified
on the integrated build (CDN unreachable from sandbox) — check on preview; (c) no CSP `report-to`
endpoint, so no burn-in telemetry (SECURITY_HEADERS_CHECKLIST); (d) `.dev` is HSTS-preloaded at the TLD,
so browsers already force HTTPS on `fastapicloud.dev` — the app header adds little until a custom
domain; `includeSubDomains` on a custom apex is risky — make it configurable before enabling;
(e) L-7: non-integer `HSTS_MAX_AGE` gives a bare `ValueError` at startup; (f) `/docs` public — decision
(keep for MVP per PRODUCTION_CONFIG_CHECKLIST, revisit before real users).

**Severity:** Medium. **Launch blocker:** deploying #6 is part of the RC; HSTS stays off (gated, not a
blocker). **Effort:** none beyond deploy; HSTS ramp 300 → 86400 → 31536000 over ≥ 2 weeks after a
custom domain. **Owner:** Maaz (deploy approval, HSTS decision), Hark (preview verification).

---

## 8. Dependency upgrades

**Evidence** (#10, `docs/security/DEPENDENCY_AUDIT_2026-10-31.md`): pip-audit 29 → 0; Starlette
0.52.1 → **1.3.1** (major), FastAPI 0.136.3 (capped below 0.137 because 0.137 restructured
`include_router` and broke two route-introspection test harnesses), python-multipart 0.0.31,
python-jose → **PyJWT 2.15.1**, `cryptography` pin removed (unused, 6 advisories). Reachability
table: 1 potentially reachable (idna), 2 limited (Starlette Host-header in `redirect_slashes` 307
`Location`), 26 unreachable. CI `dependency-audit` job + `.github/audit-exceptions.toml` with
`review_by` dates that fail CI when past; **current exceptions: none**. Dependabot grouped updates.
Golden python-jose tokens verified by PyJWT both ways (`tests/test_jwt_compat.py`), so no forced
logout on deploy/rollback.

**Residual risk:** Starlette 1.x and PyJWT never ran on FastAPI Cloud's runtime (lifespan, static
serving of `/assets`, `redirect_slashes`, proxy headers). Transitive dependencies unpinned (L-1: 26
pinned of 52 installed) → CI and prod may resolve different versions. Bootstrap `pip` 25.0.1 in the
venv carries advisories (not an app dependency).

**Staging smoke (required, S):** on a preview against a Neon branch — `/` SPA + deep link,
`/assets/*` cache headers, `/docs` + `/redoc` render, `/rides` → 307 with a sane `Location`, login
with a pre-upgrade (jose-issued) token, register/login/book/accept flow, `/healthz` `/readyz`,
413 body limit, 422 sanitised, headers on every path.

**Exceptions list:** empty; any new exception needs id, package, reason, `review_by` ≤ 60 days.
**Severity:** Medium. **Launch blocker:** the staging smoke is. **Effort:** S (2 h). **Owner:** Hark
(run, with approval for preview), Maaz (approve preview). Follow-up: `constraints.txt` lock (S).

---

## 9. Backups and restore

**Assumptions — all UNVERIFIED** (no dashboard access): production DB is Neon; plan unknown;
history-retention (PITR) window unknown — Neon Free caps at 6 h, paid plans are configurable up to
days (`docs/operations/BACKUP_RESTORE.md` §1, citing neon.com docs read 31 Oct ~02:30 PKT);
restore = instant restore of a branch to a timestamp, or child branch at a point in time; no logical
`pg_dump` scheduled; no restore drill ever performed.

**Risk.** A bad migration, a bad sweep, or an operator `UPDATE` discovered after the PITR window
is unrecoverable. With a 6 h window, a Friday-night problem found Saturday morning may be gone.

**Severity:** High. **Launch blocker:** Yes — (1) confirm plan + window ≥ 24 h (proposed RPO 24 h /
RTO 1 h), or add a daily `pg_dump` to encrypted storage; (2) one restore drill D1–D9 on a branch
before real data. **Effort:** S (1–2 h). **Owner:** Maaz.

---

## 10. Production migration rollback, per revision

Chain (single head): `d924a6e38f10` (prod, assumed) → `e3a7c41b9f52` (#3) → `f6b2d8e0c413` (#5) →
`c1a7e0b00001` (#18) → `c1a7e0b00002` (#20) → `c1a7e0b00003` (#22) → `d5e1a0b00001` (#19) →
`d5e1a0b00002` (#21). `alembic/env.py` uses `transaction_per_migration=True`, so a multi-step
upgrade or downgrade stops part-way at a committed revision on failure.

| Revision | Deploy order | Downgrade | Data effect of downgrade | Traps |
|---|---|---|---|---|
| `e3a7` FK indexes | either | drops 3 indexes concurrently | none | CONCURRENTLY waits on long transactions |
| `f6b2` unique `lower(email)` | **code first** (new code tolerates no index; old code + index → 500 on case-variant register) | drop index | none | Preflight `case_duplicate_groups = 0` must pass **before code deploy** (M-1); rollback = downgrade **before** redeploying old code |
| `c1a7…01` idempotency keys | **migrate before code** (keyed requests 500 without the table) | drop table | stored replays lost (harmless) | downgrade only **after** code revert |
| `c1a7…02` pending expiry | **migrate before code** (code selects `expires_at`); frontend `expired` label first | maps `expired → cancelled`, drops index + column | `expired` history becomes `cancelled`; enum label stays | stop the sweep first; run the remap UPDATE, revert code, remap again, then downgrade (PR #20 Rollback 1–4) |
| `c1a7…03` multi-seat | migrate (default 1, old code fine) → code → optional `BOOKING_MAX_SEATS=1` start | **refuses** while any booking has `seats_requested <> 1` (`MultiSeatDowngradeRefused`) | dropping the column would erase sold-seat counts; old code restores 1 seat per N-seat cancel | preferred rollback: `BOOKING_MAX_SEATS=1`, resolve live multi-seat bookings, revert code, **keep the column** |
| `d5e1…01` suspension + `tv` | migrate → code (missing `tv` = 0) | drops `account_status`, `token_version` | **every suspended user becomes active**; export `SELECT id FROM users WHERE account_status='suspended'` first | ordering: downgrading from head below `c1a7…03` while multi-seat rows exist stops at `c1a7…03` *after* the account columns are dropped (per-revision commits) |
| `d5e1…02` audit log | migrate → code | drops table + triggers | **all audit history deleted**; `\copy admin_audit_log TO … CSV HEADER` first | after role split, downgrade needs the migrator role |

**d5e1 ordering:** `d5e1a0b00001.down_revision` was temporarily `f6b2` and has been re-chained onto
`c1a7e0b00003` (CLAIMS 08:27; MERGE order #18 → #20 → #22 → #19 → #21). Any reorder of these PRs must
keep that chain; `tests/test_migrations.py::HEAD_REVISION` must match.

**Recommended production order for the RC (to rehearse on a Neon branch first):**
- Every revision after `f6b2` is additive and safe for the *currently deployed* code (new columns have
  server defaults, the new table and the `expired` label are unused by old code). The only
  old-code incompatibility is `f6b2`: old code + `uq_users_email_lower` turns a case-variant duplicate
  registration into a 500 instead of a 409.
- **Option A (recommended):** preflight (`case_duplicate_groups = 0`) → Neon restore point →
  `alembic upgrade head` with the old code still serving → deploy the RC build → smoke. Accepted
  exposure: minutes of possible 500s on case-variant duplicate sign-ups only.
- **Option B (zero exposure, two deploys):** `alembic upgrade e3a7c41b9f52` → deploy an intermediate
  build containing #5 but none of #18–#22 → `alembic upgrade head` → deploy the RC build.
- **RC rollback:** revert code first (previous image), *then* downgrade only what must go, in reverse
  order, honouring the per-revision notes above (stop the sweep; export suspended ids and the audit log;
  never downgrade `c1a7…03` while multi-seat rows exist).

**Severity:** High. **Launch blocker:** Yes — full-chain upgrade/downgrade/re-upgrade rehearsal on a
Neon branch of production with the real data shape. **Effort:** M (½ d). **Owner:** Hark (runbook,
local rehearsal done for each PR), Maaz (approve Neon branch + preview).

---

## 11. Monitoring and incident response

**Evidence**
- Health (#7 `health.py`): `/healthz` liveness (no I/O), `/readyz` readiness with per-dependency
  deadline `READINESS_TIMEOUT_SECONDS` (DB `SELECT 1`; Redis only when rate limiting enabled;
  `{"status":"ready","checks":{"database":"ok","redis":"disabled"}}`), `Cache-Control: no-store`.
  **Prod today: both 404; `/test-db` 200** (old probe, publicly reachable, reveals DB connectivity).
- Request IDs (#15 `http_safety.RequestIdMiddleware`): accepts a well-formed inbound `X-Request-ID`
  or mints a UUID4, echoes it on every response, stores it in a ContextVar; `SafeErrorMiddleware`
  returns a generic 500 body with the request ID and logs one JSON line (`log_unhandled`: request_id,
  path, method, exception class, SQLSTATE, constraint, app frame — never `str(exc)`).
- **Structured logging gap:** only the error path is JSON. No access/business logs (login success/
  failure counts, 409/429 rates, job outcomes), no log shipping beyond FastAPI Cloud's own log view
  (https://fastapicloud.com/docs/monitoring-and-performance/logs, `fastapi cloud logs`), no metrics
  export. Uvicorn access log format is plain text and includes query strings.
- Alerting: none configured (no uptime monitor, no error-rate alert). FastAPI Cloud has a Metrics page
  and a Logfire integration listed in its docs (not evaluated).
- Runbooks exist (PR #11): `INCIDENT_RESPONSE.md` (severities, first 15 min, playbooks for down / bad
  deploy / leaked secret / credential stuffing / Redis outage / PII exposure), `ROLLBACK.md`,
  `LOGGING_MONITORING.md` (never-log table, alert thresholds), `SMOKE_TESTS.md`. Note: FastAPI Cloud
  marks "Rollbacks" as **Coming Soon** in its docs nav (https://fastapicloud.com/docs/) — rollback is
  "redeploy the previous commit", which uses *current* env vars (ROLLBACK.md).

**Severity:** Medium. **Launch blocker:** Yes for: an external uptime monitor on `/readyz` (1-min,
alert after 2 failures) and an error-rate signal (5xx > 1 % over 5 min, or any `rideshare.errors`
line) routed to Maaz's phone/email; repoint any existing monitor from `/test-db` to `/readyz`.
**Fix:** S for the monitor (Maaz, external service such as UptimeRobot/Better Stack — vendor choice is
Maaz's); M for structured access logging + job heartbeat metrics (Hark, follow-up PR touching `main.py`).
**Owner:** Maaz (monitor + routing), Hark (logging PR, BACKGROUND_JOBS heartbeat).

---

## 12. Things checked and found sound (no action)

- Algorithm pinning, claim validation, role re-check against DB, `tv` revocation on suspend/reinstate.
- Uniform login failure and timing (dummy hash) — D6 default keeps suspension invisible.
- Admin guard enforced structurally + by test; self-action 403; masked admin view; view is audited.
- 422 sanitisation (`validation_errors.py`), bidi/zero-width rejection in names (#8 L-5).
- Booking concurrency: seat accounting only through `lifecycle.apply_booking_transition`, guarded by
  `test_no_seat_arithmetic_outside_lifecycle`; lock order Key → User → Ride → Booking.
- CORS: exact origins, no credentials, `Idempotency-Key` allowed / `Idempotent-Replayed` exposed (#24).

## 13. Not verified (needs dashboard or production access)

FastAPI Cloud: deploy branch, auto-deploy, replica bounds / scale-to-zero, Uvicorn proxy-header
settings, env var values. Neon: plan, PITR window, role ownership, pooled vs direct URL in use.
GitHub: secret scanning / push protection status, branch protection required checks. Whether prod
ever ran `main`'s code. The live production schema revision (`alembic_version`).
