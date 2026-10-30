# Incident response

Team size today: one owner (Maaz). "Incident commander", "operator" and
"communicator" are all Maaz unless a collaborator is named. Production changes
during an incident (redeploy, env change, migration, Neon restore, secret rotation)
still need Maaz's explicit go-ahead; record the time of each decision.

## 1. Severity

| Sev | Meaning | Examples | Response |
|---|---|---|---|
| 1 | Users' data or accounts at risk, or service fully down | leaked `SECRET_KEY`/DB credentials, PII exposure, DB deleted, site 5xx for everyone | immediately, until contained |
| 2 | Core journey broken for many | login fails, booking fails, `/readyz` 503 | within 1 h |
| 3 | Degraded / partial | slow responses, one page broken, CSP blocks a non-critical asset | same day |
| 4 | Cosmetic / no user impact | | next working session |

## 2. First 15 minutes (any Sev 1–2)

1. **Confirm** from outside: `curl -s -o /dev/null -w '%{http_code}\n' https://rideshare-api.fastapicloud.dev/healthz` (after PR #7; `/test-db` before), plus `/` and `/readyz`.
2. **Open an incident note** (private): start time (PKT and UTC), symptom, last deploy/config change, who is acting.
3. **What changed?** FastAPI Cloud deployments list, env-var history, Neon operations log, latest merges to the deployed branch.
4. **Stabilise before diagnosing**: if a deploy/config change in the last few hours lines up with the start, roll it back ([ROLLBACK](ROLLBACK.md)).
5. **Preserve evidence**: export relevant logs/time ranges before they rotate; do not copy secrets or PII into the note.

## 3. Playbooks

### Service down / 5xx
- Platform status pages (FastAPI Cloud, Cloudflare, Neon). If a provider is down, wait and communicate.
- `/healthz` 200 but `/readyz` 503 → database (or Redis if rate limiting is enabled). Neon console: compute state, limits/quota on Free plan, connection count.
- Startup failure after a config change → the process exits on invalid config by design (`RuntimeError` naming the variable). Fix or revert the variable.

### Bad deploy
- Redeploy the last good deployment (§2 of ROLLBACK). Check migration compatibility first.

### Bad migration
- Lock waits: find blockers via `pg_stat_activity`; a `CREATE INDEX CONCURRENTLY` waits for old transactions; cancel the migration rather than killing app sessions blindly.
- Failed half-way: re-run `alembic upgrade head`; otherwise downgrade per the release plan.
- Data damaged: [BACKUP_RESTORE](BACKUP_RESTORE.md) §3 (selective first).

### Leaked secret (Sev 1)
Order matters; full detail in [AST-002 plan §6](../security/AST-002_REMEDIATION_PLAN.md).
1. **Database credentials**: Neon → Roles → reset the app role's password (or create a new role), update `DATABASE_URL` in FastAPI Cloud, redeploy, verify `/readyz`. Old connections drop.
2. **`SECRET_KEY`**: generate a new key, update, redeploy. **All existing JWTs become invalid**; every user must log in again (tokens are in memory, so most already re-login on reload). This also kills any forged tokens.
3. **`AUTH_RATE_LIMIT_HMAC_KEY` / Redis password** (if enabled): rotate; counters reset.
4. Remove the secret from wherever it leaked (file, log, issue). If in git history, see AST-002 for history rewriting trade-offs; rotation is what actually closes the hole.
5. Review DB activity for the exposure window (Neon logs/metrics; unusual users, bulk reads) and decide on user notification.

### Credential stuffing / registration spam
- Today there is no throttling in production. Symptoms: spike in `/auth/login` 401s or many new accounts.
- Short term: identify the scale from logs; if accounts were taken over, rotate `SECRET_KEY` to log everyone out and force password resets once a reset flow exists (AST-012, not built).
- Medium term: complete [REDIS_RATE_LIMIT_PREREQUISITES](REDIS_RATE_LIMIT_PREREQUISITES.md) and enable (approval).
- Cloudflare in front belongs to the platform; we cannot add WAF rules ourselves unless a custom domain on our own Cloudflare account is used.

### Redis outage (only once rate limiting is enabled)
- Login/register return 503; `/readyz` 503. If the provider will not recover quickly: `AUTH_RATE_LIMIT_ENABLED=false` (approval), restart, re-enable after recovery.

### Personal-data exposure (CNIC, phone, email)
- Contain (fix the leak, revoke access, rotate credentials).
- Establish what was exposed, for which user IDs, for how long; work with counts and IDs, not values.
- Notify affected users plainly and promptly; take legal advice on obligations under Pakistani law (e.g. PECA 2016 and any data-protection law in force at the time). This document is not legal advice.
- Remember backups, Neon branches and dumps also hold the data.

### Unsafe driver/passenger report (product safety)
- Out of scope for infra, but the account must be blockable: there is no suspension flag yet (audit S8; M5). Interim: rotate `SECRET_KEY` logs everyone out; a DB-level fix by user ID needs approval.

## 4. Communication

- Status note on the app/landing page or social profile for Sev 1–2 with: what is affected, workaround, next update time. No technical blame, no secrets, no user data.
- Direct message to affected users for data incidents.

## 5. Closing

- Confirm with smoke tests ([SMOKE_TESTS](SMOKE_TESTS.md) §2) and 30 minutes of normal error rates.
- Delete temporary branches/previews created during the incident (keep restore backups 7 days).

## 6. Post-incident note (within 48 h, blameless)

```
Title / Sev / Start–end (PKT + UTC) / Detected by
Impact: users affected, data affected (counts only)
Timeline
Root cause
What went well / what was hard
Actions: owner, due date (tests, alerts, docs)
```
