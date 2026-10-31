-- PROPOSAL ONLY — NOT EXECUTED ANYWHERE REAL. Needs Maaz's approval and a Neon change.
-- Rehearse on a disposable Neon branch first (docs/security/DB_LEAST_PRIVILEGE.md §6).
-- Run as the CURRENT owner of the database objects (Neon: neondb_owner, called
-- rideshare_owner below). Passwords are set in the Neon console / with \password,
-- never written in this file, CI, or chat.

-- 1. Roles. NOLOGIN for the owner-only migrator is possible if migrations run via SET ROLE;
--    LOGIN keeps the deploy runbook simple (one URL per role).
CREATE ROLE rideshare_migrator  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE rideshare_app       LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
CREATE ROLE rideshare_jobs      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
CREATE ROLE rideshare_admin_ops LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;

-- 2. Ownership moves to the migrator: only the owner can ALTER/DROP a table, disable or drop
--    its triggers, or replace its trigger functions. Review `\dt` / `\df` output first.
ALTER SCHEMA public OWNER TO rideshare_migrator;
REASSIGN OWNED BY rideshare_owner TO rideshare_migrator;

-- 3. Then run 02_grants.sql AS rideshare_migrator, then (after its migration is approved)
--    03_admin_role_guard.sql as part of an Alembic revision.
