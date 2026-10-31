# Release-candidate readiness (31 Oct 2026)

- [SECURITY_REVIEW_RC.md](SECURITY_REVIEW_RC.md) — security & SRE findings with evidence, severity, launch-blocker flag, fix, effort, owner.
- [LAUNCH_BLOCKERS.md](LAUNCH_BLOCKERS.md) — every open launch blocker and the staging prerequisites checklist.
- [MIGRATION_READINESS.md](MIGRATION_READINESS.md) — chain from the presumed production baseline `d924a6e38f10` to `d5e1a0b00002`, how to verify the baseline, RC rehearsal (30/30 incl. kill -9 at every revision and repeat execution), irreversible boundaries, rollback per revision, the single recommended production sequence.
- [PREFLIGHT_REVIEW.md](PREFLIGHT_REVIEW.md) — line-by-line review of the AST-005 email preflight; fixed copy [sql/prod_email_preflight.sql](sql/prod_email_preflight.sql) (rev 2) and its self-test.
- [NEON_STAGING_PLAN.md](NEON_STAGING_PLAN.md) — branch-with-data vs schema-only, isolation, [sql/staging_sanitize.sql](sql/staging_sanitize.sql), deletion, costs/limits, preview env.
- [BACKUP_RESTORE_TEST.md](BACKUP_RESTORE_TEST.md) — restore drills with [sql/restore_verify.sql](sql/restore_verify.sql), RPO/RTO, quarterly cadence, `pg_dump` alternative.
- [STAGING_SMOKE_CHECKLIST.md](STAGING_SMOKE_CHECKLIST.md) — Starlette 1.x / PyJWT specifics and exact curl + Playwright steps for the preview.
- [BACKGROUND_JOBS.md](BACKGROUND_JOBS.md) and [SCHEDULER_AND_OPS.md](SCHEDULER_AND_OPS.md) — scheduler decision, recovery evidence from PR #26, retries, alerting, RACI, [workflows/maintenance-jobs.yml](workflows/maintenance-jobs.yml) (inactive proposal).
- [PRIVACY_CNIC_PHONE.md](PRIVACY_CNIC_PHONE.md) — plaintext CNIC/phone risk, Pakistani legal context, envelope-encryption migration plan after `d5e1a0b00002`.
- [SECRET_ROTATION_CHECKLIST.md](SECRET_ROTATION_CHECKLIST.md) — gitleaks over all refs incl. PR refs (redacted findings) and the rotation checklist.

Docs only. Nothing here was run against production, staging or Neon; nothing is scheduled; nothing was rotated.
SQL and tools under `sql/`, `tools/` and `workflows/` were exercised on disposable local databases only.
