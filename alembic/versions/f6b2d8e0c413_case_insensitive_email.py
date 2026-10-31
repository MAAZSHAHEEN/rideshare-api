"""Enforce case-insensitive email identity (AST-005).

Revision ID: f6b2d8e0c413
Revises: e3a7c41b9f52

Adds a unique index on lower(email). Before creating it, the migration checks
for existing accounts whose emails differ only by case. If any exist it aborts
with a count and the affected user IDs (never the addresses) and changes
nothing; those accounts must be merged or renamed by an operator first.
Existing rows are not rewritten: login matches them case-insensitively.
"""

from alembic import op
import sqlalchemy as sa


revision = "f6b2d8e0c413"
down_revision = "e3a7c41b9f52"
branch_labels = None
depends_on = None


class CaseDuplicateEmails(RuntimeError):
    pass


def upgrade() -> None:
    duplicates = op.get_bind().execute(sa.text(
        "SELECT array_agg(id ORDER BY id) FROM users "
        "GROUP BY lower(email) HAVING count(*) > 1 ORDER BY min(id)"
    )).scalars().all()
    if duplicates:
        raise CaseDuplicateEmails(
            f"{len(duplicates)} email group(s) differ only by case; resolve before "
            f"upgrading. Affected user IDs: {duplicates}"
        )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_email_lower", table_name="users")
