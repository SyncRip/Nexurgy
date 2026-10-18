"""Enable Turbo by default for new projects without changing saved policies.

Revision ID: a1c0ffee0035
Revises: a1c0ffee0034
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c0ffee0035"
down_revision = "a1c0ffee0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("projects", "default_execution_policy", existing_type=sa.String(length=20),
                    server_default="turbo", existing_nullable=False)


def downgrade() -> None:
    op.alter_column("projects", "default_execution_policy", existing_type=sa.String(length=20),
                    server_default="standard", existing_nullable=False)
