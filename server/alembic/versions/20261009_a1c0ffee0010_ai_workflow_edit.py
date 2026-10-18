"""AI workspace: workflow_edit operations (an AI change plan for a part workflow, applied on confirmation)

The plan and its application state live in ai_operations.result (JSONB); only the kind check changes.

Revision ID: a1c0ffee0010
Revises: a1c0ffee0009
Create Date: 2026-10-09 22:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1c0ffee0010'
down_revision: Union[str, None] = 'a1c0ffee0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_KINDS = ("kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask', "
             "'drawing_generate', 'drawing_reconstruct')")
NEW_KINDS = ("kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask', "
             "'drawing_generate', 'drawing_reconstruct', 'workflow_edit')")


def upgrade() -> None:
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', NEW_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM ai_operations WHERE kind = 'workflow_edit'")
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', OLD_KINDS)
