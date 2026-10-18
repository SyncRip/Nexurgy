"""Versioned project preferences for the retained assistant's engineering team.

Revision ID: a1c0ffee0034
Revises: a1c0ffee0033
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1c0ffee0034"
down_revision = "a1c0ffee0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_engineering_teams",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_project_engineering_teams_version"),
    )


def downgrade() -> None:
    op.drop_table("project_engineering_teams")
