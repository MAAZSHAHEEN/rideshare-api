"""Admin bootstrap CLI (scripts.grant_admin) and API privilege-escalation guards.

Isolated PostgreSQL schemas (SuspensionTestBase); one subprocess test uses a
throwaway database. Identities are obviously fake. No real admin is created
anywhere but these disposable schemas.
"""

import asyncio
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from tests.test_account_suspension import (PASSWORD, REASON, TEST_DATABASE_URL, SuspensionTestBase,
                                           password_hash)

import jwt

import audit
from database import Base
from main import app
from models import AdminAuditLog, User, UserRole
from scripts import grant_admin

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "fake-bootstrap-token-for-tests-only-0123456789"
LOCAL_URL = "postgresql+asyncpg://rideshare_ci:not-used@localhost:5432/rs_disposable"
CLI_REASON = "Initial admin approved for tests"


class CliTestBase(SuspensionTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.env = {"DATABASE_URL": LOCAL_URL, grant_admin.TOKEN_ENV: TOKEN}

    async def no_admins(self):
        async with self.session() as db:
            await db.execute(update(User).where(User.role == UserRole.admin)
                             .values(role=UserRole.passenger))
            await db.commit()

    async def cli(self, *argv, secrets=(TOKEN,), env=None):
        out, err = io.StringIO(), io.StringIO()
        code = await grant_admin.run(
            list(argv), env=self.env if env is None else env,
            stdin=io.StringIO("".join(f"{line}\n" for line in secrets)),
            out=out, err=err, engine=self.engine)
        self.last_out, self.last_err = out.getvalue(), err.getvalue()
        return code

    def bootstrap_args(self, email="TEST.passenger@example.COM", *extra):
        return ("--bootstrap", "--email", email, "--reason", CLI_REASON, "--confirm",
                "--secrets-stdin", *extra)

    def grant_args(self, email, actor="test.admin@example.com", *extra):
        return ("--email", email, "--actor-email", actor, "--reason", "Second on-call admin",
                "--confirm", "--secrets-stdin", *extra)

    async def user_row(self, name):
        async with self.session() as db:
            return (await db.execute(select(User.role, User.account_status, User.token_version)
                                     .where(User.id == self.users[name].id))).one()

    async def audit_rows(self):
        async with self.session() as db:
            return (await db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))).all()


