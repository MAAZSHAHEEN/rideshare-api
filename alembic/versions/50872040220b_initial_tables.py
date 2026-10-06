"""initial tables

Revision ID: 50872040220b
Revises: 
Create Date: 2026-02-27 10:01:49.328748

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50872040220b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('password', sa.String(), nullable=False),
        sa.Column('cnic', sa.String(), nullable=False),
        sa.Column('phone_number', sa.String(), nullable=False),
        sa.Column('role', sa.Enum('passenger', 'driver', 'admin', name='userrole'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cnic'),
    )
    op.create_index('ix_users_id', 'users', ['id'], unique=False)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    op.create_table(
        'rides',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('driver_id', sa.Integer(), nullable=False),
        sa.Column('origin', sa.String(), nullable=False),
        sa.Column('destination', sa.String(), nullable=False),
        sa.Column('departure_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('available_seats', sa.Integer(), nullable=False),
        sa.Column('fare_per_seat', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('active', 'completed', 'cancelled', name='ridestatus'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['driver_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rides_id', 'rides', ['id'], unique=False)

    op.create_table(
        'bookings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ride_id', sa.Integer(), nullable=False),
        sa.Column('passenger_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'accepted', 'rejected', 'cancelled', name='bookingstatus'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['ride_id'], ['rides.id']),
        sa.ForeignKeyConstraint(['passenger_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bookings_id', 'bookings', ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL drops table-owned indexes and sequences with their tables.
    op.drop_table('bookings')
    op.drop_table('rides')
    op.drop_table('users')
    # Enum types are independent schema objects and must be removed explicitly.
    sa.Enum(name='bookingstatus').drop(op.get_bind())
    sa.Enum(name='ridestatus').drop(op.get_bind())
    sa.Enum(name='userrole').drop(op.get_bind())
