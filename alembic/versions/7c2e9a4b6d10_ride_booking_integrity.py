"""Enforce ride and active-booking integrity.

Revision ID: 7c2e9a4b6d10
Revises: 50872040220b

Existing negative seats/fares, NULL statuses, or duplicate live bookings must be
resolved explicitly before upgrading. PostgreSQL validates existing rows and
rolls back this migration on failure; no user data is deleted or rewritten.
"""

from alembic import op
import sqlalchemy as sa


revision = '7c2e9a4b6d10'
down_revision = '50872040220b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        'ck_rides_available_seats_nonnegative', 'rides', 'available_seats >= 0',
    )
    op.create_check_constraint(
        'ck_rides_fare_per_seat_nonnegative', 'rides', 'fare_per_seat >= 0',
    )
    op.alter_column('rides', 'status', existing_type=sa.Enum(name='ridestatus'), nullable=False)
    op.alter_column('bookings', 'status', existing_type=sa.Enum(name='bookingstatus'), nullable=False)
    op.create_index(
        'uq_bookings_active_passenger_ride', 'bookings', ['ride_id', 'passenger_id'],
        unique=True, postgresql_where=sa.text("status IN ('pending', 'accepted')"),
    )


def downgrade() -> None:
    op.drop_index('uq_bookings_active_passenger_ride', table_name='bookings')
    op.alter_column('bookings', 'status', existing_type=sa.Enum(name='bookingstatus'), nullable=True)
    op.alter_column('rides', 'status', existing_type=sa.Enum(name='ridestatus'), nullable=True)
    op.drop_constraint('ck_rides_fare_per_seat_nonnegative', 'rides', type_='check')
    op.drop_constraint('ck_rides_available_seats_nonnegative', 'rides', type_='check')
