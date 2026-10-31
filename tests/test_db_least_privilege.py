"""Rehearsal of the proposed least-privilege role split (docs/security/DB_LEAST_PRIVILEGE.md).

Builds a throwaway database whose objects belong to a migrator role, applies
docs/security/least_privilege/02_grants.sql and 03_admin_role_guard.sql verbatim
(role names suffixed so parallel runs and real roles never collide), then logs
in as the app, jobs and admin-operator roles and proves:

- the app role cannot alter the audit trail by any route: UPDATE, DELETE,
  TRUNCATE, DISABLE TRIGGER, DROP TRIGGER/TABLE/FUNCTION, CREATE OR REPLACE of
  the trigger function, OWNER change, session_replication_role, SET ROLE,
  setval on the audit sequence;
- the app role cannot make anyone an admin (column grant + guard trigger);
- normal API writes still work;
- scripts.grant_admin works only as the operator role.

Skips unless TEST_DATABASE_URL connects as a role that can create roles and
databases (CI's postgres service user and the local sandbox user are superusers).
Every role and database it creates is dropped afterwards. Nothing here touches
a real database.
"""

import os
import re
import secrets
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "least-privilege-tests-only-not-a-production-key",
}):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    import models  # noqa: F401  (registers the tables on Base.metadata)
    from database import Base
    from scripts import grant_admin

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "docs" / "security" / "least_privilege"
ROLES = ("rideshare_migrator", "rideshare_app", "rideshare_jobs", "rideshare_admin_ops")
DENIED = "42501"  # insufficient_privilege (also "must be owner")


