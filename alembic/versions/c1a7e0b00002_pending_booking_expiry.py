"""Pending-request expiry: bookings.expires_at and the 'expired' status (Milestone C).

Revision ID: c1a7e0b00002
Revises: c1a7e0b00001

Additive and safe for legacy data:

1. ``bookings.expires_at timestamptz NULL``. Metadata-only (no rewrite, no
   default). Existing rows stay NULL, meaning "expires at the ride's
   departure" -- the rule that already applied, so no legacy request changes
   meaning. ``ADD COLUMN IF NOT EXISTS`` plus a type check keeps a retried run
   idempotent while refusing a stray column of the wrong shape.
2. ``ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'expired'``. Not used by
   this migration, and added in autocommit mode.
3. ``ix_bookings_pending_expires_at ON bookings (expires_at) WHERE status =
   'pending'`` built CONCURRENTLY (writes continue). Re-runnable: a valid index
   is kept; an INVALID one left by an interrupted build is dropped
   concurrently and rebuilt.

Steps 1-2 run with lock_timeout = 5s so the brief ACCESS EXCLUSIVE lock for
ADD COLUMN can never queue behind a long transaction and stall traffic; on
timeout the migration fails cleanly and is simply re-run.

Downgrade maps every 'expired' booking to 'cancelled' (the only pre-existing
terminal status that means "closed without a seat"), then drops the index and
the column. PostgreSQL cannot drop an enum value; the unused 'expired' label
stays in the type (harmless to old code, which never reads or writes it once
no row holds it). Re-upgrading is a no-op for the label.
"""

from alembic import op
import sqlalchemy as sa


revision = "c1a7e0b00002"
down_revision = "c1a7e0b00001"
branch_labels = None
depends_on = None

INDEX = "ix_bookings_pending_expires_at"


class UnexpectedExpiresAtColumn(RuntimeError):
    pass


def _index_validity(name: str):
    """None when absent, else pg_index.indisvalid for the index in this schema."""
    return op.get_bind().execute(sa.text(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = :name AND c.relnamespace = to_regnamespace(current_schema())"
    ), {"name": name}).scalar()


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text("SET lock_timeout = '5s'"))
        try:
            bind.execute(sa.text(
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS expires_at timestamptz NULL"))
            column = bind.execute(sa.text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'bookings' "
                "AND column_name = 'expires_at'"
            )).one()
            if tuple(column) != ("timestamp with time zone", "YES", None):
                raise UnexpectedExpiresAtColumn(
                    f"bookings.expires_at exists with an unexpected definition {tuple(column)}; "
                    "inspect and drop it before re-running")
            bind.execute(sa.text("ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'expired'"))
        finally:
            bind.execute(sa.text("RESET lock_timeout"))

        valid = _index_validity(INDEX)
        if not valid:
            if valid is False:
                op.drop_index(INDEX, table_name="bookings", postgresql_concurrently=True,
                              if_exists=True)
            op.create_index(INDEX, "bookings", ["expires_at"], unique=False,
                            postgresql_concurrently=True,
                            postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name="bookings", postgresql_concurrently=True, if_exists=True)
    # Transactional with the version stamp.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE bookings SET status = 'cancelled' WHERE status = 'expired'")
    op.drop_column("bookings", "expires_at")
