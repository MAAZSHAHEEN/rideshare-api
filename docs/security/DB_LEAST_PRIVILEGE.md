# Database least privilege: proposed role split

Status: **PROPOSAL ONLY — nothing executed on Neon or production.** Executing it needs Maaz's
approval and a Neon change, rehearsed on a disposable Neon branch first. Refines the sketch in
`docs/release/SECURITY_REVIEW_RC.md` §5 (finding S5) with column-level grants, an operator role
for admin grants, and a guard trigger. The SQL lives in `docs/security/least_privilege/` and is
**executed verbatim by `tests/test_db_least_privilege.py`** in a throwaway database, so the
document and the proof cannot drift.

## 1. Why

Today the API connects with the role that owns every table (inferred from the single
`DATABASE_URL`). The owner can `ALTER TABLE admin_audit_log DISABLE TRIGGER ALL`, drop the
table, or `UPDATE users SET role = 'admin'`. The audit triggers then stop accidents and app bugs,
not a stolen API credential.

## 2. Roles

| Role | Used by | Can | Cannot |
|---|---|---|---|
| `rideshare_owner` (Neon `neondb_owner` today) | Neon console only | everything; creates the roles | — (kept out of every URL after the split) |
| `rideshare_migrator` | `alembic upgrade` in the deploy runbook | owns schema `public` and every object; DDL | not superuser, no CREATEROLE/CREATEDB/BYPASSRLS |
| `rideshare_app` | the API (FastAPI Cloud `DATABASE_URL`) | `users`: SELECT, INSERT, UPDATE of `name, email, password, phone_number, updated_at, account_status, token_version`; DML on `rides, bookings, idempotency_keys`; `admin_audit_log`: SELECT, INSERT; `alembic_version`: SELECT; `nextval` on sequences | UPDATE/DELETE/TRUNCATE the audit log; disable/drop/create triggers; replace trigger functions; any DDL; `session_replication_role`; `setval`; UPDATE `users.role`/`cnic`; INSERT a user with role `admin` (guard trigger) |
| `rideshare_jobs` | `jobs.expire_bookings`, `jobs.purge_idempotency_keys` | SELECT, UPDATE `rides, bookings`; SELECT, DELETE `idempotency_keys` | anything on `users` or the audit log |
| `rideshare_admin_ops` | `python -m scripts.grant_admin` (`ADMIN_BOOTSTRAP.md`) | SELECT `users`; UPDATE `role, token_version, updated_at`; SELECT, INSERT audit log | DELETE/UPDATE audit rows; other user columns; rides/bookings |
| `rideshare_readonly` (optional) | support/analytics | as in SECURITY_REVIEW_RC §5 step 6 (no raw PII columns) | writes |

All non-owner roles: `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`, and `NOINHERIT`
for the login roles that must never pick up another role's rights.

## 3. Exact SQL

1. [`least_privilege/01_roles_and_ownership.sql`](least_privilege/01_roles_and_ownership.sql) — as the
   current owner: `CREATE ROLE …`, `ALTER SCHEMA public OWNER TO rideshare_migrator`,
   `REASSIGN OWNED BY rideshare_owner TO rideshare_migrator`. (Not run by the test, which creates
   the objects as the migrator directly — the same end state.)
2. [`least_privilege/02_grants.sql`](least_privilege/02_grants.sql) — as `rideshare_migrator`: every
   GRANT/REVOKE above, plus default privileges:
   ```sql
   ALTER DEFAULT PRIVILEGES FOR ROLE rideshare_migrator IN SCHEMA public
       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rideshare_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE rideshare_migrator IN SCHEMA public
       GRANT USAGE ON SEQUENCES TO rideshare_app;
   ```
   A future append-only table must `REVOKE UPDATE, DELETE, TRUNCATE` in its own migration, and a
   future table the jobs need must grant `rideshare_jobs` explicitly (no default for jobs).
3. [`least_privilege/03_admin_role_guard.sql`](least_privilege/03_admin_role_guard.sql) — trigger
   `users_admin_role_guard`: a row that becomes `admin` (INSERT, or UPDATE of `role`) is rejected
   with SQLSTATE 42501 unless `current_user` is a member of `rideshare_admin_ops`. Column grants
   cannot limit INSERT values, so without it a stolen API credential could insert a new admin.
   This one is schema, so it would ship as its own approval-gated Alembic revision.

