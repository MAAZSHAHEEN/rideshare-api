"""Admin-only guard (Milestone 5): 401 unauthenticated, 403 non-admin, 200 admin."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
    "SECRET_KEY": "admin-guard-tests-only-not-a-production-key",
}):
    from jose import jwt

    import config
    from database import Base, get_db
    from main import app as main_app
    from models import User, UserRole
    from routers import admin as admin_routes
    from routers.admin import ADMIN_PREFIX, admin_router
    from routers.auth import create_access_token
    from routers.dependencies import ADMIN_FORBIDDEN_DETAIL, require_admin


def _calls(dependant):
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def unguarded_admin_routes(app: FastAPI) -> list[str]:
    """Paths under /admin whose dependency tree does not include require_admin."""
    return [
        route.path for route in app.routes
        if isinstance(route, APIRoute)
        and (route.path == ADMIN_PREFIX or route.path.startswith(f"{ADMIN_PREFIX}/"))
        and require_admin not in set(_calls(route.dependant))
    ]


def build_probe_app():
    calls = []
    marker = []

    async def extra_dependency():
        marker.append("extra")

    router = admin_router("/probe", dependencies=[Depends(extra_dependency)])

    @router.get("/whoami")
    async def whoami(admin: User = Depends(require_admin)):
        calls.append(admin.id)
        return {"id": admin.id}

    @router.post("/action")
    async def action():
        calls.append("action")
        return {"ok": True}

    probe = FastAPI()
    probe.include_router(router)
    return probe, calls, marker


class AdminRouteInventoryTests(unittest.TestCase):
    def test_every_admin_route_on_the_real_app_requires_admin(self):
        self.assertEqual(unguarded_admin_routes(main_app), [])

    def test_inventory_detects_an_unguarded_admin_route(self):
        unsafe = FastAPI()

        @unsafe.get("/admin/users")
        async def users():
            return []

        @unsafe.get("/administrators-public")
        async def not_admin_prefix():
            return []

        self.assertEqual(unguarded_admin_routes(unsafe), ["/admin/users"])

    def test_admin_router_routes_are_guarded(self):
        probe, _, _ = build_probe_app()
        self.assertEqual(unguarded_admin_routes(probe), [])
        self.assertIn(require_admin, {d.dependency for d in admin_routes.router.dependencies})
        self.assertTrue(admin_routes.router.prefix.startswith(ADMIN_PREFIX))


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class AdminGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = f"test_admin_guard_{uuid4().hex}"
        self.engine = create_async_engine(TEST_DATABASE_URL, connect_args={
            "server_settings": {"search_path": self.schema},
        })
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.execute(CreateSchema(self.schema))
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            self.users = {}
            for name, role in (("passenger", UserRole.passenger), ("driver", UserRole.driver),
                               ("admin", UserRole.admin)):
                user = User(name=f"Test {name}", email=f"{name}@example.com",
                            cnic=f"fake-cnic-{name}", phone_number="fake-phone",
                            password="not-a-real-hash", role=role)
                db.add(user)
                self.users[name] = user
            await db.commit()

        async def override_get_db():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        self.app, self.calls, self.marker = build_probe_app()
        self.app.dependency_overrides[get_db] = override_get_db
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        async with self.engine.begin() as conn:
            await conn.execute(DropSchema(self.schema, cascade=True))

    def token(self, name):
        user = self.users[name]
        return create_access_token(user.id, user.role)

    async def request(self, method, path, token=None, raw_header=None):
        headers = {}
        if raw_header is not None:
            headers["Authorization"] = raw_header
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return await self.client.request(method, f"/admin/probe{path}", headers=headers)

    async def test_admin_is_allowed(self):
        response = await self.request("GET", "/whoami", self.token("admin"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"id": self.users["admin"].id})
        response = await self.request("POST", "/action", self.token("admin"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls, [self.users["admin"].id, "action"])
        self.assertEqual(self.marker, ["extra", "extra"])

    async def test_passenger_and_driver_get_403_and_handler_never_runs(self):
        for name in ("passenger", "driver"):
            for method, path in (("GET", "/whoami"), ("POST", "/action")):
                with self.subTest(user=name, path=path):
                    response = await self.request(method, path, self.token(name))
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json(), {"detail": ADMIN_FORBIDDEN_DETAIL})
        self.assertEqual(self.calls, [])

    async def test_unauthenticated_requests_get_401(self):
        expired = jwt.encode({
            "sub": str(self.users["admin"].id), "role": "admin",
            "iat": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
            "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
        }, config.SECRET_KEY, algorithm=config.ALGORITHM)
        wrong_key = jwt.encode({
            "sub": str(self.users["admin"].id), "role": "admin",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }, "x" * 48, algorithm=config.ALGORITHM)
        cases = {
            "no header": None,
            "empty bearer": "Bearer ",
            "malformed": "Bearer malformed",
            "basic scheme": "Basic YWRtaW46YWRtaW4=",
            "expired admin token": f"Bearer {expired}",
            "admin claim, wrong key": f"Bearer {wrong_key}",
        }
        for label, header in cases.items():
            for method, path in (("GET", "/whoami"), ("POST", "/action")):
                with self.subTest(case=label, path=path):
                    response = await self.request(method, path, raw_header=header)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                    self.assertEqual(set(response.json()), {"detail"})
        self.assertEqual(self.calls, [])

    async def test_signed_admin_claim_for_non_admin_account_is_401(self):
        # Even a validly signed token cannot grant admin: the DB role must match.
        for name in ("passenger", "driver"):
            with self.subTest(user=name):
                forged = create_access_token(self.users[name].id, UserRole.admin)
                response = await self.request("GET", "/whoami", forged)
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.calls, [])

    async def test_demoted_or_deleted_admin_token_stops_working(self):
        token = self.token("admin")
        async with self.engine.begin() as conn:
            await conn.execute(update(User).where(User.id == self.users["admin"].id)
                               .values(role=UserRole.driver))
        response = await self.request("GET", "/whoami", token)
        self.assertEqual(response.status_code, 401, response.text)
        async with self.engine.begin() as conn:
            await conn.execute(delete(User).where(User.id == self.users["admin"].id))
        response = await self.request("GET", "/whoami", token)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.calls, [])

    async def test_admin_role_cannot_be_self_registered(self):
        # Regression: the admin guard is only meaningful if nobody can mint an admin.
        async def override_get_db():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        with patch.dict(main_app.dependency_overrides, {get_db: override_get_db}):
            async with AsyncClient(transport=ASGITransport(app=main_app),
                                   base_url="http://testserver") as client:
                response = await client.post("/auth/register", json={
                    "name": "Mallory Test", "email": "mallory@example.com",
                    "password": "long-enough-test-password", "cnic": "00000-0000000-0",
                    "phone_number": "03000000000", "role": "admin",
                })
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
