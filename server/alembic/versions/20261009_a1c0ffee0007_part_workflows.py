"""Part workflows: workflows.part_id (R1.2 web refactor)

A workflow with part_id is that part's own working copy (started from a
template, edited for the part); NULL keeps it a project template.

Revision ID: a1c0ffee0007
Revises: a1c0ffee0006
Create Date: 2026-10-09 18:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0007'
down_revision: Union[str, None] = 'a1c0ffee0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('part_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key('fk_workflows_part_id_parts', 'workflows', 'parts', ['part_id'], ['id'], ondelete='CASCADE')
    op.create_index('ix_workflows_part_id', 'workflows', ['part_id'])


def downgrade() -> None:
    op.drop_index('ix_workflows_part_id', table_name='workflows')
    op.drop_constraint('fk_workflows_part_id_parts', 'workflows', type_='foreignkey')
    op.drop_column('workflows', 'part_id')