## 4. Proof (disposable database, automated)

`tests/test_db_least_privilege.py` (skips unless `TEST_DATABASE_URL` is a superuser, which CI's
postgres service user and the sandbox user are). It creates suffixed roles and a throwaway DB,
builds the schema as the migrator, runs 02 and 03 verbatim, then as **`rideshare_app`** every
one of these fails with SQLSTATE 42501:

`UPDATE admin_audit_log …` (×2), `DELETE FROM admin_audit_log`, `TRUNCATE admin_audit_log`
(± CASCADE), `ALTER TABLE admin_audit_log DISABLE TRIGGER ALL | <name> | USER`,
`DROP TRIGGER …`, `DROP TABLE admin_audit_log`, `ALTER TABLE … OWNER TO`,
`DROP FUNCTION admin_audit_log_immutable() CASCADE`, `CREATE OR REPLACE FUNCTION
admin_audit_log_immutable() …`, `CREATE TRIGGER … ON admin_audit_log`,
`SET session_replication_role = replica`, `SET ROLE rideshare_migrator`,
`SELECT setval('admin_audit_log_id_seq', 1)`, `CREATE TABLE public.…`,
`ALTER TABLE users DISABLE TRIGGER users_admin_role_guard`,
`UPDATE users SET role = 'admin'`, `UPDATE users SET cnic = …`,
`INSERT INTO users (…, role) VALUES (…, 'admin')`, `UPDATE alembic_version …`.

…while the API's real writes still succeed (registration insert, `FOR NO KEY UPDATE` +
suspension update, ride insert/update, audit INSERT, `alembic_version` read). Afterwards, as
superuser: the audit rows are unchanged, both triggers are still enabled (`tgenabled = 'O'`),
the owner is still the migrator. `rideshare_jobs` cannot read `users` or touch the audit log.
`scripts.grant_admin` fails as the app role (permission denied, rolled back) and succeeds as
`rideshare_admin_ops`, which still cannot delete or edit audit rows.

**Residual risk:** a real superuser can still `SET session_replication_role = replica` and skip
triggers (confirmed in RC-stress M3). On Neon, `neondb_owner` is not a true superuser but owns
objects, hence the ownership move. Keep the owner/migrator credentials out of the app runtime.

## 5. Alembic and the deploy

- Migrations must run as `rideshare_migrator` (owner): `alembic/env.py` reads `DATABASE_URL`,
  so the runbook sets it to the migrator URL **for the `alembic upgrade` step only**, e.g.
  `DATABASE_URL="$MIGRATOR_DATABASE_URL" alembic upgrade head`. (Optional follow-up PR: make
  `env.py` prefer `MIGRATION_DATABASE_URL`.)
- Objects created by later migrations are owned by the migrator and pick up the default privileges.
- The two jobs get their own URL (`rideshare_jobs`) in the scheduler's secrets; advisory locks
  need no grant; use the direct (non-pooler) Neon host for session advisory locks.

## 6. How FastAPI Cloud's `DATABASE_URL` would change (proposal)

| | Before | After |
|---|---|---|
| FastAPI Cloud `DATABASE_URL` | owner role | `postgresql+asyncpg://rideshare_app:<set in Neon console>@<neon host>/<db>?ssl=require` |
| Deploy runbook migration step | same URL | migrator URL, from the operator's shell / a protected CI secret, never in the app env |
| Job scheduler secret | n/a | `rideshare_jobs` URL |
| Admin grants | manual SQL (unaudited) | `rideshare_admin_ops` URL in the operator's shell, `scripts.grant_admin` |

Rollout (all approval-gated, Maaz executes): Neon branch → run 01/02/03 → point a staging app at
the `rideshare_app` URL → run the full backend suite and the RC acceptance specs against it →
then production in a quiet window: create roles, reassign, grants, switch the FastAPI Cloud
`DATABASE_URL`, smoke `/readyz` + login + one booking. **Rollback:** switch `DATABASE_URL` back to
the owner URL (instant); the grants and roles can stay. Passwords are set in the Neon console,
never in SQL files, CI logs or chat. Effort: S (½ day incl. rehearsal).
