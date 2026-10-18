"""CAD AI: revision requirements / checks, drawing packages, assistant drawing operations

Design: .trellis/tasks/archive/2026-10/10-08-cad-ai/design.md §6.

Revision ID: a1c0ffee0006
Revises: a1c0ffee0005
Create Date: 2026-10-09 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0006'
down_revision: Union[str, None] = 'a1c0ffee0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
OLD_KINDS = "kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask')"
NEW_KINDS = ("kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask', "
             "'drawing_generate', 'drawing_reconstruct')")


def upgrade() -> None:
    op.add_column('model_revisions', sa.Column('requirements', JSONB, nullable=True))
    op.add_column('model_revisions', sa.Column('checks', JSONB, nullable=True))
    op.add_column('drawings', sa.Column('pages', JSONB, server_default='[]', nullable=False))
    op.add_column('drawings', sa.Column('manifest', JSONB, nullable=True))
    op.add_column('drawings', sa.Column('manifest_sha256', sa.String(length=64), nullable=True))
    op.add_column('drawings', sa.Column('checks', JSONB, nullable=True))
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', NEW_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM ai_operations WHERE kind IN ('drawing_generate', 'drawing_reconstruct')")
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', OLD_KINDS)
    op.drop_column('drawings', 'checks')
    op.drop_column('drawings', 'manifest_sha256')
    op.drop_column('drawings', 'manifest')
    op.drop_column('drawings', 'pages')
    op.drop_column('model_revisions', 'checks')
    op.drop_column('model_revisions', 'requirements')
