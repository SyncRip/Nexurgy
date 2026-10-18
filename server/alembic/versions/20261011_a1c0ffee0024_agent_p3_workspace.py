"""P3 workspace agent: the timeline's workspace event types

Owned by the P3 package (.trellis/tasks/10-11-agent-execution-layer/design.md §8):

- ck_agent_execution_events_type also allows ``command`` (a workspace terminal command), ``file_change`` (a file the
  workspace agent wrote) and ``confirmation`` (a workspace action waiting for / decided by a person).

Downgrade keeps the history: events of the new types become ``system`` events whose code is their old type.

Revision ID: a1c0ffee0024
Revises: a1c0ffee0023
Create Date: 2026-10-11 20:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1c0ffee0024'
down_revision: Union[str, None] = 'a1c0ffee0023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

P1_TYPES = ("system", "model_call", "message", "tool_call", "tool_result", "question", "deliver", "blocked", "verdict", "status")
P3_TYPES = ("command", "file_change", "confirmation")


def _check(types: tuple[str, ...]) -> str:
    return "type IN (" + ", ".join(f"'{t}'" for t in types) + ")"


def upgrade() -> None:
    op.drop_constraint('ck_agent_execution_events_type', 'agent_execution_events', type_='check')
    op.create_check_constraint('ck_agent_execution_events_type', 'agent_execution_events', _check(P1_TYPES + P3_TYPES))


def downgrade() -> None:
    op.execute("UPDATE agent_execution_events SET payload = payload || jsonb_build_object('code', type, 'message', ''), type = 'system' "
               "WHERE type IN ('command', 'file_change', 'confirmation')")
    op.drop_constraint('ck_agent_execution_events_type', 'agent_execution_events', type_='check')
    op.create_check_constraint('ck_agent_execution_events_type', 'agent_execution_events', _check(P1_TYPES))
