"""Multi-seat bookings: bookings.seats_requested (Milestone C).

Revision ID: c1a7e0b00003
Revises: c1a7e0b00002

Additive and safe for legacy data:

1. ``bookings.seats_requested integer NOT NULL DEFAULT 1``. PostgreSQL >= 11
   stores a constant default in the catalog: no table rewrite, every legacy
   row reads 1 (the only value the old code ever meant). ``ADD COLUMN IF NOT
   EXISTS`` plus a shape check keeps a retried run idempotent while refusing a
   stray column of a different definition.
2. ``ck_bookings_seats_requested_range CHECK (seats_requested >= 1 AND
   seats_requested <= 8)`` added ``NOT VALID`` (brief ACCESS EXCLUSIVE, no
   scan), then ``VALIDATE CONSTRAINT`` (SHARE UPDATE EXCLUSIVE: reads and
   writes continue during the scan). A same-named constraint with another
   definition is refused.

Each step runs in autocommit with ``lock_timeout = 5s`` so the ACCESS EXCLUSIVE
lock can never queue behind a long transaction and stall traffic; on timeout
the migration fails cleanly, the stamp stays at c1a7e0b00002 and a re-run
continues from wherever the previous attempt stopped.

No ride-level column is added: seat accounting stays the existing
``rides.available_seats`` counter, debited/restored by exactly
``seats_requested`` under the ride lock. ``seats_total`` (with its backfill)
is deferred; see docs/architecture/BOOKING_SCHEMA_CHANGES.md §3.

Downgrade REFUSES (raises, nothing changed) while any booking has
``seats_requested <> 1``: dropping the column would silently turn those into
1-seat bookings, so old code would restore 1 seat instead of N when an accepted
multi-seat booking is cancelled (seats permanently lost), and the history of
how many seats were sold would be gone. The error lists the counts per status;
the operator resolves those rows deliberately (runbook in the doc) and re-runs.
The check, constraint drop and column drop run in one transaction holding an
ACCESS EXCLUSIVE lock on bookings, so no multi-seat row can appear in between.
"""

from alembic import op
import sqlalchemy as sa


revision = "c1a7e0b00003"
down_revision = "c1a7e0b00002"
branch_labels = None
depends_on = None

COLUMN = "seats_requested"
CONSTRAINT = "ck_bookings_seats_requested_range"
CHECK_SQL = "seats_requested >= 1 AND seats_requested <= 8"
# pg_get_constraintdef() normal form of CHECK_SQL.
CHECK_DEF = "CHECK (((seats_requested >= 1) AND (seats_requested <= 8)))"


class UnexpectedSeatsColumn(RuntimeError):
    pass


class MultiSeatDowngradeRefused(RuntimeError):
    pass


def _constraint_definition(bind):
    """None when absent, else (definition without NOT VALID, convalidated)."""
    row = bind.execute(sa.text(
        "SELECT pg_get_constraintdef(c.oid), c.convalidated FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE c.conname = :name AND t.relname = 'bookings' "
        "AND t.relnamespace = to_regnamespace(current_schema())"
    ), {"name": CONSTRAINT}).first()
    if row is None:
        return None
    return row[0].replace(" NOT VALID", ""), row[1]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text("SET lock_timeout = '5s'"))
        try:
            bind.execute(sa.text(
                f"ALTER TABLE bookings ADD COLUMN IF NOT EXISTS {COLUMN} integer NOT NULL DEFAULT 1"))
            column = bind.execute(sa.text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'bookings' "
                "AND column_name = :name"
            ), {"name": COLUMN}).one()
            if tuple(column) != ("integer", "NO", "1"):
                raise UnexpectedSeatsColumn(
                    f"bookings.{COLUMN} exists with an unexpected definition {tuple(column)}; "
                    "inspect and drop it before re-running")

            existing = _constraint_definition(bind)
            if existing is None:
                bind.execute(sa.text(
                    f"ALTER TABLE bookings ADD CONSTRAINT {CONSTRAINT} CHECK ({CHECK_SQL}) NOT VALID"))
            elif existing[0] != CHECK_DEF:
                raise UnexpectedSeatsColumn(
                    f"{CONSTRAINT} exists with an unexpected definition; "
                    "inspect and drop it before re-running")
            # Idempotent; scans under SHARE UPDATE EXCLUSIVE (writes continue).
            bind.execute(sa.text(f"ALTER TABLE bookings VALIDATE CONSTRAINT {CONSTRAINT}"))
        finally:
            bind.execute(sa.text("RESET lock_timeout"))


def downgrade() -> None:
    # Transactional with the version stamp: refuse, or change everything.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE bookings IN ACCESS EXCLUSIVE MODE")
    rows = op.get_bind().execute(sa.text(
        f"SELECT status::text, count(*), sum({COLUMN}) FROM bookings "
        f"WHERE {COLUMN} <> 1 GROUP BY status ORDER BY status"
    )).all()
    if rows:
        summary = ", ".join(f"{status}={count} ({seats} seats)" for status, count, seats in rows)
        raise MultiSeatDowngradeRefused(
            f"Refusing to drop bookings.{COLUMN}: bookings with more than one seat exist "
            f"[{summary}]. Dropping it would lose seats on cancellation and the sold-seat "
            "history. Resolve these rows first (docs/architecture/BOOKING_SCHEMA_CHANGES.md "
            "§3, rollback runbook), then re-run the downgrade.")
    op.execute(f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.drop_column("bookings", COLUMN)