def _dsn(url):
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class LeastPrivilegeRehearsal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.super = await asyncpg.connect(_dsn(TEST_DATABASE_URL))
        self.addAsyncCleanup(self.super.close)
        can = await self.super.fetchrow(
            "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user")
        if not can["rolsuper"]:
            self.skipTest("needs a superuser TEST_DATABASE_URL to create disposable roles")

        suffix = uuid4().hex[:10]
        self.names = {role: f"{role}_t{suffix}" for role in ROLES}
        self.passwords = {role: secrets.token_urlsafe(24) for role in ROLES}  # disposable
        self.db = f"test_least_priv_{suffix}"
        for role in ROLES:
            await self.super.execute(
                f'CREATE ROLE "{self.names[role]}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                f"NOBYPASSRLS PASSWORD '{self.passwords[role]}'")
        self.addAsyncCleanup(self.drop_everything)
        await self.super.execute(f'CREATE DATABASE "{self.db}" OWNER "{self.names["rideshare_migrator"]}"')

        base = TEST_DATABASE_URL.rsplit("/", 1)[0]
        host_part = base.split("@", 1)[1]
        self.urls = {role: f"postgresql+asyncpg://{self.names[role]}:{self.passwords[role]}"
                           f"@{host_part}/{self.db}" for role in ROLES}
        self.super_db_url = f"{base}/{self.db}"

        su = await asyncpg.connect(_dsn(self.super_db_url))
        try:
            await su.execute(f'ALTER SCHEMA public OWNER TO "{self.names["rideshare_migrator"]}"')
        finally:
            await su.close()

        # Schema built by the migrator, so the migrator owns every object (as after REASSIGN).
        engine = create_async_engine(self.urls["rideshare_migrator"], poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
        migrator = await self.connect("rideshare_migrator")
        try:
            await migrator.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            await migrator.execute("INSERT INTO alembic_version VALUES ('d5e1a0b00002')")
            await migrator.execute(self.sql("02_grants.sql"))
            await migrator.execute(self.sql("03_admin_role_guard.sql"))
            self.passenger_id = await migrator.fetchval(
                "INSERT INTO users (name, email, password, cnic, phone_number, role, "
                "account_status, token_version) VALUES ('Test Rider', 'rider@example.com', 'x', "
                "'1234500000881', '+923000000881', 'passenger', 'active', 0) RETURNING id")
            self.audit_id = await migrator.fetchval(
                "INSERT INTO admin_audit_log (actor_id, action, target_type, target_id) "
                "VALUES (NULL, 'user.view', 'user', $1) RETURNING id", self.passenger_id)
        finally:
            await migrator.close()

    def sql(self, filename):
        text = (SQL_DIR / filename).read_text()
        for role in sorted(ROLES, key=len, reverse=True):
            text = re.sub(rf"\b{role}\b", self.names[role], text)
        return text

    async def connect(self, role):
        return await asyncpg.connect(_dsn(self.urls[role]))

    async def drop_everything(self):
        await self.super.execute(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)')
        for role in ROLES:
            await self.super.execute(f'DROP ROLE IF EXISTS "{self.names[role]}"')

    async def assert_denied(self, conn, statement, *args):
        with self.assertRaises(asyncpg.PostgresError, msg=statement) as raised:
            await conn.execute(statement, *args)
        self.assertEqual(raised.exception.sqlstate, DENIED, f"{statement}: {raised.exception}")
        return raised.exception

    async def test_app_role_cannot_tamper_with_the_audit_log(self):
        app = await self.connect("rideshare_app")
        self.addAsyncCleanup(app.close)
        roles = {k: f'"{v}"' for k, v in self.names.items()}
        attempts = [
            "UPDATE admin_audit_log SET reason = 'edited'",
            "UPDATE admin_audit_log SET metadata = '{}'::jsonb WHERE id = 1",
            "DELETE FROM admin_audit_log",
            "TRUNCATE admin_audit_log",
            "TRUNCATE admin_audit_log CASCADE",
            "ALTER TABLE admin_audit_log DISABLE TRIGGER ALL",
            "ALTER TABLE admin_audit_log DISABLE TRIGGER admin_audit_log_no_update_delete",
            "ALTER TABLE admin_audit_log DISABLE TRIGGER USER",
            "DROP TRIGGER admin_audit_log_no_update_delete ON admin_audit_log",
            "DROP TABLE admin_audit_log",
            f"ALTER TABLE admin_audit_log OWNER TO {roles['rideshare_app']}",
            "DROP FUNCTION admin_audit_log_immutable() CASCADE",
            "CREATE OR REPLACE FUNCTION admin_audit_log_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$",
            "CREATE TRIGGER sneaky BEFORE INSERT ON admin_audit_log FOR EACH ROW "
            "EXECUTE FUNCTION admin_audit_log_immutable()",
            "SET session_replication_role = replica",
            f"SET ROLE {roles['rideshare_migrator']}",
            "SELECT setval('admin_audit_log_id_seq', 1)",
            "CREATE TABLE public.shadow_audit (id int)",
            "ALTER TABLE users DISABLE TRIGGER users_admin_role_guard",
        ]
        for statement in attempts:
            with self.subTest(statement=statement):
                error = await self.assert_denied(app, statement)
                if statement.startswith(("UPDATE", "DELETE", "TRUNCATE")):
                    # The GRANT layer refuses first; the append-only trigger is a second layer
                    # (it would say "append-only"), so both must hold independently.
                    self.assertIn("permission denied", str(error))
        # Append-only use still works.
        new_id = await app.fetchval(
            "INSERT INTO admin_audit_log (actor_id, action, target_type, target_id, reason) "
            "VALUES (NULL, 'user.view', 'user', $1, 'app insert') RETURNING id", self.passenger_id)
        self.assertGreater(new_id, self.audit_id)
        self.assertEqual(await app.fetchval("SELECT count(*) FROM admin_audit_log"), 2)

        su = await asyncpg.connect(_dsn(self.super_db_url))
        try:
            rows = await su.fetch("SELECT id, reason, metadata FROM admin_audit_log ORDER BY id")
            self.assertEqual([(r["id"], r["reason"]) for r in rows],
                             [(self.audit_id, None), (new_id, "app insert")])
            enabled = await su.fetch(
                "SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'admin_audit_log'::regclass "
                "AND NOT tgisinternal ORDER BY tgname")
            self.assertEqual([(r["tgname"], r["tgenabled"]) for r in enabled],
                             [("admin_audit_log_no_truncate", b"O"),
                              ("admin_audit_log_no_update_delete", b"O")])
            owner = await su.fetchval("SELECT tableowner FROM pg_tables WHERE tablename = 'admin_audit_log'")
            self.assertEqual(owner, self.names["rideshare_migrator"])
        finally:
            await su.close()

    async def test_app_role_cannot_create_or_promote_admins_but_normal_writes_work(self):
        app = await self.connect("rideshare_app")
        self.addAsyncCleanup(app.close)
        await self.assert_denied(app, "UPDATE users SET role = 'admin' WHERE id = $1", self.passenger_id)
        await self.assert_denied(app, "UPDATE users SET cnic = '0000000000000' WHERE id = $1",
                                 self.passenger_id)
        await self.assert_denied(
            app, "INSERT INTO users (name, email, password, cnic, phone_number, role) VALUES "
                 "('Test Evil', 'evil@example.com', 'x', '1234500000882', '+923000000882', 'admin')")
        # What the API legitimately does:
        driver_id = await app.fetchval(
            "INSERT INTO users (name, email, password, cnic, phone_number, role) VALUES "
            "('Test Driver', 'drv@example.com', 'x', '1234500000883', '+923000000883', 'driver') "
            "RETURNING id")
        async with app.transaction():
            await app.execute("SELECT id FROM users WHERE id = $1 FOR NO KEY UPDATE", self.passenger_id)
            await app.execute("UPDATE users SET account_status = 'suspended', "
                              "token_version = token_version + 1 WHERE id = $1", self.passenger_id)
        ride_id = await app.fetchval(
            "INSERT INTO rides (driver_id, origin, destination, departure_time, available_seats, "
            "fare_per_seat, status) VALUES ($1, 'Lahore', 'Islamabad', now() + interval '1 day', 3, "
            "2000, 'active') RETURNING id", driver_id)
        await app.execute("UPDATE rides SET available_seats = 2 WHERE id = $1", ride_id)
        self.assertEqual(await app.fetchval("SELECT version_num FROM alembic_version"), "d5e1a0b00002")
        await self.assert_denied(app, "UPDATE alembic_version SET version_num = 'x'")

    async def test_jobs_role_is_confined_to_rides_bookings_and_keys(self):
        jobs = await self.connect("rideshare_jobs")
        self.addAsyncCleanup(jobs.close)
        for statement in ("SELECT count(*) FROM users", "SELECT count(*) FROM admin_audit_log",
                          "DELETE FROM admin_audit_log", "UPDATE users SET role = 'admin'",
                          "INSERT INTO admin_audit_log (action, target_type, target_id) "
                          "VALUES ('user.view', 'user', 1)"):
            with self.subTest(statement=statement):
                await self.assert_denied(jobs, statement)
        await jobs.execute("UPDATE bookings SET status = status WHERE false")
        await jobs.execute("DELETE FROM idempotency_keys WHERE false")

    async def test_grant_admin_cli_works_only_as_the_operator_role(self):
        args = grant_admin.build_parser().parse_args([
            "--bootstrap", "--email", "RIDER@example.com", "--reason", "Rehearsal grant only",
            "--confirm"])
        app_engine = create_async_engine(self.urls["rideshare_app"], poolclass=NullPool)
        self.addAsyncCleanup(app_engine.dispose)
        with self.assertRaises(Exception) as raised:
            await grant_admin.apply_change(app_engine, args, "Rehearsal grant only", None)
        self.assertIn("permission denied", str(raised.exception))

        ops_engine = create_async_engine(self.urls["rideshare_admin_ops"], poolclass=NullPool)
        self.addAsyncCleanup(ops_engine.dispose)
        result = await grant_admin.apply_change(ops_engine, args, "Rehearsal grant only", None)
        self.assertEqual((result["new_role"], result["token_version"]), ("admin", 1))

        ops = await self.connect("rideshare_admin_ops")
        self.addAsyncCleanup(ops.close)
        self.assertEqual(await ops.fetchval("SELECT count(*) FROM admin_audit_log "
                                            "WHERE action = 'admin_granted'"), 1)
        for statement in ("DELETE FROM admin_audit_log", "UPDATE admin_audit_log SET reason = 'x'",
                          "UPDATE users SET cnic = '0' WHERE id = 1",
                          "DELETE FROM users", "SELECT count(*) FROM bookings"):
            with self.subTest(statement=statement):
                await self.assert_denied(ops, statement)


if __name__ == "__main__":
    unittest.main()
