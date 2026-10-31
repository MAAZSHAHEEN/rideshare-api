"""Account status and token version for suspension (Milestone D, M5-A).

Revision ID: d5e1a0b00001
Revises: c1a7e0b00003

Adds users.account_status VARCHAR(16) NOT NULL DEFAULT 'active' and
users.token_version INTEGER NOT NULL DEFAULT 0.

Legacy safety and locking (PostgreSQL >= 11; CI and local run 17):
- ADD COLUMN ... NOT NULL DEFAULT <constant> is a catalog-only change: the
  default is stored as the column's "missing value" and the table is NOT
  rewritten, so every existing row reads 'active' / 0 immediately. The
  ACCESS EXCLUSIVE lock is held only for the catalog update.
- The two CHECK constraints need one scan of users to validate. env.py runs
  each revision in a single transaction, so that scan happens while the
  ACCESS EXCLUSIVE lock from ADD COLUMN is still held (NOT VALID + VALIDATE
  would not help inside one transaction). users is small (one row per
  account), so this is a sub-second hold; every legacy row holds the
  defaults, so validation cannot fail.
- lock_timeout is bounded so a long-running transaction on users makes the
  migration fail fast instead of queueing every login behind it.

Chain (SPRINT_PLAN): f6b2d8e0c413 -> c1a7e0b00001 -> c1a7e0b00002 ->
c1a7e0b00003 -> d5e1a0b00001 -> d5e1a0b00002. Re-chained from f6b2d8e0c413
onto c1a7e0b00003 on 31 Oct 2026; the operations are unchanged.

Downgrade drops both columns (and their constraints). Suspension state and
token versions are lost; a suspended user becomes active again, which is why
the rollback plan disables admin suspension first (see the PR).
"""

from alembic import op
import sqlalchemy as sa


revision = "d5e1a0b00001"
down_revision = "c1a7e0b00003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column(
        "account_status", sa.String(length=16), nullable=False,
        server_default=sa.text("'active'"),
    ))
    op.add_column("users", sa.Column(
        "token_version", sa.Integer(), nullable=False, server_default=sa.text("0"),
    ))
    op.create_check_constraint(
        "ck_users_account_status", "users", "account_status IN ('active', 'suspended')",
    )
    op.create_check_constraint(
        "ck_users_token_version_nonnegative", "users", "token_version >= 0",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_users_token_version_nonnegative", "users", type_="check")
    op.drop_constraint("ck_users_account_status", "users", type_="check")
    op.drop_column("users", "token_version")
    op.drop_column("users", "account_status")
