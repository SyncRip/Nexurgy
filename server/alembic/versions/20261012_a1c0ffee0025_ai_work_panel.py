"""AI work panel: card messages and workflow_create operations

Owned by package B of the AI work panel (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §2.4–§2.5):

- ck_ai_messages_kind also allows ``card`` (a run / workflow the conversation follows; payload names the object).
- ck_ai_operations_kind also allows ``workflow_create`` (a new part workflow planned from a built-in template).

Downgrade removes the card messages and the workflow_create operations first (their context builds go with them;
messages and calls that pointed at an operation keep their rows, the pointer becomes NULL).

Revision ID: a1c0ffee0025
Revises: a1c0ffee0024
Create Date: 2026-10-12 10:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1c0ffee0025'
down_revision: Union[str, None] = 'a1c0ffee0024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MESSAGE_KINDS = ("request", "operation", "answer", "choice", "notice")
OPERATION_KINDS = ("task_draft", "model_create", "model_modify", "drawing_intake", "ask", "drawing_generate", "drawing_reconstruct",
                   "workflow_edit", "spec_extract", "assembly_create", "assembly_modify", "drafting_create")


def _check(values: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint('ck_ai_messages_kind', 'ai_messages', type_='check')
    op.create_check_constraint('ck_ai_messages_kind', 'ai_messages', _check(MESSAGE_KINDS + ("card",)))
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', _check(OPERATION_KINDS + ("workflow_create",)))


def downgrade() -> None:
    op.execute("DELETE FROM ai_messages WHERE kind = 'card'")
    op.execute("DELETE FROM ai_operations WHERE kind = 'workflow_create'")
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', _check(OPERATION_KINDS))
    op.drop_constraint('ck_ai_messages_kind', 'ai_messages', type_='check')
    op.create_check_constraint('ck_ai_messages_kind', 'ai_messages', _check(MESSAGE_KINDS))
