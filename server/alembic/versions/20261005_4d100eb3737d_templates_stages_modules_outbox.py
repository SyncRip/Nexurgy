"""templates stages modules outbox (P3 / P4)

Revision ID: 4d100eb3737d
Revises: 55728e3fd047
Create Date: 2026-10-05 11:00:54.355182
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '4d100eb3737d'
down_revision: Union[str, None] = '55728e3fd047'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())

OLD_ASSET_TYPES = "type IN ('part_model', 'drawing', 'process_plan', 'quote', 'document', 'intake')"
NEW_ASSET_TYPES = "type IN ('part_model', 'drawing', 'process_plan', 'quote', 'document', 'intake', 'process_module')"
OLD_KINDS = "kind IN ('engineering', 'task_acceptance')"
NEW_KINDS = "kind IN ('engineering', 'task_acceptance', 'milestone_review')"
OLD_AV_TARGET = "num_nonnulls(model_revision_id, drawing_id, process_plan_id, quote_id, file_version_id, intake_id) = 1"
NEW_AV_TARGET = "num_nonnulls(model_revision_id, drawing_id, process_plan_id, quote_id, file_version_id, intake_id, process_module_id) = 1"
OLD_REQ_TARGET = "num_nonnulls(asset_version_id, submission_id) = 1"
NEW_REQ_TARGET = "num_nonnulls(asset_version_id, submission_id, milestone_review_id) = 1"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_='check')
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    # --- template library and published template versions (they reference each other) ---
    op.create_table('public_templates',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('source_project_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['source_project_id'], ['projects.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workflow_template_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('graph', JSONB, nullable=False),
    sa.Column('input_schema', JSONB, nullable=False),
    sa.Column('output_schema', JSONB, nullable=False),
    sa.Column('policy', JSONB, nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('draft_version', sa.Integer(), nullable=False),
    sa.Column('source_public_version_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('published_by', sa.Uuid(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deprecated_by', sa.Uuid(), nullable=True),
    sa.Column('deprecated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deprecation_reason', sa.Text(), nullable=False),
    sa.CheckConstraint("status IN ('published', 'deprecated')", name='ck_template_versions_status'),
    sa.ForeignKeyConstraint(['deprecated_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['published_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workflow_id', 'version_no', name='uq_template_versions_workflow_no')
    )
    op.create_index(op.f('ix_workflow_template_versions_project_id'), 'workflow_template_versions', ['project_id'], unique=False)
    op.create_index(op.f('ix_workflow_template_versions_workflow_id'), 'workflow_template_versions', ['workflow_id'], unique=False)
    op.create_table('public_template_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('public_template_id', sa.Uuid(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('graph', JSONB, nullable=False),
    sa.Column('input_schema', JSONB, nullable=False),
    sa.Column('output_schema', JSONB, nullable=False),
    sa.Column('policy', JSONB, nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('source_template_version_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('published_by', sa.Uuid(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deprecated_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('published', 'deprecated')", name='ck_public_template_versions_status'),
    sa.ForeignKeyConstraint(['public_template_id'], ['public_templates.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['published_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['source_template_version_id'], ['workflow_template_versions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_template_id', 'version_no', name='uq_public_template_versions_no')
    )
    op.create_index(op.f('ix_public_template_versions_public_template_id'), 'public_template_versions', ['public_template_id'], unique=False)
    op.create_foreign_key('fk_template_versions_source_public', 'workflow_template_versions', 'public_template_versions',
                          ['source_public_version_id'], ['id'], ondelete='SET NULL')

    op.add_column('workflows', sa.Column('policy', JSONB, server_default='{}', nullable=False))
    op.add_column('workflows', sa.Column('source_public_template_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('workflows_source_public_template_id_fkey', 'workflows', 'public_templates', ['source_public_template_id'], ['id'], ondelete='SET NULL')
    op.add_column('workflow_runs', sa.Column('template_version_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_workflow_runs_template_version_id'), 'workflow_runs', ['template_version_id'], unique=False)
    op.create_foreign_key('workflow_runs_template_version_id_fkey', 'workflow_runs', 'workflow_template_versions', ['template_version_id'], ['id'], ondelete='SET NULL')
    op.add_column('project_members', sa.Column('capabilities', postgresql.ARRAY(sa.String(length=40)), server_default='{}', nullable=False))

    # --- outbox and notifications ---
    op.create_table('outbox_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.String(length=60), nullable=False),
    sa.Column('payload', JSONB, nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedupe_key')
    )
    op.create_index('ix_outbox_events_pending', 'outbox_events', ['processed_at', 'created_at'], unique=False)
    op.create_index(op.f('ix_outbox_events_project_id'), 'outbox_events', ['project_id'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.String(length=60), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('link', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['event_id'], ['outbox_events.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id', 'user_id', name='uq_notifications_event_user')
    )
    op.create_index('ix_notifications_inbox', 'notifications', ['user_id', 'read_at', 'created_at'], unique=False)

    # --- stages: milestone reviews ---
    op.create_table('milestone_reviews',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('milestone_id', sa.Uuid(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('snapshot', JSONB, nullable=False),
    sa.Column('snapshot_hash', sa.String(length=64), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('reviewer_id', sa.Uuid(), nullable=True),
    sa.Column('submitted_by', sa.Uuid(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'withdrawn', 'superseded')", name='ck_milestone_reviews_status'),
    sa.ForeignKeyConstraint(['milestone_id'], ['project_milestones.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['submitted_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('milestone_id', 'seq', name='uq_milestone_reviews_seq')
    )
    op.create_index(op.f('ix_milestone_reviews_milestone_id'), 'milestone_reviews', ['milestone_id'], unique=False)
    op.create_index(op.f('ix_milestone_reviews_project_id'), 'milestone_reviews', ['project_id'], unique=False)
    op.create_index('uq_milestone_reviews_one_pending', 'milestone_reviews', ['milestone_id'], unique=True, postgresql_where=sa.text("status = 'pending'"))
    op.create_table('milestone_review_items',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('review_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=True),
    sa.Column('submission_id', sa.Uuid(), nullable=True),
    sa.Column('asset_version_id', sa.Uuid(), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.CheckConstraint("kind IN ('task_submission', 'extra_asset')", name='ck_milestone_review_items_kind'),
    sa.CheckConstraint('num_nonnulls(submission_id, asset_version_id) >= 1', name='ck_milestone_review_items_target'),
    sa.ForeignKeyConstraint(['asset_version_id'], ['asset_versions.id'], ),
    sa.ForeignKeyConstraint(['review_id'], ['milestone_reviews.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['submission_id'], ['task_submissions.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_milestone_review_items_review_id'), 'milestone_review_items', ['review_id'], unique=False)
    op.create_index(op.f('ix_milestone_review_items_submission_id'), 'milestone_review_items', ['submission_id'], unique=False)

    op.add_column('project_milestones', sa.Column('requirements', JSONB, server_default='{}', nullable=False))
    op.add_column('project_milestones', sa.Column('status', sa.String(length=20), server_default='open', nullable=False))
    op.add_column('project_milestones', sa.Column('accepted_review_id', sa.Uuid(), nullable=True))
    op.add_column('project_milestones', sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('project_milestones', sa.Column('version', sa.Integer(), server_default='1', nullable=False))
    op.create_check_constraint('ck_project_milestones_status', 'project_milestones', "status IN ('open', 'in_review', 'accepted')")
    op.create_foreign_key('fk_milestones_accepted_review', 'project_milestones', 'milestone_reviews', ['accepted_review_id'], ['id'], ondelete='SET NULL')
    op.add_column('projects', sa.Column('current_milestone_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_projects_current_milestone', 'projects', 'project_milestones', ['current_milestone_id'], ['id'], ondelete='SET NULL')
    op.add_column('tasks', sa.Column('required', sa.Boolean(), server_default='true', nullable=False))

    op.add_column('approval_requests', sa.Column('milestone_review_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('approval_requests_milestone_review_id_fkey', 'approval_requests', 'milestone_reviews', ['milestone_review_id'], ['id'], ondelete='CASCADE')
    _replace_check('approval_requests', 'ck_approval_requests_one_target', NEW_REQ_TARGET)
    _replace_check('approval_requests', 'ck_approval_requests_kind', NEW_KINDS)

    op.add_column('impact_notices', sa.Column('milestone_id', sa.Uuid(), nullable=True))
    op.add_column('impact_notices', sa.Column('milestone_review_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('impact_notices_milestone_review_id_fkey', 'impact_notices', 'milestone_reviews', ['milestone_review_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('impact_notices_milestone_id_fkey', 'impact_notices', 'project_milestones', ['milestone_id'], ['id'], ondelete='SET NULL')

    # --- process module assets ---
    op.create_table('process_modules',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('applicability', JSONB, nullable=False),
    sa.Column('parameters', JSONB, nullable=False),
    sa.Column('steps', JSONB, nullable=False),
    sa.Column('validation', JSONB, nullable=False),
    sa.Column('source_plan_id', sa.Uuid(), nullable=True),
    sa.Column('source_steps', JSONB, nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_plan_id'], ['process_plans.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_process_modules_project_id'), 'process_modules', ['project_id'], unique=False)
    op.add_column('asset_versions', sa.Column('process_module_id', sa.Uuid(), nullable=True))
    op.create_unique_constraint('asset_versions_process_module_id_key', 'asset_versions', ['process_module_id'])
    op.create_foreign_key('asset_versions_process_module_id_fkey', 'asset_versions', 'process_modules', ['process_module_id'], ['id'], ondelete='CASCADE')
    _replace_check('asset_versions', 'ck_asset_versions_one_target', NEW_AV_TARGET)
    _replace_check('assets', 'ck_assets_type', NEW_ASSET_TYPES)


def downgrade() -> None:
    # Business history is not meant to be dropped by a downgrade in production (design §12.2);
    # this exists for development databases only.
    op.execute("DELETE FROM asset_versions WHERE process_module_id IS NOT NULL")
    op.execute("DELETE FROM assets WHERE type = 'process_module'")
    op.execute("DELETE FROM approval_requests WHERE milestone_review_id IS NOT NULL OR kind = 'milestone_review'")
    _replace_check('assets', 'ck_assets_type', OLD_ASSET_TYPES)
    _replace_check('asset_versions', 'ck_asset_versions_one_target', OLD_AV_TARGET)
    op.drop_constraint('asset_versions_process_module_id_fkey', 'asset_versions', type_='foreignkey')
    op.drop_constraint('asset_versions_process_module_id_key', 'asset_versions', type_='unique')
    op.drop_column('asset_versions', 'process_module_id')
    op.drop_index(op.f('ix_process_modules_project_id'), table_name='process_modules')
    op.drop_table('process_modules')

    op.drop_constraint('impact_notices_milestone_id_fkey', 'impact_notices', type_='foreignkey')
    op.drop_constraint('impact_notices_milestone_review_id_fkey', 'impact_notices', type_='foreignkey')
    op.drop_column('impact_notices', 'milestone_review_id')
    op.drop_column('impact_notices', 'milestone_id')

    _replace_check('approval_requests', 'ck_approval_requests_kind', OLD_KINDS)
    _replace_check('approval_requests', 'ck_approval_requests_one_target', OLD_REQ_TARGET)
    op.drop_constraint('approval_requests_milestone_review_id_fkey', 'approval_requests', type_='foreignkey')
    op.drop_column('approval_requests', 'milestone_review_id')

    op.drop_column('tasks', 'required')
    op.drop_constraint('fk_projects_current_milestone', 'projects', type_='foreignkey')
    op.drop_column('projects', 'current_milestone_id')
    op.drop_constraint('fk_milestones_accepted_review', 'project_milestones', type_='foreignkey')
    op.drop_constraint('ck_project_milestones_status', 'project_milestones', type_='check')
    op.drop_column('project_milestones', 'version')
    op.drop_column('project_milestones', 'accepted_at')
    op.drop_column('project_milestones', 'accepted_review_id')
    op.drop_column('project_milestones', 'status')
    op.drop_column('project_milestones', 'requirements')
    op.drop_index(op.f('ix_milestone_review_items_submission_id'), table_name='milestone_review_items')
    op.drop_index(op.f('ix_milestone_review_items_review_id'), table_name='milestone_review_items')
    op.drop_table('milestone_review_items')
    op.drop_index('uq_milestone_reviews_one_pending', table_name='milestone_reviews', postgresql_where=sa.text("status = 'pending'"))
    op.drop_index(op.f('ix_milestone_reviews_project_id'), table_name='milestone_reviews')
    op.drop_index(op.f('ix_milestone_reviews_milestone_id'), table_name='milestone_reviews')
    op.drop_table('milestone_reviews')

    op.drop_index('ix_notifications_inbox', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_outbox_events_project_id'), table_name='outbox_events')
    op.drop_index('ix_outbox_events_pending', table_name='outbox_events')
    op.drop_table('outbox_events')

    op.drop_column('project_members', 'capabilities')
    op.drop_constraint('workflow_runs_template_version_id_fkey', 'workflow_runs', type_='foreignkey')
    op.drop_index(op.f('ix_workflow_runs_template_version_id'), table_name='workflow_runs')
    op.drop_column('workflow_runs', 'template_version_id')
    op.drop_constraint('workflows_source_public_template_id_fkey', 'workflows', type_='foreignkey')
    op.drop_column('workflows', 'source_public_template_id')
    op.drop_column('workflows', 'policy')
    op.drop_constraint('fk_template_versions_source_public', 'workflow_template_versions', type_='foreignkey')
    op.drop_index(op.f('ix_public_template_versions_public_template_id'), table_name='public_template_versions')
    op.drop_table('public_template_versions')
    op.drop_index(op.f('ix_workflow_template_versions_workflow_id'), table_name='workflow_template_versions')
    op.drop_index(op.f('ix_workflow_template_versions_project_id'), table_name='workflow_template_versions')
    op.drop_table('workflow_template_versions')
    op.drop_table('public_templates')
