# Rollback

Any rollback in production (redeploy, config change, Alembic downgrade, Neon
restore) is an approval-gated action. In a live incident, Maaz approves in the
incident channel; record who approved and when.

## 1. Decide what to roll back

| Symptom after a release | First action |
|---|---|
| 5xx / broken pages, no schema change in the release | **Code rollback** (§2) |
| Broken behaviour caused by a feature with a flag | **Config rollback** (§3) |
| Errors tied to a new migration (lock waits, constraint violations, 500 on conflict) | Read the release's rollback order, then **schema rollback** (§4) + code |
| Data deleted or corrupted | Stop writes if spreading; **data recovery** ([BACKUP_RESTORE](BACKUP_RESTORE.md) §3A, then B) |
| Leaked secret | Not a rollback: [INCIDENT_RESPONSE](INCIDENT_RESPONSE.md) secret playbook |

## 2. Code rollback (FastAPI Cloud)

From DEPLOYMENT.md and FastAPI Cloud's redeployment docs:

1. Before each release, record the last good deployment ID and its git SHA in the release PR.
2. Dashboard → Deployments → last good deployment → **Actions → Redeploy**. It reuses that build's image with the app's **current** environment variables (old variable values are *not* restored).
3. If the image is unavailable: check out the recorded SHA, rebuild the frontend (`npm ci && npm run build`), and `fastapi deploy . --app-id <verified-app-id>` (manual, approved).
4. Run [SMOKE_TESTS](SMOKE_TESTS.md) §2.

Compatibility rules:

- If the bad release added a migration, check whether the old code works on the new schema **before** redeploying. Wave 1: AST-005's `uq_users_email_lower` makes old code return 500 on a case-variant duplicate registration, so downgrade `f6b2d8e0c413` first; AST-003 indexes are compatible with old code.
- PR #7 removes `/test-db`. Rolling back past it brings `/test-db` back and removes `/healthz`/`/readyz`; switch any platform health check or uptime monitor accordingly.
- A backend older than Task #18 breaks the dual-mode frontend (`allowed_modes`); see DEPLOYMENT.md.

## 3. Config rollback (feature switches)

Each is an environment change in FastAPI Cloud followed by a restart/redeploy so the process re-reads it (all are read at import time).

| Switch | Rolls back | Caveat |
|---|---|---|
| `SECURITY_HEADERS_ENABLED=false` (PR #6) | All security headers incl. CSP | Restores the pre-PR #6 header set; fix the CSP and re-enable quickly. |
| `HSTS_ENABLED=false` (PR #6) | Stops *sending* HSTS | Browsers that already saw the header keep enforcing HTTPS for `max-age`. To clear, serve `HSTS_MAX_AGE=0` with HSTS enabled for a while. That is why HSTS starts at `max-age=300`. |
| `AUTH_RATE_LIMIT_ENABLED=false` | Login/register throttling (and the Redis dependency of `/readyz`) | Brute-force protection is gone again; use only to stop a Redis-caused login outage. |
| `FRONTEND_ORIGINS` | CORS allow-list | Restore the previous exact list. |

## 4. Schema rollback (Alembic)

1. Confirm the target: `alembic history --verbose | head`, `alembic current`.
2. Downgrade **one revision at a time**: `alembic downgrade <previous_revision>`. Never `downgrade base` on a database with data.
3. Wave 1 chain and order: `f6b2d8e0c413 → e3a7c41b9f52` (drops `uq_users_email_lower`; must happen before old code is redeployed), then if needed `e3a7c41b9f52 → d924a6e38f10` (drops the three indexes concurrently, with `IF EXISTS`). Neither deletes data (rehearsed locally, see the staging plan).
4. Re-check `alembic current` and smoke-test.

If a migration failed half-way: re-run `alembic upgrade head` first (Wave 1 revisions are re-runnable); downgrade only if the re-run cannot succeed.

## 5. Data rollback

Neon PITR or branch-based selective recovery: [BACKUP_RESTORE](BACKUP_RESTORE.md) §3. A full restore rewinds everything, including legitimate writes, and is the last resort.

## 6. After any rollback

- Keep the failing deployment/branch for diagnosis; do not delete restore branches for 7 days.
- Write a short post-incident note ([INCIDENT_RESPONSE](INCIDENT_RESPONSE.md) §6).
- Add a regression test before re-releasing.
