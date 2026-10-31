"""Append-only admin audit log (Milestone D, M5-B) against isolated PostgreSQL schemas."""

import asyncio
import json
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from tests.test_account_suspension import REASON, SuspensionTestBase

import audit
from models import AdminAuditLog, BookingStatus, RideStatus
from routers.dependencies import ADMIN_FORBIDDEN_DETAIL


class AuditTestBase(SuspensionTestBase):
    async def rows(self):
        async with self.session() as db:
            return (await db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))).all()


class AuditWriteTests(AuditTestBase):
    async def test_suspend_reinstate_and_view_each_write_one_row(self):
        ride = await self.add_ride(seats=2)
        booking = await self.add_booking(ride, "passenger", BookingStatus.accepted)
        target = self.users["driver"]
        admin = self.users["admin"]

        self.assertEqual((await self.client.get(f"/admin/users/{target.id}",
                                                headers=self.auth("admin"))).status_code, 200)
        self.assertEqual((await self.suspend("driver")).status_code, 200)
        self.assertEqual((await self.reinstate("driver", actor="other_admin",
                                               body={"reason": "  Appeal accepted  "})).status_code, 200)

        rows = await self.rows()
        self.assertEqual([(r.actor_id, r.action, r.target_type, r.target_id) for r in rows], [
            (admin.id, "user.view", "user", target.id),
            (admin.id, "user.suspend", "user", target.id),
            (self.users["other_admin"].id, "user.reinstate", "user", target.id),
        ])
        view, suspend, reinstate = rows
        self.assertIsNone(view.reason)
        self.assertEqual(view.details, {})
        self.assertEqual(suspend.reason, REASON["reason"])
        self.assertEqual(suspend.details, {
            "previous_status": "active", "new_status": "suspended", "token_version": 1,
            "cancelled_ride_ids": [ride], "cancelled_booking_ids": [booking],
            "departed_active_ride_ids": [],
        })
        self.assertEqual(reinstate.reason, "Appeal accepted")  # stored trimmed
        self.assertEqual(reinstate.details, {"previous_status": "suspended",
                                             "new_status": "active", "token_version": 2})
        for row in rows:
            self.assertIsNotNone(row.created_at)
            dumped = json.dumps(row.details) + (row.reason or "")
            for user in self.users.values():
                for pii in (user.cnic, user.phone_number, user.email, user.name):
                    self.assertNotIn(pii, dumped)

    async def test_rejected_requests_write_nothing(self):
        target = self.users["passenger"].id
        attempts = (
            ("GET", "/admin/users/999999", "admin", None, 404),
            ("GET", f"/admin/users/{target}", "driver", None, 403),
            ("POST", f"/admin/users/{target}/suspend", "passenger", REASON, 403),
            ("POST", f"/admin/users/{target}/suspend", None, REASON, 401),
            ("POST", f"/admin/users/{target}/suspend", "admin", {"reason": "x"}, 422),
            ("POST", f"/admin/users/{self.users['admin'].id}/suspend", "admin", REASON, 403),
            ("POST", f"/admin/users/{target}/reinstate", "admin", REASON, 409),
            ("POST", "/admin/users/999999/suspend", "admin", REASON, 404),
        )
        for method, path, actor, body, status in attempts:
            with self.subTest(path=path, actor=actor):
                headers = self.auth(actor) if actor else {}
                response = await self.client.request(method, path, json=body, headers=headers)
                self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(await self.rows(), [])

    async def test_audit_failure_rolls_back_the_suspension(self):
        ride = await self.add_ride(seats=2)

        async def broken_record(*args, **kwargs):
            raise RuntimeError("simulated audit write failure")

        with patch.object(audit, "record", broken_record):
            with self.assertRaises(RuntimeError):
                await self.suspend("driver")
        self.assertEqual(await self.account("driver"), ("active", 0))
        self.assertEqual(await self.ride_state(ride), (RideStatus.active, 2))
        self.assertEqual(await self.rows(), [])

    async def test_failure_after_the_audit_write_rolls_both_back(self):
        # If audit.record committed on its own, this would leave a suspension
        # and an audit row behind even though the request failed.
        import routers.admin as admin_routes

        with patch.object(admin_routes, "SuspensionResponse", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await self.suspend("passenger")
        self.assertEqual(await self.account("passenger"), ("active", 0))
        self.assertEqual(await self.rows(), [])

    async def test_failed_commit_leaves_neither_action_nor_audit_row(self):
        from sqlalchemy.ext.asyncio import AsyncSession

        async def failing_commit(session):
            raise RuntimeError("simulated commit failure")

        with patch.object(AsyncSession, "commit", failing_commit):
            with self.assertRaises(RuntimeError):
                await self.suspend("passenger")
        self.assertEqual(await self.account("passenger"), ("active", 0))
        self.assertEqual(await self.rows(), [])

    async def test_concurrent_suspensions_leave_exactly_one_row(self):
        responses = await asyncio.gather(self.suspend("passenger", actor="admin"),
                                         self.suspend("passenger", actor="other_admin"))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertEqual([r.action for r in await self.rows()], ["user.suspend"])


class AuditImmutabilityTests(AuditTestBase):
    async def seed_row(self):
        await self.suspend("passenger")
        return (await self.rows())[0].id

    async def assert_blocked(self, statement, parameters=None):
        with self.assertRaises(DBAPIError) as raised:
            async with self.engine.begin() as conn:
                await conn.execute(text(statement), parameters or {})
        self.assertEqual(raised.exception.orig.sqlstate, "42501")
        self.assertIn("append-only", str(raised.exception.orig))

    async def test_update_delete_truncate_are_rejected_by_the_database(self):
        row_id = await self.seed_row()
        before = [(r.id, r.action, r.reason, r.details) for r in await self.rows()]
        for statement in (
            "UPDATE admin_audit_log SET reason = 'rewritten' WHERE id = :id",
            "UPDATE admin_audit_log SET actor_id = NULL",
            "UPDATE admin_audit_log SET metadata = '{}'::jsonb WHERE id = :id",
            "DELETE FROM admin_audit_log WHERE id = :id",
            "DELETE FROM admin_audit_log",
            "TRUNCATE admin_audit_log",
        ):
            with self.subTest(statement=statement):
                await self.assert_blocked(statement, {"id": row_id})
        self.assertEqual([(r.id, r.action, r.reason, r.details) for r in await self.rows()], before)

    async def test_orm_update_and_delete_are_rejected(self):
        row_id = await self.seed_row()
        for statement in (
            update(AdminAuditLog).where(AdminAuditLog.id == row_id).values(reason="changed"),
            delete(AdminAuditLog).where(AdminAuditLog.id == row_id),
        ):
            with self.subTest(statement=str(statement).split()[0]):
                async with self.session() as db:
                    with self.assertRaises(DBAPIError):
                        await db.execute(statement)
                        await db.commit()
                    await db.rollback()
        self.assertEqual(len(await self.rows()), 1)

    async def test_inserts_still_work(self):
        async with self.session() as db:
            await audit.record(db, actor_id=None, action="user.view", target_type="user",
                               target_id=self.users["passenger"].id)
            await db.commit()
        self.assertEqual(len(await self.rows()), 1)


class AuditMetadataValidationTests(unittest.TestCase):
    def test_pii_shaped_metadata_is_rejected(self):
        bad = (
            {"email": "a"}, {"phone_last4": 1}, {"cnic_masked": "x"}, {"display_name": "a"},
            {"user_name": "x"}, {"access_token": "x"},
            {"note": "Ali Khan"}, {"note": "a@example.com"}, {"note": "x" * 65},
            {"ids": ["1"]}, {"ids": [1.0]}, {"nested": {"a": 1}}, {"ratio": 0.5},
            {"Bad-Key": 1}, {"": 1}, {1: 1},
        )
        for metadata in bad:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                audit.validate_metadata(metadata)

    def test_expected_metadata_is_accepted(self):
        metadata = {"previous_status": "active", "new_status": "suspended", "token_version": 3,
                    "cancelled_ride_ids": [1, 2], "flag": True, "missing": None}
        self.assertEqual(audit.validate_metadata(metadata), metadata)
        self.assertEqual(audit.validate_metadata(None), {})


class AuditLogEndpointTests(AuditTestBase):
    async def seed(self, count):
        async with self.session() as db:
            for index in range(count):
                await audit.record(
                    db, actor_id=self.users["admin" if index % 2 else "other_admin"].id,
                    action="user.view", target_type="user",
                    target_id=self.users["passenger" if index < 3 else "driver"].id)
            await db.commit()

    async def page(self, actor="admin", **params):
        return await self.client.get("/admin/audit-log", params=params, headers=self.auth(actor))

    async def test_pagination_is_newest_first_and_complete(self):
        await self.seed(5)
        seen, before_id = [], None
        while True:
            params = {"limit": 2} | ({"before_id": before_id} if before_id else {})
            response = await self.page(**params)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertLessEqual(len(body["items"]), 2)
            seen += [item["id"] for item in body["items"]]
            before_id = body["next_before_id"]
            if before_id is None:
                break
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        first = (await self.page(limit=1)).json()["items"][0]
        self.assertEqual(set(first), {"id", "actor_id", "action", "target_type", "target_id",
                                      "reason", "metadata", "created_at"})

    async def test_filters(self):
        await self.seed(5)
        passenger = self.users["passenger"].id
        body = (await self.page(target_type="user", target_id=passenger)).json()
        self.assertEqual({item["target_id"] for item in body["items"]}, {passenger})
        self.assertEqual(len(body["items"]), 3)
        body = (await self.page(actor_id=self.users["admin"].id)).json()
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual((await self.page(action="user.suspend")).json(),
                         {"items": [], "next_before_id": None})

    async def test_bounds_and_access(self):
        for params in ({"limit": 0}, {"limit": 101}, {"before_id": 0}, {"target_id": -1},
                       {"actor_id": 2**31}, {"limit": "x"}):
            with self.subTest(params=params):
                self.assertEqual((await self.page(**params)).status_code, 422)
        self.assertEqual((await self.page(limit=100)).status_code, 200)
        for actor in ("passenger", "driver"):
            response = await self.page(actor=actor)
            self.assertEqual((response.status_code, response.json()),
                             (403, {"detail": ADMIN_FORBIDDEN_DETAIL}))
        self.assertEqual((await self.client.get("/admin/audit-log")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
