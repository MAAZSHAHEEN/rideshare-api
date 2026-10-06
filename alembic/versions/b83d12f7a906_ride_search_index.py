"""Index active ride search by departure time and stable ID order.

Revision ID: b83d12f7a906
Revises: 7c2e9a4b6d10
"""

from alembic import op


revision = "b83d12f7a906"
down_revision = "7c2e9a4b6d10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_rides_status_departure_time_id", "rides", ["status", "departure_time", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_rides_status_departure_time_id", table_name="rides")
