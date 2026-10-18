"""collaboration_node_tasks.clarifications: answers to the questions an agent was blocked on

Revision ID: a1c0ffee0009
Revises: a1c0ffee0008
Create Date: 2026-10-09 20:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0009'
down_revision: Union[str, None] = 'a1c0ffee0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collaboration_node_tasks', sa.Column('clarifications', postgresql.JSONB(astext_type=sa.Text()),
                                                        server_default='[]', nullable=False))


def downgrade() -> None:
    op.drop_column('collaboration_node_tasks', 'clarifications')
