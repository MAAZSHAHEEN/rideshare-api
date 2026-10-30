"""Index foreign keys used by the owner listing and booking lock paths (AST-003).

Revision ID: e3a7c41b9f52
Revises: d924a6e38f10

Additive only. Each composite index leads with a foreign key, so it also serves
plain FK lookups and ON DELETE checks:

- ix_rides_driver_id_departure_time_id: GET /rides/me filters by driver_id and
  orders by departure_time DESC, id DESC (served by a backward index scan).
- ix_bookings_passenger_id_id: GET /bookings/me filters by passenger_id and
  orders by id DESC.
- ix_bookings_ride_id_id: driver booking lists, ride cancel/complete booking
  locks (ORDER BY id) and every booking lookup by ride.

Indexes are built CONCURRENTLY in an autocommit block so production writes are
not blocked during the build. The upgrade is re-runnable: an index that already
exists and is valid is kept, and an INVALID index left by an interrupted
concurrent build is dropped concurrently and rebuilt. A concurrent build waits
for transactions already open on the table, so avoid long-running transactions
while it runs.
"""

from alembic import op
import sqlalchemy as sa


revision = "e3a7c41b9f52"
down_revision = "d924a6e38f10"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_rides_driver_id_departure_time_id", "rides", ["driver_id", "departure_time", "id"]),
    ("ix_bookings_passenger_id_id", "bookings", ["passenger_id", "id"]),
    ("ix_bookings_ride_id_id", "bookings", ["ride_id", "id"]),
)


def _index_validity(name: str):
    """None when absent, else pg_index.indisvalid for the index in this schema."""
    return op.get_bind().execute(sa.text(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = :name AND c.relnamespace = to_regnamespace(current_schema())"
    ), {"name": name}).scalar()


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns in INDEXES:
            valid = _index_validity(name)
            if valid:
                continue
            if valid is False:
                op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
            op.create_index(name, table, columns, unique=False, postgresql_concurrently=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _ in reversed(INDEXES):
            op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