class BootstrapTests(CliTestBase):
    async def test_bootstrap_grants_first_admin_audited_with_token_bump(self):
        await self.no_admins()
        before = self.token("passenger")
        self.assertEqual((await self.client.get("/me", headers=self.auth(before))).status_code, 200)

        self.assertEqual(await self.cli(*self.bootstrap_args("  TEST.Passenger@EXAMPLE.com ")), 0,
                         self.last_err)
        self.assertEqual(await self.user_row("passenger"), (UserRole.admin, "active", 1))
        [row] = await self.audit_rows()
        self.assertEqual((row.actor_id, row.action, row.target_type, row.target_id, row.reason),
                         (None, "admin_granted", "user", self.users["passenger"].id, CLI_REASON))
        self.assertEqual(row.details, {
            "actor": "system:bootstrap", "mode": "bootstrap", "previous_role": "passenger",
            "new_role": "admin", "token_version": 1, "target_status": "active"})
        self.assertIn("admin_granted", self.last_out)
        self.assertNotIn("test.passenger@example.com", self.last_out.lower())  # masked

        # Old token is dead (role and tv changed); a fresh login is an admin.
        self.assertEqual((await self.client.get("/me", headers=self.auth(before))).status_code, 401)
        login = await self.login("passenger")
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["access_token"]
        claims = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual((claims["role"], claims["tv"]), ("admin", 1))
        page = await self.client.get("/admin/audit-log", headers=self.auth(token))
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["items"][0]["action"], "admin_granted")

    async def test_bootstrap_refused_while_any_admin_exists(self):
        self.assertEqual(await self.cli(*self.bootstrap_args()), grant_admin.EXIT_REFUSED)
        self.assertIn("already exists", self.last_err)
        async with self.session() as db:  # a suspended admin still counts
            await db.execute(update(User).where(User.id != self.users["admin"].id,
                                                User.role == UserRole.admin)
                             .values(role=UserRole.passenger))
            await db.execute(update(User).where(User.id == self.users["admin"].id)
                             .values(account_status="suspended"))
            await db.commit()
        self.assertEqual(await self.cli(*self.bootstrap_args()), grant_admin.EXIT_REFUSED)
        self.assertEqual(await self.user_row("passenger"), (UserRole.passenger, "active", 0))
        self.assertEqual(await self.audit_rows(), [])

    async def test_confirm_token_and_usage_are_enforced_before_any_write(self):
        await self.no_admins()
        no_confirm = [a for a in self.bootstrap_args() if a != "--confirm"]
        cases = [
            ("no --confirm", no_confirm, (TOKEN,), self.env, grant_admin.EXIT_USAGE),
            ("wrong token", self.bootstrap_args(), ("fake-wrong-token-" + "0" * 30,), self.env,
             grant_admin.EXIT_UNAUTHORIZED),
            ("empty token", self.bootstrap_args(), ("",), self.env, grant_admin.EXIT_UNAUTHORIZED),
            ("token prefix", self.bootstrap_args(), (TOKEN[:-1],), self.env,
             grant_admin.EXIT_UNAUTHORIZED),
            ("env unset", self.bootstrap_args(), (TOKEN,), {"DATABASE_URL": LOCAL_URL},
             grant_admin.EXIT_UNAUTHORIZED),
            ("env too short", self.bootstrap_args(), ("short-token",),
             {"DATABASE_URL": LOCAL_URL, grant_admin.TOKEN_ENV: "short-token"},
             grant_admin.EXIT_UNAUTHORIZED),
            ("env placeholder", self.bootstrap_args(), ("change-me" + "x" * 40,),
             {"DATABASE_URL": LOCAL_URL, grant_admin.TOKEN_ENV: "change-me" + "x" * 40},
             grant_admin.EXIT_UNAUTHORIZED),
            ("bootstrap + actor", self.bootstrap_args("test.passenger@example.com", "--actor-email",
                                                      "test.admin@example.com"),
             (TOKEN,), self.env, grant_admin.EXIT_USAGE),
            ("bootstrap + revoke", self.bootstrap_args("test.passenger@example.com", "--revoke"),
             (TOKEN,), self.env, grant_admin.EXIT_USAGE),
            ("grant without actor", [a for a in self.bootstrap_args() if a != "--bootstrap"],
             (TOKEN,), self.env, grant_admin.EXIT_USAGE),
            ("to-role without revoke", self.bootstrap_args("test.passenger@example.com",
                                                           "--to-role", "driver"),
             (TOKEN,), self.env, grant_admin.EXIT_USAGE),
            ("reason with CNIC", ["--bootstrap", "--email", "test.passenger@example.com",
                                  "--reason", "CNIC 12345-0000000-1", "--confirm",
                                  "--secrets-stdin"], (TOKEN,), self.env, grant_admin.EXIT_USAGE),
            ("unknown flag --role", [*self.bootstrap_args(), "--role", "admin"], (TOKEN,),
             self.env, grant_admin.EXIT_USAGE),
            ("no target", self.bootstrap_args("nobody@example.com"), (TOKEN,), self.env,
             grant_admin.EXIT_REFUSED),
        ]
        for label, argv, secrets, env, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(await self.cli(*argv, secrets=secrets, env=env), expected,
                                 self.last_err)
        self.assertEqual(await self.user_row("passenger"), (UserRole.passenger, "active", 0))
        self.assertEqual(await self.audit_rows(), [])

    async def test_suspended_target_is_not_granted(self):
        await self.no_admins()
        async with self.session() as db:
            await db.execute(update(User).where(User.id == self.users["driver"].id)
                             .values(account_status="suspended", token_version=1))
            await db.commit()
        self.assertEqual(await self.cli(*self.bootstrap_args("test.driver@example.com")),
                         grant_admin.EXIT_REFUSED)
        self.assertEqual(await self.user_row("driver"), (UserRole.driver, "suspended", 1))
        self.assertEqual(await self.audit_rows(), [])

    async def test_failed_audit_write_rolls_back_the_role_change(self):
        await self.no_admins()
        with patch.object(audit, "record", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                await self.cli(*self.bootstrap_args())
        self.assertEqual(await self.user_row("passenger"), (UserRole.passenger, "active", 0))

    async def test_concurrent_bootstraps_yield_exactly_one_admin(self):
        await self.no_admins()
        codes = await asyncio.gather(
            self.cli(*self.bootstrap_args("test.passenger@example.com")),
            self.cli(*self.bootstrap_args("test.driver@example.com")))
        self.assertEqual(sorted(codes), [grant_admin.EXIT_OK, grant_admin.EXIT_REFUSED])
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(User)
                                             .where(User.role == UserRole.admin)), 1)
        self.assertEqual(len(await self.audit_rows()), 1)

    async def test_second_bootstrap_waits_on_the_advisory_lock_then_is_refused(self):
        """Forced: run 1 is paused inside its transaction (after its audit row, before commit);
        run 2 must wait on the advisory lock, then see the new admin and refuse."""
        await self.no_admins()
        gate, reached = asyncio.Event(), asyncio.Event()
        real_record = audit.record

        async def held_record(*args, **kwargs):
            entry = await real_record(*args, **kwargs)
            if not reached.is_set():
                reached.set()
                await gate.wait()
            return entry

        with patch.object(audit, "record", held_record):
            first = asyncio.create_task(self.cli(*self.bootstrap_args("test.passenger@example.com")))
            await asyncio.wait_for(reached.wait(), 10)
            second = asyncio.create_task(self.cli(*self.bootstrap_args("test.driver@example.com")))
            blocked = False
            async with self.engine.connect() as conn:
                for _ in range(250):
                    if second.done():
                        break
                    if await conn.scalar(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND wait_event = 'advisory'")):
                        blocked = True
                        break
                    # pg_stat_activity's list of backends is snapshotted once per transaction:
                    # end it, or a backend that connected after the first poll is never seen.
                    await conn.rollback()
                    await asyncio.sleep(0.02)
            gate.set()
            codes = [await first, await second]
        self.assertEqual(codes, [grant_admin.EXIT_OK, grant_admin.EXIT_REFUSED])
        self.assertTrue(blocked, "second run did not wait on the advisory lock")
        self.assertEqual((await self.user_row("driver"))[0], UserRole.driver)
        self.assertEqual(len(await self.audit_rows()), 1)


class GrantRevokeTests(CliTestBase):
    async def test_existing_admin_grants_with_password_and_is_recorded_as_actor(self):
        code = await self.cli(*self.grant_args("Test.Driver@Example.com"), secrets=(TOKEN, PASSWORD))
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(await self.user_row("driver"), (UserRole.admin, "active", 1))
        [row] = await self.audit_rows()
        self.assertEqual((row.actor_id, row.action, row.details["actor"], row.details["mode"],
                          row.details["previous_role"]),
                         (self.users["admin"].id, "admin_granted", "admin_cli", "grant", "driver"))

    async def test_actor_must_be_an_active_admin_with_the_right_password(self):
        async with self.session() as db:
            await db.execute(update(User).where(User.id == self.users["other_admin"].id)
                             .values(account_status="suspended"))
            await db.commit()
        cases = [
            ("wrong password", "test.admin@example.com", "Wrong-password-123!"),
            ("empty password", "test.admin@example.com", ""),
            ("actor not admin", "test.other_driver@example.com", PASSWORD),
            ("actor suspended admin", "test.other_admin@example.com", PASSWORD),
            ("unknown actor", "nobody@example.com", PASSWORD),
        ]
        for label, actor, password in cases:
            with self.subTest(case=label):
                code = await self.cli(*self.grant_args("test.driver@example.com", actor),
                                      secrets=(TOKEN, password))
                self.assertEqual(code, grant_admin.EXIT_UNAUTHORIZED, self.last_err)
        self.assertEqual(await self.user_row("driver"), (UserRole.driver, "active", 0))
        self.assertEqual(await self.audit_rows(), [])

    async def test_admin_cannot_change_own_role_and_target_rules_hold(self):
        cases = [
            ("self grant", self.grant_args("test.admin@example.com"), grant_admin.EXIT_REFUSED),
            ("self revoke", (*self.grant_args("test.admin@example.com"), "--revoke"),
             grant_admin.EXIT_REFUSED),
            ("already admin", self.grant_args("test.other_admin@example.com"),
             grant_admin.EXIT_REFUSED),
            ("revoke non-admin", (*self.grant_args("test.driver@example.com"), "--revoke"),
             grant_admin.EXIT_REFUSED),
        ]
        for label, argv, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(await self.cli(*argv, secrets=(TOKEN, PASSWORD)), expected,
                                 self.last_err)
        self.assertEqual(await self.user_row("admin"), (UserRole.admin, "active", 0))
        self.assertEqual(await self.user_row("other_admin"), (UserRole.admin, "active", 0))
        self.assertEqual(await self.audit_rows(), [])

    async def test_revoke_demotes_bumps_version_and_kills_admin_token(self):
        victim_token = self.token("other_admin")
        ok = await self.client.get("/admin/audit-log", headers=self.auth(victim_token))
        self.assertEqual(ok.status_code, 200)
        code = await self.cli(*self.grant_args("test.other_admin@example.com"), "--revoke",
                              "--to-role", "driver", secrets=(TOKEN, PASSWORD))
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(await self.user_row("other_admin"), (UserRole.driver, "active", 1))
        [row] = await self.audit_rows()
        self.assertEqual((row.actor_id, row.action, row.details["new_role"]),
                         (self.users["admin"].id, "admin_revoked", "driver"))
        denied = await self.client.get("/admin/audit-log", headers=self.auth(victim_token))
        self.assertEqual(denied.status_code, 401)

    async def test_suspended_admin_can_be_revoked_and_stays_suspended(self):
        response = await self.suspend("other_admin")  # admin-on-admin, see D-TF-1
        self.assertEqual(response.status_code, 200, response.text)
        code = await self.cli(*self.grant_args("test.other_admin@example.com"), "--revoke",
                              secrets=(TOKEN, PASSWORD))
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(await self.user_row("other_admin"), (UserRole.passenger, "suspended", 2))

    async def test_non_bootstrap_grant_with_zero_admins_is_refused(self):
        await self.no_admins()
        code = await self.cli(*self.grant_args("test.driver@example.com"), secrets=(TOKEN, PASSWORD))
        self.assertEqual(code, grant_admin.EXIT_UNAUTHORIZED)
        self.assertEqual(await self.audit_rows(), [])


class ProductionGuardTests(CliTestBase):
    def test_looks_like_production(self):
        prod = [
            ("postgresql+asyncpg://u:p@ep-fake-123456.us-east-2.aws.neon.tech/neondb", {}),
            ("postgresql+asyncpg://u:p@ep-fake-pooler.neon.tech:5432/neondb?ssl=require", {}),
            ("postgresql+asyncpg://u:p@10.0.0.5/rideshare", {}),
            ("postgresql+asyncpg://u:p@db.example.com/rideshare", {}),
            ("postgresql+asyncpg://u:p@localhost/rideshare_prod", {}),
            ("postgresql+asyncpg://u:p@localhost/rideshare", {"APP_ENV": "production"}),
            ("postgresql+asyncpg://u:p@localhost/rideshare", {"ENVIRONMENT": " Prod "}),
            ("not a url", {}),
        ]
        local = [
            ("postgresql+asyncpg://u:p@localhost:5432/rideshare_ci", {}),
            ("postgresql+asyncpg://u:p@127.0.0.1/rs_tf1", {"APP_ENV": "test"}),
            ("postgresql+asyncpg://u:p@[::1]/rs", {}),
            ("postgresql+asyncpg://u:p@db:5432/rideshare", {}),
            ("postgresql+asyncpg:///rideshare", {}),
        ]
        for url, env in prod:
            with self.subTest(url=url, env=env):
                self.assertIsNotNone(grant_admin.looks_like_production(url, env))
        for url, env in local:
            with self.subTest(url=url, env=env):
                self.assertIsNone(grant_admin.looks_like_production(url, env))

    async def test_production_url_refused_without_flag_and_nothing_read(self):
        await self.no_admins()
        env = {**self.env, "DATABASE_URL": "postgresql+asyncpg://u:p@ep-fake.neon.tech/neondb"}
        stdin = io.StringIO(f"{TOKEN}\n")
        err = io.StringIO()
        code = await grant_admin.run(list(self.bootstrap_args()), env=env, stdin=stdin,
                                     out=io.StringIO(), err=err, engine=self.engine)
        self.assertEqual(code, grant_admin.EXIT_PRODUCTION)
        self.assertIn("Maaz", err.getvalue())
        self.assertEqual(stdin.tell(), 0)  # token never read
        self.assertEqual(await self.user_row("passenger"), (UserRole.passenger, "active", 0))

    async def test_flag_allows_production_looking_url_with_warning(self):
        await self.no_admins()
        env = {**self.env, "APP_ENV": "production"}
        self.assertEqual(await self.cli(*self.bootstrap_args(), env=env), grant_admin.EXIT_PRODUCTION)
        code = await self.cli(*self.bootstrap_args("test.passenger@example.com",
                                                   "--i-understand-production"), env=env)
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("written approval", self.last_err)


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class CliSubprocessTests(unittest.IsolatedAsyncioTestCase):
    """The real `python -m scripts.grant_admin` entry point, without SECRET_KEY in its env."""

    def run_cli(self, argv, env, stdin=""):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("SECRET_KEY", "DATABASE_URL", grant_admin.TOKEN_ENV)}
        return subprocess.run([sys.executable, "-B", "-m", "scripts.grant_admin", *argv],
                              cwd=ROOT, env={**clean, **env}, input=stdin, capture_output=True,
                              text=True, timeout=60)

    async def test_entry_point_bootstrap_and_production_refusal(self):
        name = f"test_grant_admin_{uuid4().hex[:12]}"
        admin_engine = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT",
                                           poolclass=NullPool)
        self.addAsyncCleanup(admin_engine.dispose)
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))

        async def drop():
            async with admin_engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        self.addAsyncCleanup(drop)
        url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{name}"
        engine = create_async_engine(url, poolclass=NullPool)
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as db:
            db.add(User(name="Test Operator", email="ops.admin@example.com",
                        cnic="1234500000991", phone_number="+923000000991",
                        password=password_hash(), role=UserRole.passenger))
            await db.commit()

        argv = ["--bootstrap", "--email", "OPS.Admin@example.com", "--reason", CLI_REASON,
                "--confirm", "--secrets-stdin"]
        env = {"DATABASE_URL": url, grant_admin.TOKEN_ENV: TOKEN}
        refused = self.run_cli(argv,
                               {**env, "DATABASE_URL": "postgresql+asyncpg://u:p@ep-fake.neon.tech/neondb"},
                               f"{TOKEN}\n")
        self.assertEqual(refused.returncode, grant_admin.EXIT_PRODUCTION, refused.stderr)
        unconfirmed = self.run_cli([a for a in argv if a != "--confirm"], env, f"{TOKEN}\n")
        self.assertEqual(unconfirmed.returncode, grant_admin.EXIT_USAGE, unconfirmed.stderr)

        done = self.run_cli(argv, env, f"{TOKEN}\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("admin_granted", done.stdout)
        async with AsyncSession(engine) as db:
            user = await db.scalar(select(User))
            self.assertEqual((user.role, user.token_version), (UserRole.admin, 1))
            row = await db.scalar(select(AdminAuditLog))
            self.assertEqual((row.actor_id, row.action, row.details["actor"]),
                             (None, "admin_granted", "system:bootstrap"))
        again = self.run_cli(argv, env, f"{TOKEN}\n")
        self.assertEqual(again.returncode, grant_admin.EXIT_REFUSED, again.stderr)


PRIVILEGED_FIELDS = {"is_admin", "admin", "is_superuser", "superuser", "account_status",
                     "token_version", "tv", "permissions", "scopes"}


class ApiEscalationTests(SuspensionTestBase):
    """No HTTP path can set or change a role (registration, profile, admin endpoints)."""

    def registration(self, **extra):
        return {"name": "Test Newcomer", "email": "test.newcomer@example.com",
                "password": "Fake-test-password-123!", "cnic": "1234500000777",
                "phone_number": "+923000000777", **extra}

    async def test_register_rejects_admin_role_in_any_spelling(self):
        for role in ("admin", "ADMIN", "Admin", " admin", "superuser", 2, ["admin"], None):
            with self.subTest(role=role):
                response = await self.client.post("/auth/register", json=self.registration(role=role))
                self.assertEqual(response.status_code, 422, response.text)
        async with self.session() as db:
            self.assertIsNone(await db.scalar(select(User.id).where(
                User.email == "test.newcomer@example.com")))

    async def test_register_ignores_privileged_mass_assignment_fields(self):
        response = await self.client.post("/auth/register", json=self.registration(
            is_admin=True, is_superuser=True, account_status="suspended", token_version=99,
            id=1, role_id=3, permissions=["admin"]))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "passenger")
        self.assertNotIn("is_admin", response.json())
        async with self.session() as db:
            row = (await db.execute(select(User.id, User.role, User.account_status, User.token_version)
                                    .where(User.email == "test.newcomer@example.com"))).one()
        self.assertNotEqual(row.id, 1)
        self.assertEqual((row.role, row.account_status, row.token_version),
                         (UserRole.passenger, "active", 0))

    def test_no_request_body_schema_carries_role_or_privilege_fields(self):
        spec = app.openapi()
        schemas = spec["components"]["schemas"]

        def walk(schema, seen):
            if "$ref" in schema:
                name = schema["$ref"].rsplit("/", 1)[-1]
                if name not in seen:
                    seen.add(name)
                    walk(schemas[name], seen)
            for key in ("anyOf", "allOf", "oneOf"):
                for part in schema.get(key, []):
                    walk(part, seen)
            for prop in schema.get("properties", {}).values():
                walk(prop, seen)
            if "items" in schema:
                walk(schema["items"], seen)

        body_models = set()
        for path, methods in spec["paths"].items():
            for method, operation in methods.items():
                body = operation.get("requestBody", {}).get("content", {})
                for media in body.values():
                    walk(media["schema"], body_models)
        self.assertIn("UserCreate", body_models)
        for name in body_models:
            props = set(schemas[name].get("properties", {}))
            with self.subTest(schema=name):
                self.assertFalse(props & PRIVILEGED_FIELDS, props)
                if name != "UserCreate":
                    self.assertNotIn("role", props)
        role = schemas["UserCreate"]["properties"]["role"]
        self.assertEqual(sorted(role.get("enum", [])), ["driver", "passenger"])

    def test_admin_surface_is_exactly_the_reviewed_routes(self):
        # Adding any /admin route (e.g. one that changes a role) must update this list in review.
        routes = {(method, route.path) for route in app.routes if route.path.startswith("/admin")
                  for method in getattr(route, "methods", ()) if method != "HEAD"}
        self.assertEqual(routes, {
            ("GET", "/admin/users/{user_id}"),
            ("POST", "/admin/users/{user_id}/suspend"),
            ("POST", "/admin/users/{user_id}/reinstate"),
            ("GET", "/admin/audit-log"),
        })
        writes = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ())
                  if m in {"PUT", "PATCH", "POST", "DELETE"}}
        self.assertFalse({w for w in writes if w[1] in {"/me", "/users/me", "/users/{user_id}"}},
                         "a profile update route now exists: re-run the role mass-assignment review")

    async def test_role_fields_in_admin_bodies_are_ignored(self):
        body = {**REASON, "role": "admin", "is_admin": True, "token_version": 0}
        response = await self.suspend("passenger", body=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["role"], "passenger")
        response = await self.reinstate("passenger", body=body)
        self.assertEqual(response.status_code, 200, response.text)
        async with self.session() as db:
            self.assertEqual((await db.execute(select(User.role, User.token_version).where(
                User.id == self.users["passenger"].id))).one(), (UserRole.passenger, 2))

    async def test_admin_cannot_act_on_self_and_nothing_is_audited(self):
        for action in (self.suspend, self.reinstate):
            with self.subTest(action=action.__name__):
                response = await action("admin")
                self.assertEqual(response.status_code, 403, response.text)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AdminAuditLog)), 0)
            self.assertEqual((await db.execute(select(User.role, User.account_status,
                                                      User.token_version)
                                               .where(User.id == self.users["admin"].id))).one(),
                             (UserRole.admin, "active", 0))

    async def test_admin_on_admin_suspension_is_allowed_audited_and_revokes(self):
        victim = (await self.login("other_admin")).json()["access_token"]
        self.assertEqual((await self.client.get("/admin/audit-log",
                                                headers=self.auth(victim))).status_code, 200)
        response = await self.suspend("other_admin")
        self.assertEqual(response.status_code, 200, response.text)
        async with self.session() as db:
            row = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "user.suspend"))
        self.assertEqual((row.actor_id, row.target_id, row.details["target_role"]),
                         (self.users["admin"].id, self.users["other_admin"].id, "admin"))
        self.assertEqual((await self.client.get("/admin/audit-log",
                                                headers=self.auth(victim))).status_code, 401)
        # A suspended admin cannot suspend back.
        self.assertEqual((await self.client.post(
            f"/admin/users/{self.users['admin'].id}/suspend", json=REASON,
            headers=self.auth(victim))).status_code, 401)
        self.assertEqual((await self.login("other_admin")).status_code, 401)
        self.assertEqual((await self.reinstate("other_admin")).status_code, 200)
        self.assertEqual((await self.client.get("/admin/audit-log",
                                                headers=self.auth(victim))).status_code, 401)
        fresh = (await self.login("other_admin")).json()["access_token"]
        self.assertEqual((await self.client.get("/admin/audit-log",
                                                headers=self.auth(fresh))).status_code, 200)


