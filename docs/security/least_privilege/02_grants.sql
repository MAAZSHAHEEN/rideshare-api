-- PROPOSAL ONLY. Run AS rideshare_migrator (the owner) after 01_roles_and_ownership.sql.
-- tests/test_db_least_privilege.py executes this file verbatim (role names suffixed) in a
-- disposable database and proves every tamper attempt by the app role fails.

-- Schema: nobody but the owner creates objects.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rideshare_app, rideshare_jobs, rideshare_admin_ops;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;

-- API (FastAPI Cloud DATABASE_URL). users: registration inserts; suspension/reinstatement
-- update status + token_version. role, cnic, id and created_at are NOT updatable by the API.
GRANT SELECT, INSERT ON users TO rideshare_app;
GRANT UPDATE (name, email, password, phone_number, updated_at, account_status, token_version)
    ON users TO rideshare_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON rides, bookings, idempotency_keys TO rideshare_app;
-- Audit log: append-only for the API.
GRANT SELECT, INSERT ON admin_audit_log TO rideshare_app;
GRANT SELECT ON alembic_version TO rideshare_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rideshare_app;   -- nextval only, no setval

-- Background jobs (jobs.expire_bookings, jobs.purge_idempotency_keys): no users, no audit log.
GRANT SELECT, UPDATE ON rides, bookings TO rideshare_jobs;
GRANT SELECT, DELETE ON idempotency_keys TO rideshare_jobs;

-- Operator admin grants (python -m scripts.grant_admin): read users, change role/token_version,
-- append audit rows. Nothing else.
GRANT SELECT ON users TO rideshare_admin_ops;
GRANT UPDATE (role, token_version, updated_at) ON users TO rideshare_admin_ops;
GRANT SELECT, INSERT ON admin_audit_log TO rideshare_admin_ops;
GRANT USAGE ON SEQUENCE admin_audit_log_id_seq TO rideshare_admin_ops;

-- Belt and braces: no role but the owner may change or wipe the audit log, or hang a trigger on it.
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON admin_audit_log
    FROM rideshare_app, rideshare_jobs, rideshare_admin_ops;

-- Future tables created by migrations (as the migrator) get API DML automatically. A new
-- append-only table must REVOKE UPDATE, DELETE, TRUNCATE in its own migration.
ALTER DEFAULT PRIVILEGES FOR ROLE rideshare_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rideshare_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rideshare_migrator IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO rideshare_app;
