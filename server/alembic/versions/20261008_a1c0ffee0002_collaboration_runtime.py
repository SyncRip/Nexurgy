"""collaboration drafts, runs, node tasks and agent executions (D02 / D03)

Design: .trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §2.2. The link
from collaboration_runs to knowledge_collections is added by the next revision
(the knowledge tables come with D04 / D05).

Revision ID: a1c0ffee0002
Revises: a1c0ffee0001
Create Date: 2026-10-08 09:41:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0002'
down_revision: Union[str, None] = 'a1c0ffee0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column('workflows', sa.Column('kind', sa.String(length=20), server_default='tool', nullable=False))
    op.create_check_constraint('ck_workflows_kind', 'workflows', "kind IN ('tool', 'collaboration')")
    op.add_column('workflow_runs', sa.Column('paused', sa.Boolean(), server_default='false', nullable=False))

    op.create_table('collaboration_drafts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('demand', sa.Text(), nullable=False),
    sa.Column('execution_mode', sa.String(length=20), nullable=False),
    sa.Column('planning_source', sa.String(length=10), server_default='ai', nullable=False),
    sa.Column('planner_agent_id', sa.Uuid(), nullable=True),
    sa.Column('executor_kind', sa.String(length=10), server_default='human', nullable=False),
    sa.Column('executor_user_id', sa.Uuid(), nullable=True),
    sa.Column('executor_agent_id', sa.Uuid(), nullable=True),
    sa.Column('reviewer_kind', sa.String(length=10), server_default='human', nullable=False),
    sa.Column('reviewer_user_id', sa.Uuid(), nullable=True),
    sa.Column('reviewer_agent_id', sa.Uuid(), nullable=True),
    sa.Column('pre_reviewer_agent_id', sa.Uuid(), nullable=True),
    sa.Column('part_id', sa.Uuid(), nullable=True),
    sa.Column('fixed_inputs', JSONB, server_default='[]', nullable=False),
    sa.Column('knowledge_item_ids', postgresql.ARRAY(sa.Uuid()), server_default='{}', nullable=False),
    sa.Column('missing_information', JSONB, server_default='[]', nullable=False),
    sa.Column('validation_errors', JSONB, server_default='[]', nullable=False),
    sa.Column('planning', JSONB, server_default='{}', nullable=False),
    sa.Column('summary', sa.Text(), server_default='', nullable=False),
    sa.Column('ready_to_start', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("execution_mode IN ('full_auto', 'semi_auto')", name='ck_collab_drafts_mode'),
    sa.CheckConstraint("executor_kind IN ('human', 'agent')", name='ck_collab_drafts_executor_kind'),
    sa.CheckConstraint("planning_source IN ('ai', 'manual')", name='ck_collab_drafts_source'),
    sa.CheckConstraint("reviewer_kind IN ('human', 'agent')", name='ck_collab_drafts_reviewer_kind'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['executor_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['executor_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['part_id'], ['parts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['planner_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['pre_reviewer_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewer_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewer_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workflow_id')
    )
    op.create_index(op.f('ix_collaboration_drafts_project_id'), 'collaboration_drafts', ['project_id'], unique=False)

    op.create_table('collaboration_runs',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=True),
    sa.Column('template_version_id', sa.Uuid(), nullable=True),
    sa.Column('draft_id', sa.Uuid(), nullable=True),
    sa.Column('execution_mode', sa.String(length=20), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('request_hash', sa.String(length=64), nullable=True),
    sa.Column('contract', JSONB, nullable=False),
    sa.Column('contract_hash', sa.String(length=64), nullable=False),
    sa.Column('role_snapshots', JSONB, nullable=False),
    sa.Column('control_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pause_reason', sa.Text(), nullable=True),
    sa.Column('forked_from_run_id', sa.Uuid(), nullable=True),
    sa.Column('fork_reason', sa.Text(), nullable=True),
    sa.Column('archive_status', sa.String(length=20), server_default='none', nullable=False),
    sa.Column('archive_error', sa.Text(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("archive_status IN ('none', 'pending', 'archived', 'failed')", name='ck_collab_runs_archive'),
    sa.CheckConstraint("execution_mode IN ('full_auto', 'semi_auto')", name='ck_collab_runs_mode'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['draft_id'], ['collaboration_drafts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['forked_from_run_id'], ['workflow_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['template_version_id'], ['workflow_template_versions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('run_id'),
    sa.UniqueConstraint('project_id', 'idempotency_key', name='uq_collab_runs_idempotency')
    )
    op.create_index(op.f('ix_collaboration_runs_project_id'), 'collaboration_runs', ['project_id'], unique=False)

    op.create_table('collaboration_node_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('node_id', sa.String(length=60), nullable=False),
    sa.Column('node_run_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('executor_kind', sa.String(length=10), server_default='human', nullable=False),
    sa.Column('executor_user_id', sa.Uuid(), nullable=True),
    sa.Column('executor_agent_id', sa.Uuid(), nullable=True),
    sa.Column('reviewer_kind', sa.String(length=10), server_default='human', nullable=False),
    sa.Column('reviewer_user_id', sa.Uuid(), nullable=True),
    sa.Column('reviewer_agent_id', sa.Uuid(), nullable=True),
    sa.Column('pre_reviewer_agent_id', sa.Uuid(), nullable=True),
    sa.Column('input_slots', JSONB, nullable=False),
    sa.Column('output_slots', JSONB, nullable=False),
    sa.Column('recipe', JSONB, nullable=False),
    sa.Column('limits', JSONB, nullable=False),
    sa.Column('phase', sa.String(length=20), server_default='blocked', nullable=False),
    sa.Column('blocked_reason', JSONB, nullable=True),
    sa.Column('budget', JSONB, server_default='{}', nullable=False),
    sa.Column('current_submission_id', sa.Uuid(), nullable=True),
    sa.Column('accepted_submission_id', sa.Uuid(), nullable=True),
    sa.Column('bound_inputs', JSONB, server_default='[]', nullable=False),
    sa.Column('formal_outputs', JSONB, server_default='[]', nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_token', sa.Uuid(), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("executor_kind IN ('human', 'agent')", name='ck_collab_node_tasks_executor_kind'),
    sa.CheckConstraint("phase IN ('blocked', 'ready', 'executing', 'submitted', 'in_review', 'rework', 'accepted', 'failed', 'cancelled')", name='ck_collab_node_tasks_phase'),
    sa.CheckConstraint("reviewer_kind IN ('human', 'agent')", name='ck_collab_node_tasks_reviewer_kind'),
    sa.ForeignKeyConstraint(['accepted_submission_id'], ['task_submissions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['current_submission_id'], ['task_submissions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['executor_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['executor_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_run_id'], ['node_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['pre_reviewer_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewer_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewer_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('node_run_id'),
    sa.UniqueConstraint('run_id', 'node_id', name='uq_collab_node_tasks_run_node'),
    sa.UniqueConstraint('task_id')
    )
    op.create_index('ix_collab_node_tasks_queue', 'collaboration_node_tasks', ['phase', 'lease_until'], unique=False)
    op.create_index(op.f('ix_collaboration_node_tasks_run_id'), 'collaboration_node_tasks', ['run_id'], unique=False)

    op.create_table('agent_executions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('node_task_id', sa.Uuid(), nullable=False),
    sa.Column('agent_profile_id', sa.Uuid(), nullable=True),
    sa.Column('profile_version', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('rework_no', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_token', sa.Uuid(), nullable=True),
    sa.Column('steps', JSONB, nullable=False),
    sa.Column('tool_calls', JSONB, nullable=False),
    sa.Column('result', JSONB, nullable=True),
    sa.Column('error', JSONB, nullable=True),
    sa.Column('usage', JSONB, nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('submission_id', sa.Uuid(), nullable=True),
    sa.Column('decision_id', sa.Uuid(), nullable=True),
    sa.Column('superseded_by_id', sa.Uuid(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('execute', 'pre_review', 'review', 'planner')", name='ck_agent_executions_role'),
    sa.CheckConstraint("status IN ('running', 'delivered', 'blocked', 'failed', 'superseded', 'cancelled')", name='ck_agent_executions_status'),
    sa.ForeignKeyConstraint(['agent_profile_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['decision_id'], ['approval_decisions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_task_id'], ['collaboration_node_tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['submission_id'], ['task_submissions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['superseded_by_id'], ['agent_executions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agent_executions_node_task', 'agent_executions', ['node_task_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_agent_executions_run_id'), 'agent_executions', ['run_id'], unique=False)

    op.add_column('ai_calls', sa.Column('agent_execution_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('ai_calls_agent_execution_id_fkey', 'ai_calls', 'agent_executions', ['agent_execution_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('ai_calls_agent_execution_id_fkey', 'ai_calls', type_='foreignkey')
    op.drop_column('ai_calls', 'agent_execution_id')
    op.drop_index(op.f('ix_agent_executions_run_id'), table_name='agent_executions')
    op.drop_index('ix_agent_executions_node_task', table_name='agent_executions')
    op.drop_table('agent_executions')
    op.drop_index(op.f('ix_collaboration_node_tasks_run_id'), table_name='collaboration_node_tasks')
    op.drop_index('ix_collab_node_tasks_queue', table_name='collaboration_node_tasks')
    op.drop_table('collaboration_node_tasks')
    op.drop_index(op.f('ix_collaboration_runs_project_id'), table_name='collaboration_runs')
    op.drop_table('collaboration_runs')
    op.drop_index(op.f('ix_collaboration_drafts_project_id'), table_name='collaboration_drafts')
    op.drop_table('collaboration_drafts')
    op.drop_column('workflow_runs', 'paused')
    op.drop_constraint('ck_workflows_kind', 'workflows', type_='check')
    op.drop_column('workflows', 'kind')
