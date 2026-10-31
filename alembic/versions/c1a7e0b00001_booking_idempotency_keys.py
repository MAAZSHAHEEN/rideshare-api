"""Idempotency keys for booking writes (Milestone C, AST-022).

Revision ID: c1a7e0b00001
Revises: f6b2d8e0c413

Additive only: one new, empty table. No existing table is altered or
rewritten, so legacy rows cannot violate anything and the upgrade takes no
lock on users, rides or bookings beyond the brief SHARE ROW EXCLUSIVE lock on
users that creating the foreign key needs (no scan: the new table is empty).

- PRIMARY KEY (user_id, key) is the uniqueness guarantee every concurrent
  duplicate request serialises on.
- ix_idempotency_keys_expires_at serves the TTL purge job. The table is new
  and empty, so a plain (non-concurrent) CREATE INDEX is instant.

Downgrade drops the table (and with it every stored replay). Application code
without idempotency support ignores the header, so downgrade after a code
revert loses nothing but the ability to replay retries.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c1a7e0b00001"
down_revision = "f6b2d8e0c413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(key::text) >= 1", name="ck_idempotency_keys_key_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "key", name="pk_idempotency_keys"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
