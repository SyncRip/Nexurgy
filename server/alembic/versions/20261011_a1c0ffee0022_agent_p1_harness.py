"""P1 agent harness: profile runtime, executor columns, execution events, model diagnose results

Owned by the P1 package (.trellis/tasks/10-11-agent-execution-layer/design.md §6.3, docs/agent/README.zh-CN.md §4.7):

- agent_profiles.runtime (native | tool_agent | workspace_agent, default native) and runtime_options (closed JSON
  structure, default {}); both are frozen into collaboration_runs.role_snapshots when a run starts;
- agent_executions.runtime / runtime_impl (framework and version, e.g. "pydantic-ai 2.55.0") / runtime_ref (session id)
  / stop_requested_at (a person asked to stop the attempt);
- agent_execution_events: the live timeline of one attempt, written under the lease in its own transaction;
  (execution_id, seq) is unique and the SSE stream resumes by seq;
- model_configs.last_diagnose: the latest capability diagnosis (tool calling, parallel calls, strict, usage, streaming),
  bound to the configuration version like last_test.

Additive only: existing profiles stay native, existing executions have no runtime recorded. The MCP token columns are
P2's (a1c0ffee0023).

Revision ID: a1c0ffee0022
Revises: a1c0ffee0021
Create Date: 2026-10-11 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0022'
down_revision: Union[str, None] = 'a1c0ffee0021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIMES = "('native', 'tool_agent', 'workspace_agent')"
EVENT_TYPES = ("('system', 'model_call', 'message', 'tool_call', 'tool_result', 'question', 'deliver', 'blocked', "
               "'verdict', 'status')")


def upgrade() -> None:
    op.add_column('agent_profiles', sa.Column('runtime', sa.String(length=20), server_default='native', nullable=False))
    op.add_column('agent_profiles', sa.Column('runtime_options', postgresql.JSONB(astext_type=sa.Text()), server_default='{}',
                                              nullable=False))
    op.create_check_constraint('ck_agent_profiles_runtime', 'agent_profiles', f"runtime IN {RUNTIMES}")

    op.add_column('agent_executions', sa.Column('runtime', sa.String(length=20), nullable=True))
    op.add_column('agent_executions', sa.Column('runtime_impl', sa.String(length=60), nullable=True))
    op.add_column('agent_executions', sa.Column('runtime_ref', sa.String(length=200), nullable=True))
    op.add_column('agent_executions', sa.Column('stop_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint('ck_agent_executions_runtime', 'agent_executions', f"runtime IS NULL OR runtime IN {RUNTIMES}")
    op.create_index('ix_agent_executions_profile_created', 'agent_executions', ['agent_profile_id', 'created_at'])

    op.create_table(
        'agent_execution_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('execution_id', sa.Uuid(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(f"type IN {EVENT_TYPES}", name='ck_agent_execution_events_type'),
        sa.ForeignKeyConstraint(['execution_id'], ['agent_executions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('execution_id', 'seq', name='uq_agent_execution_events_seq'),
    )

    op.add_column('model_configs', sa.Column('last_diagnose', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('model_configs', 'last_diagnose')
    op.drop_table('agent_execution_events')
    op.drop_index('ix_agent_executions_profile_created', table_name='agent_executions')
    op.drop_constraint('ck_agent_executions_runtime', 'agent_executions', type_='check')
    op.drop_column('agent_executions', 'stop_requested_at')
    op.drop_column('agent_executions', 'runtime_ref')
    op.drop_column('agent_executions', 'runtime_impl')
    op.drop_column('agent_executions', 'runtime')
    op.drop_constraint('ck_agent_profiles_runtime', 'agent_profiles', type_='check')
    op.drop_column('agent_profiles', 'runtime_options')
    op.drop_column('agent_profiles', 'runtime')
