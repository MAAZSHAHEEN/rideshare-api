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
not blocked during the build. If a concurrent build is interrupted, PostgreSQL
leaves an INVALID index: run ``alembic downgrade d924a6e38f10`` (drops them
concurrently with IF EXISTS) and upgrade again.
"""

from alembic import op


revision = "e3a7c41b9f52"
down_revision = "d924a6e38f10"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_rides_driver_id_departure_time_id", "rides", ["driver_id", "departure_time", "id"]),
    ("ix_bookings_passenger_id_id", "bookings", ["passenger_id", "id"]),
    ("ix_bookings_ride_id_id", "bookings", ["ride_id", "id"]),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns in INDEXES:
            op.create_index(name, table, columns, unique=False, postgresql_concurrently=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _ in reversed(INDEXES):
            op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
