"""Migration lifecycle against a genuinely empty disposable PostgreSQL database.

Set TEST_DATABASE_URL to a dedicated postgresql+asyncpg test database. Its user
needs CREATEDB permission. This test creates a new database from template0 and
drops only that database afterward; it never migrates the configured database.
Run: python -B -m unittest discover -s tests -p test_migrations.py -v
"""

import asyncio
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, Enum, MetaData, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
with patch.dict(os.environ, {
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://localhost/rideshare_test",
}):
    from models import Base


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to a PostgreSQL test database")
class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source_url = make_url(TEST_DATABASE_URL)
        if source_url.drivername != "postgresql+asyncpg":
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        self.database_name = f"test_migrations_{uuid4().hex}"
        self.admin_engine = create_async_engine(
            source_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        self.addAsyncCleanup(self.admin_engine.dispose)
        async with self.admin_engine.connect() as connection:
            await connection.exec_driver_sql(
                f'CREATE DATABASE "{self.database_name}" TEMPLATE template0'
            )
        self.addAsyncCleanup(self.drop_database)

        self.url = source_url.set(database=self.database_name)
        self.engine = create_async_engine(self.url, poolclass=NullPool)
        self.addAsyncCleanup(self.engine.dispose)
        self.child_environment = os.environ.copy()
        self.child_environment.update({
            "DATABASE_URL": self.url.render_as_string(hide_password=False),
            "SECRET_KEY": "migration-tests-only-not-a-production-key",
        })

    async def drop_database(self):
        async with self.admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{self.database_name}"')

    async def run_python(self, *arguments, expect_failure=False):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", *arguments,
            cwd=PROJECT_ROOT, env=self.child_environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                stdout, stderr = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        output = (stdout + stderr).decode(errors="replace")
        for value in (
            self.child_environment["DATABASE_URL"],
            self.child_environment["SECRET_KEY"],
            self.url.password,
        ):
            if value:
                output = output.replace(value, "[REDACTED]")
        if expect_failure:
            self.assertNotEqual(process.returncode, 0, "Invalid existing data was accepted")
        elif process.returncode:
            self.fail(f"Validation subprocess failed:\n{output}")
        return output

    async def snapshot(self):
        def inspect_schema(connection):
            inspector = inspect(connection)
            return {
                "tables": set(inspector.get_table_names(schema="public")),
                "enums": {enum["name"]: enum["labels"]
                          for enum in inspector.get_enums(schema="public")},
                "sequences": set(inspector.get_sequence_names(schema="public")),
            }

        async with self.engine.connect() as connection:
            return await connection.run_sync(inspect_schema)

    async def assert_metadata_matches(self, metadata=None, revision_id="b83d12f7a906"):
        metadata = metadata if metadata is not None else Base.metadata

        def compare(connection):
            context = MigrationContext.configure(connection, opts={
                "compare_type": True, "compare_server_default": True,
            })
            differences = compare_metadata(context, metadata)
            self.assertEqual(differences, [], "Migrated schema differs from model metadata")
            inspector = inspect(connection)
            for table in metadata.sorted_tables:
                self.assertEqual(
                    inspector.get_pk_constraint(table.name)["constrained_columns"],
                    [column.name for column in table.primary_key.columns],
                )
                # Autogenerate does not reliably compare CHECK expressions.
                normalize = lambda expression: re.sub(r"[\s()]", "", str(expression))
                self.assertEqual(
                    {item["name"]: normalize(item["sqltext"])
                     for item in inspector.get_check_constraints(table.name)},
                    {item.name: normalize(item.sqltext)
                     for item in table.constraints if isinstance(item, CheckConstraint)},
                )
                reflected_indexes = {item["name"]: item for item in inspector.get_indexes(table.name)}
                for index in table.indexes:
                    predicate = index.dialect_options["postgresql"].get("where")
                    if predicate is not None:
                        actual = reflected_indexes[index.name]
                        self.assertTrue(actual["unique"])
                        self.assertEqual(actual["column_names"], [column.name for column in index.columns])
                        reflected_predicate = actual["dialect_options"]["postgresql_where"]
                        self.assertEqual(
                            re.findall(r"'([^']+)'", reflected_predicate),
                            re.findall(r"'([^']+)'", str(predicate)),
                        )

        async with self.engine.connect() as connection:
            await connection.run_sync(compare)
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            self.assertEqual(revision, revision_id)
        state = await self.snapshot()
        self.assertEqual(state["tables"], set(metadata.tables) | {"alembic_version"})
        # Alembic's normal type comparison does not detect every enum-label change.
        expected_enums = {
            column.type.name: column.type.enums
            for table in metadata.tables.values()
            for column in table.columns if isinstance(column.type, Enum)
        }
        self.assertEqual(state["enums"], expected_enums)
        self.assertEqual(state["sequences"], {"users_id_seq", "rides_id_seq", "bookings_id_seq"})
        return state

    def task5_metadata(self):
        metadata = MetaData()
        for table in Base.metadata.sorted_tables:
            table.to_metadata(metadata)
        rides = metadata.tables["rides"]
        rides.indexes = {index for index in rides.indexes
                         if index.name != "ix_rides_status_departure_time_id"}
        return metadata

    def task4_metadata(self):
        metadata = self.task5_metadata()
        rides = metadata.tables["rides"]
        rides.constraints = {constraint for constraint in rides.constraints
                             if not isinstance(constraint, CheckConstraint)}
        bookings = metadata.tables["bookings"]
        bookings.indexes = {index for index in bookings.indexes
                            if index.name != "uq_bookings_active_passenger_ride"}
        rides.c.status.nullable = True
        bookings.c.status.nullable = True
        return metadata

    async def prepare_integrity_data(self, revision="head"):
        await self.run_python("-m", "alembic", "upgrade", revision)
        async with self.engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO users (id, name, email, password, cnic, phone_number, role)
                VALUES (1, 'Driver', 'driver@example.com', 'test-hash', '1', '1', 'driver'),
                       (2, 'Passenger', 'passenger@example.com', 'test-hash', '2', '2', 'passenger')
            """))
            await connection.execute(text("""
                INSERT INTO rides (driver_id, origin, destination, departure_time,
                                   available_seats, fare_per_seat, status)
                VALUES (1, 'Peshawar', 'Islamabad', now() + interval '1 day', 2, 500, 'active')
            """))

    async def execute_sql(self, statement, parameters=None):
        async with self.engine.begin() as connection:
            await connection.execute(text(statement), parameters or {})

    async def assert_sql_rejected(self, statement, sqlstate, constraint=None, parameters=None):
        with self.assertRaises(IntegrityError) as raised:
            await self.execute_sql(statement, parameters)
        self.assertEqual(raised.exception.orig.sqlstate, sqlstate)
        if constraint:
            self.assertEqual(raised.exception.orig.__cause__.constraint_name, constraint)

    async def test_postgresql_rejects_negative_seats_and_fares(self):
        await self.prepare_integrity_data()
        for field, constraint in (
            ("available_seats", "ck_rides_available_seats_nonnegative"),
            ("fare_per_seat", "ck_rides_fare_per_seat_nonnegative"),
        ):
            with self.subTest(field=field):
                await self.assert_sql_rejected(
                    f"UPDATE rides SET {field} = -1 WHERE id = 1", "23514", constraint,
                )
                await self.assert_sql_rejected(
                    f"INSERT INTO rides (driver_id, origin, destination, departure_time, available_seats, fare_per_seat, status) "
                    f"SELECT driver_id, origin, destination, departure_time, "
                    f"{'-1' if field == 'available_seats' else '2'}, "
                    f"{'-1' if field == 'fare_per_seat' else '500'}, status FROM rides WHERE id = 1",
                    "23514", constraint,
                )
        await self.execute_sql("UPDATE rides SET available_seats = 0, fare_per_seat = 0 WHERE id = 1")

    async def test_postgresql_rejects_all_duplicate_active_status_combinations(self):
        await self.prepare_integrity_data()
        insert = "INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, :status)"
        for first in ("pending", "accepted"):
            for second in ("pending", "accepted"):
                with self.subTest(first=first, second=second):
                    await self.execute_sql("DELETE FROM bookings")
                    await self.execute_sql(insert, {"status": first})
                    await self.assert_sql_rejected(
                        insert, "23505", "uq_bookings_active_passenger_ride", {"status": second},
                    )

    async def test_historical_bookings_allow_rebooking_but_cannot_duplicate_active_state(self):
        await self.prepare_integrity_data()
        for active in ("pending", "accepted"):
            with self.subTest(active=active):
                await self.execute_sql("DELETE FROM bookings")
                await self.execute_sql("""
                    INSERT INTO bookings (ride_id, passenger_id, status)
                    VALUES (1, 2, 'rejected'), (1, 2, 'cancelled'),
                           (1, 2, 'rejected'), (1, 2, 'cancelled'), (1, 2, :active)
                """, {"active": active})
                for historical in ("rejected", "cancelled"):
                    await self.assert_sql_rejected(
                        "UPDATE bookings SET status = :active WHERE status = :historical",
                        "23505", "uq_bookings_active_passenger_ride",
                        {"active": active, "historical": historical},
                    )
                await self.execute_sql("UPDATE bookings SET status = 'cancelled' WHERE status = :active", {"active": active})
                await self.execute_sql("INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, 'pending')")

    async def test_postgresql_rejects_null_ride_and_booking_statuses(self):
        await self.prepare_integrity_data()
        await self.assert_sql_rejected(
            "INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, NULL)", "23502",
        )
        await self.assert_sql_rejected(
            "INSERT INTO rides (driver_id, origin, destination, departure_time, available_seats, fare_per_seat, status) "
            "SELECT driver_id, origin, destination, departure_time, available_seats, fare_per_seat, NULL FROM rides WHERE id = 1",
            "23502",
        )
        await self.execute_sql("INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, 'pending')")
        for table in ("rides", "bookings"):
            with self.subTest(table=table):
                await self.assert_sql_rejected(f"UPDATE {table} SET status = NULL", "23502")

    async def test_integrity_revision_upgrade_downgrade_and_restore(self):
        await self.prepare_integrity_data("50872040220b")
        await self.assert_metadata_matches(self.task4_metadata(), "50872040220b")
        await self.run_python("-m", "alembic", "upgrade", "7c2e9a4b6d10")
        await self.assert_metadata_matches(self.task5_metadata(), "7c2e9a4b6d10")
        await self.run_python("-m", "alembic", "downgrade", "-1")
        await self.assert_metadata_matches(self.task4_metadata(), "50872040220b")
        # DDL downgrade removes enforcement, without removing the original data.
        await self.execute_sql("UPDATE rides SET available_seats = -1, fare_per_seat = -1, status = NULL")
        await self.execute_sql("""
            INSERT INTO bookings (ride_id, passenger_id, status)
            VALUES (1, 2, NULL), (1, 2, 'pending'), (1, 2, 'accepted')
        """)
        # Explicitly repair only this test's deliberately invalid rows for re-upgrade.
        await self.execute_sql("DELETE FROM bookings")
        await self.execute_sql("UPDATE rides SET available_seats = 2, fare_per_seat = 500, status = 'active'")
        await self.run_python("-m", "alembic", "upgrade", "head")
        await self.assert_metadata_matches()
        await self.assert_sql_rejected("UPDATE rides SET available_seats = -1", "23514", "ck_rides_available_seats_nonnegative")

    async def test_ride_search_index_upgrade_downgrade_reupgrade(self):
        await self.prepare_integrity_data("7c2e9a4b6d10")
        await self.assert_metadata_matches(self.task5_metadata(), "7c2e9a4b6d10")
        for attempt in range(2):
            await self.run_python("-m", "alembic", "upgrade", "head")
            await self.assert_metadata_matches()
            async with self.engine.connect() as connection:
                indexes = await connection.run_sync(lambda conn: inspect(conn).get_indexes("rides"))
                index = next(item for item in indexes if item["name"] == "ix_rides_status_departure_time_id")
                self.assertEqual(index["column_names"], ["status", "departure_time", "id"])
                self.assertFalse(index["unique"])
                self.assertEqual(await connection.scalar(text("SELECT count(*) FROM rides")), 1)
            if attempt == 0:
                await self.run_python("-m", "alembic", "downgrade", "-1")
                await self.assert_metadata_matches(self.task5_metadata(), "7c2e9a4b6d10")

    async def test_invalid_existing_data_aborts_migration_without_rewriting_rows(self):
        await self.prepare_integrity_data("50872040220b")
        cases = (
            ("UPDATE rides SET available_seats = -1", "ck_rides_available_seats_nonnegative"),
            ("UPDATE rides SET fare_per_seat = -1", "ck_rides_fare_per_seat_nonnegative"),
            ("UPDATE rides SET status = NULL", "contains null values"),
            ("INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, NULL)", "contains null values"),
            ("INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, 'pending'), (1, 2, 'accepted')", "uq_bookings_active_passenger_ride"),
        )
        for invalid_sql, expected_error in cases:
            with self.subTest(invariant=expected_error):
                await self.execute_sql(invalid_sql)
                async with self.engine.connect() as connection:
                    before_rides = (await connection.execute(text("SELECT * FROM rides ORDER BY id"))).all()
                    before_bookings = (await connection.execute(text("SELECT * FROM bookings ORDER BY id"))).all()
                output = await self.run_python("-m", "alembic", "upgrade", "head", expect_failure=True)
                self.assertIn(expected_error, output)
                await self.assert_metadata_matches(self.task4_metadata(), "50872040220b")
                async with self.engine.connect() as connection:
                    self.assertEqual((await connection.execute(text("SELECT * FROM rides ORDER BY id"))).all(), before_rides)
                    self.assertEqual((await connection.execute(text("SELECT * FROM bookings ORDER BY id"))).all(), before_bookings)
                await self.execute_sql("DELETE FROM bookings")
                await self.execute_sql("UPDATE rides SET available_seats = 2, fare_per_seat = 500, status = 'active'")

    async def test_empty_database_upgrade_downgrade_reupgrade_and_startup(self):
        empty = {"tables": set(), "enums": {}, "sequences": set()}
        self.assertEqual(await self.snapshot(), empty)

        # Import in a fresh interpreter, then run the real lifespan on an empty DB.
        # Any create_all/Table.create call fails, even if it creates no objects.
        await self.run_python("-c", """
import asyncio
from unittest.mock import patch
from sqlalchemy import MetaData, Table

with patch.object(MetaData, 'create_all', side_effect=AssertionError('startup called create_all')):
    with patch.object(Table, 'create', side_effect=AssertionError('startup called Table.create')):
        from main import app
        from database import engine

        async def start():
            try:
                async with app.router.lifespan_context(app):
                    pass
            finally:
                await engine.dispose()

        asyncio.run(start())
""")
        self.assertEqual(await self.snapshot(), empty, "Import/startup changed the empty schema")

        with self.subTest(stage="first upgrade"):
            await self.run_python("-m", "alembic", "upgrade", "head")
            first_schema = await self.assert_metadata_matches()

        with self.subTest(stage="downgrade to base"):
            await self.run_python("-m", "alembic", "downgrade", "base")
            # Alembic retains its own empty version table at base.
            self.assertEqual(await self.snapshot(), {
                "tables": {"alembic_version"}, "enums": {}, "sequences": set(),
            })
            async with self.engine.connect() as connection:
                count = await connection.scalar(text("SELECT count(*) FROM alembic_version"))
                self.assertEqual(count, 0)

        with self.subTest(stage="second upgrade"):
            await self.run_python("-m", "alembic", "upgrade", "head")
            self.assertEqual(await self.assert_metadata_matches(), first_schema)


if __name__ == "__main__":
    unittest.main()
