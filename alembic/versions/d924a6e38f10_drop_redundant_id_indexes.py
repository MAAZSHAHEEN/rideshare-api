"""Drop ID indexes already covered by primary keys.

Revision ID: d924a6e38f10
Revises: b83d12f7a906
"""

from alembic import op


revision = "d924a6e38f10"
down_revision = "b83d12f7a906"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_users_id", table_name="users")
    op.drop_index("ix_rides_id", table_name="rides")
    op.drop_index("ix_bookings_id", table_name="bookings")


def downgrade() -> None:
    op.create_index("ix_users_id", "users", ["id"], unique=False)
    op.create_index("ix_rides_id", "rides", ["id"], unique=False)
    op.create_index("ix_bookings_id", "bookings", ["id"], unique=False)
