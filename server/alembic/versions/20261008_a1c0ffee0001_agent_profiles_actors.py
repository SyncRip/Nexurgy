"""agent profiles and actor columns (collaboration D01)

Adds agent_profiles and the actor pair (<col>_kind / <col>_agent_id) next to
the historical user columns of submissions, decisions, approvals, deliverable
drafts, baseline snapshots, audit rows and AI calls. Existing rows are
backfilled as human by the server default; the user columns become nullable
so an agent may act without a stand-in person.
Design: .trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §2.1.

Downgrade is for development databases only and is destructive: the user
columns become NOT NULL again, so every row an agent acted on is DELETED
(agent submissions with their items, acceptance requests and decisions,
agent deliverable drafts, agent-adopted baselines, agent decisions and
engineering approvals). Rows that would block those deletes are cleaned up
first: submissions that pinned an agent-adopted baseline lose that pointer
(baseline_snapshot_id = NULL) and milestone review items that froze an agent
submission are removed. Tasks / reviews that referenced the deleted rows keep
their status, so the data is not meaningful afterwards. Deleting instead of
refusing is deliberate: the test fixture downgrades to base at every session
start, and a refusal would wedge any database left with agent rows by an
interrupted session. Never run it against data you want to keep.

Revision ID: a1c0ffee0001
Revises: 4d100eb3737d
Create Date: 2026-10-08 09:40:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0001'
down_revision: Union[str, None] = '4d100eb3737d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _actor_check(kind: str, human: str, agent: str) -> str:
    return f"({kind} = 'human' AND {human} IS NOT NULL AND {agent} IS NULL) OR ({kind} = 'agent' AND {agent} IS NOT NULL AND {human} IS NULL)"


# table -> (human column, kind column, agent column, check name)
ACTOR_TABLES = [
    ('task_submissions', 'submitted_by', 'submitted_by_kind', 'submitted_by_agent_id', 'ck_submissions_actor'),
    ('task_deliverable_drafts', 'selected_by', 'selected_by_kind', 'selected_by_agent_id', 'ck_deliverables_actor'),
    ('task_baseline_snapshots', 'adopted_by', 'adopted_by_kind', 'adopted_by_agent_id', 'ck_task_baselines_actor'),
    ('approval_decisions', 'actor_id', 'actor_kind', 'actor_agent_id', 'ck_approval_decisions_actor'),
    ('approvals', 'user_id', 'actor_kind', 'agent_id', 'ck_approvals_actor'),
]


def upgrade() -> None:
    op.create_table('agent_profiles',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('purpose', sa.String(length=20), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('role_prompt', sa.Text(), server_default='', nullable=False),
    sa.Column('model_config_id', sa.Uuid(), nullable=True),
    sa.Column('allowed_tools', postgresql.ARRAY(sa.String(length=40)), server_default='{}', nullable=False),
    sa.Column('execution_modes', postgresql.ARRAY(sa.String(length=20)), server_default='{}', nullable=False),
    sa.Column('knowledge_scope', JSONB, server_default='{}', nullable=False),
    sa.Column('limits', JSONB, server_default='{}', nullable=False),
    sa.Column('pricing', JSONB, nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("purpose IN ('execute', 'review', 'planner', 'general')", name='ck_agent_profiles_purpose'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_agent_profiles_project_name')
    )
    op.create_index(op.f('ix_agent_profiles_project_id'), 'agent_profiles', ['project_id'], unique=False)

    # Actor pair on the historical user columns (rows backfilled as human by the default).
    for table, human, kind, agent, check in ACTOR_TABLES:
        op.add_column(table, sa.Column(kind, sa.String(length=10), server_default='human', nullable=False))
        op.add_column(table, sa.Column(agent, sa.Uuid(), nullable=True))
        op.alter_column(table, human, existing_type=sa.UUID(), nullable=True)
        op.create_foreign_key(f'{table}_{agent}_fkey', table, 'agent_profiles', [agent], ['id'], ondelete='SET NULL')
        op.create_check_constraint(check, table, _actor_check(kind, human, agent))
    op.add_column('approval_decisions', sa.Column('criterion_decisions', JSONB, server_default='[]', nullable=False))

    op.add_column('audit_log', sa.Column('agent_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('audit_log_agent_id_fkey', 'audit_log', 'agent_profiles', ['agent_id'], ['id'], ondelete='SET NULL')
    op.add_column('ai_calls', sa.Column('agent_profile_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('ai_calls_agent_profile_id_fkey', 'ai_calls', 'agent_profiles', ['agent_profile_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    # Development databases only (destructive): rows acted on by agents cannot keep a NOT NULL user column.
    # Rows that reference agent-acted rows without ON DELETE are cleared before the delete (see the module docstring).
    prepare = {
        'task_baseline_snapshots': [
            "UPDATE task_submissions SET baseline_snapshot_id = NULL "
            "WHERE baseline_snapshot_id IN (SELECT id FROM task_baseline_snapshots WHERE adopted_by_kind = 'agent')",
        ],
        'task_submissions': [
            "DELETE FROM milestone_review_items "
            "WHERE submission_id IN (SELECT id FROM task_submissions WHERE submitted_by_kind = 'agent')",
        ],
    }
    op.drop_constraint('ai_calls_agent_profile_id_fkey', 'ai_calls', type_='foreignkey')
    op.drop_column('ai_calls', 'agent_profile_id')
    op.drop_constraint('audit_log_agent_id_fkey', 'audit_log', type_='foreignkey')
    op.drop_column('audit_log', 'agent_id')
    op.drop_column('approval_decisions', 'criterion_decisions')
    for table, human, kind, agent, check in reversed(ACTOR_TABLES):
        for stmt in prepare.get(table, []):
            op.execute(stmt)
        op.execute(f"DELETE FROM {table} WHERE {kind} = 'agent'")
        op.drop_constraint(check, table, type_='check')
        op.drop_constraint(f'{table}_{agent}_fkey', table, type_='foreignkey')
        op.alter_column(table, human, existing_type=sa.UUID(), nullable=False)
        op.drop_column(table, agent)
        op.drop_column(table, kind)
    op.drop_index(op.f('ix_agent_profiles_project_id'), table_name='agent_profiles')
    op.drop_table('agent_profiles')