class SuspendedTokenEndToEndTests(SuspensionTestBase):
    async def test_suspension_lifecycle_over_http(self):
        ride = await self.add_ride(driver="other_driver", seats=2)
        login = await self.login("passenger")
        self.assertEqual(login.status_code, 200, login.text)
        old = login.json()["access_token"]
        for path in ("/me", "/bookings/me"):
            self.assertEqual((await self.client.get(path, headers=self.auth(old))).status_code, 200)

        self.assertEqual((await self.suspend("passenger")).status_code, 200)
        for method, path in (("GET", "/me"), ("GET", "/bookings/me"), ("POST", f"/bookings/{ride}")):
            with self.subTest(path=path):
                response = await self.client.request(method, path, headers=self.auth(old))
                self.assertEqual((response.status_code, response.json()),
                                 (401, {"detail": "Invalid token"}))
        right = await self.login("passenger")
        wrong = await self.login("passenger", password="Wrong-password-123!")
        self.assertEqual((right.status_code, right.json()), (wrong.status_code, wrong.json()))
        self.assertEqual(right.status_code, 401)
        # There is no refresh endpoint that could mint a token for a suspended account.
        self.assertIn((await self.client.post("/auth/refresh", headers=self.auth(old))).status_code,
                      (404, 405))

        self.assertEqual((await self.reinstate("passenger")).status_code, 200)
        self.assertEqual((await self.client.get("/me", headers=self.auth(old))).status_code, 401)
        fresh = await self.login("passenger")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        new = fresh.json()["access_token"]
        self.assertEqual(jwt.decode(new, options={"verify_signature": False})["tv"], 2)
        self.assertEqual((await self.client.get("/me", headers=self.auth(new))).status_code, 200)
        booked = await self.client.post(f"/bookings/{ride}", headers=self.auth(new))
        self.assertEqual(booked.status_code, 201, booked.text)


if __name__ == "__main__":
    unittest.main()
