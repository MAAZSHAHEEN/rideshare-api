"""Append-only admin audit log (Milestone D, M5-B).

Revision ID: d5e1a0b00002
Revises: d5e1a0b00001

Creates admin_audit_log plus a trigger function that rejects every UPDATE,
DELETE and TRUNCATE, for every role including the table owner. (A superuser
can still disable triggers; restricting the app role to INSERT/SELECT is the
third layer and an approval-gated production role change, not done here.)

New, empty table: no legacy rows, no rewrite, instant.

Downgrade drops the triggers, the function and the table. THAT DELETES THE
AUDIT HISTORY: on any database with real admin actions, export it first,
e.g. \copy admin_audit_log TO 'admin_audit_log.csv' CSV HEADER.
The SQL below is a frozen copy of models.AUDIT_LOG_*_SQL.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d5e1a0b00002"
down_revision = "d5e1a0b00001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_admin_audit_log_target", "admin_audit_log",
                    ["target_type", "target_id", "id"])
    op.create_index("ix_admin_audit_log_actor_id_id", "admin_audit_log", ["actor_id", "id"])
    op.execute("""
        CREATE FUNCTION admin_audit_log_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'admin_audit_log is append-only (% blocked)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
    """)
    op.execute(
        "CREATE TRIGGER admin_audit_log_no_update_delete BEFORE UPDATE OR DELETE "
        "ON admin_audit_log FOR EACH ROW EXECUTE FUNCTION admin_audit_log_immutable()"
    )
    op.execute(
        "CREATE TRIGGER admin_audit_log_no_truncate BEFORE TRUNCATE "
        "ON admin_audit_log FOR EACH STATEMENT EXECUTE FUNCTION admin_audit_log_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER admin_audit_log_no_truncate ON admin_audit_log")
    op.execute("DROP TRIGGER admin_audit_log_no_update_delete ON admin_audit_log")
    op.execute("DROP FUNCTION admin_audit_log_immutable()")
    op.drop_index("ix_admin_audit_log_actor_id_id", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_log_target", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
