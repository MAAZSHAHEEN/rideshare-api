"""Migration lifecycle against a genuinely empty disposable PostgreSQL database.

Set TEST_DATABASE_URL to a dedicated postgresql+asyncpg test database. Its user
needs CREATEDB permission. This test creates a new database from template0 and
drops only that database afterward; it never migrates the configured database.
Run: python -B -m unittest discover -s tests -p test_migrations.py -v
"""

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Enum, inspect, text
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

    async def run_python(self, *arguments):
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
        if process.returncode:
            output = (stdout + stderr).decode(errors="replace")
            for value in (
                self.child_environment["DATABASE_URL"],
                self.child_environment["SECRET_KEY"],
                self.url.password,
            ):
                if value:
                    output = output.replace(value, "[REDACTED]")
            self.fail(f"Validation subprocess failed:\n{output}")

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

    async def assert_metadata_matches(self):
        def compare(connection):
            context = MigrationContext.configure(connection, opts={
                "compare_type": True, "compare_server_default": True,
            })
            differences = compare_metadata(context, Base.metadata)
            self.assertEqual(differences, [], "Migrated schema differs from model metadata")
            inspector = inspect(connection)
            for table in Base.metadata.sorted_tables:
                self.assertEqual(
                    inspector.get_pk_constraint(table.name)["constrained_columns"],
                    [column.name for column in table.primary_key.columns],
                )
                self.assertEqual(inspector.get_check_constraints(table.name), [])

        async with self.engine.connect() as connection:
            await connection.run_sync(compare)
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            self.assertEqual(revision, "50872040220b")
        state = await self.snapshot()
        self.assertEqual(state["tables"], set(Base.metadata.tables) | {"alembic_version"})
        # Alembic's normal type comparison does not detect every enum-label change.
        expected_enums = {
            column.type.name: column.type.enums
            for table in Base.metadata.tables.values()
            for column in table.columns if isinstance(column.type, Enum)
        }
        self.assertEqual(state["enums"], expected_enums)
        self.assertEqual(state["sequences"], {"users_id_seq", "rides_id_seq", "bookings_id_seq"})
        return state

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
