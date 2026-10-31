# Operations documentation

Nothing here deploys, migrates or changes configuration by itself. Actions marked
as approval-gated need the repository owner's explicit go-ahead.

| Document | Use it when |
|---|---|
| [PRODUCTION_CONFIG_CHECKLIST](PRODUCTION_CONFIG_CHECKLIST.md) | reviewing production settings, `/docs` exposure, CORS, cookies/HSTS |
| [../ENVIRONMENT.md](../ENVIRONMENT.md) | looking up any environment variable |
| [SMOKE_TESTS](SMOKE_TESTS.md) | after every staging or production deploy/rollback |
| [MIGRATION_REHEARSAL](MIGRATION_REHEARSAL.md) | before any Alembic migration reaches production |
| [BACKUP_RESTORE](BACKUP_RESTORE.md) | before risky changes; recovering data; quarterly drill |
| [ROLLBACK](ROLLBACK.md) | a release misbehaves |
| [INCIDENT_RESPONSE](INCIDENT_RESPONSE.md) | something is broken or leaked |
| [LOGGING_MONITORING](LOGGING_MONITORING.md) | implementing logging, error tracking, alerts; what never to log |
| [SECURITY_HEADERS_CHECKLIST](SECURITY_HEADERS_CHECKLIST.md) | validating PR #6 headers; before enabling HSTS |
| [REDIS_RATE_LIMIT_PREREQUISITES](REDIS_RATE_LIMIT_PREREQUISITES.md) | before enabling auth rate limiting |
| `HEALTH_CHECKS.md` (added by PR #7) | `/healthz` and `/readyz` semantics and probe settings |
| [../security/AST-002_REMEDIATION_PLAN.md](../security/AST-002_REMEDIATION_PLAN.md) | stale `main`, historical `.env`, secret rotation |

Related: `docs/security/DEPENDENCY_AUDIT_2026-10-31.md` (PR #10, dependency report),
`docs/privacy/CNIC_PHONE_PROTECTION.md` (PR #8), `DEPLOYMENT.md` (build and deploy path).

Release-candidate review (31 Oct 2026): [docs/release](../release/README.md) — security review, background jobs, launch blockers.
