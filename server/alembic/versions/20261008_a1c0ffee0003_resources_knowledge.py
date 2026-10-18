"""workflow resources and knowledge archive (D04 / D05)

Design: .trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §2.3. Also links
collaboration_runs to its knowledge collection.

Revision ID: a1c0ffee0003
Revises: a1c0ffee0002
Create Date: 2026-10-08 09:42:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0003'
down_revision: Union[str, None] = 'a1c0ffee0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # Collections and their versions reference each other: the current-version link is added afterwards.
    op.create_table('knowledge_collections',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('scope', JSONB, server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('current_version_id', sa.Uuid(), nullable=True),
    sa.Column('index_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('index_error', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_by_kind', sa.String(length=10), server_default='human', nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_by_agent_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deprecated_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("created_by_kind IN ('human', 'agent')", name='ck_knowledge_collections_actor_kind'),
    sa.CheckConstraint("index_status IN ('pending', 'indexing', 'ready', 'partial', 'failed')", name='ck_knowledge_collections_index'),
    sa.CheckConstraint("status IN ('active', 'deprecated')", name='ck_knowledge_collections_status'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id')
    )
    op.create_index(op.f('ix_knowledge_collections_project_id'), 'knowledge_collections', ['project_id'], unique=False)
    op.create_table('knowledge_collection_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('collection_id', sa.Uuid(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('manifest', JSONB, nullable=False),
    sa.Column('manifest_hash', sa.String(length=64), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['collection_id'], ['knowledge_collections.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('collection_id', 'manifest_hash', name='uq_knowledge_versions_hash'),
    sa.UniqueConstraint('collection_id', 'version_no', name='uq_knowledge_versions_no')
    )
    op.create_index(op.f('ix_knowledge_collection_versions_collection_id'), 'knowledge_collection_versions', ['collection_id'], unique=False)
    op.create_foreign_key('fk_knowledge_collections_current_version', 'knowledge_collections', 'knowledge_collection_versions',
                          ['current_version_id'], ['id'], ondelete='SET NULL')

    op.create_table('workflow_resources',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('node_task_id', sa.Uuid(), nullable=True),
    sa.Column('node_run_id', sa.Uuid(), nullable=True),
    sa.Column('agent_execution_id', sa.Uuid(), nullable=True),
    sa.Column('submission_id', sa.Uuid(), nullable=True),
    sa.Column('attempt', sa.Integer(), nullable=True),
    sa.Column('slot_key', sa.String(length=80), nullable=True),
    sa.Column('asset_version_id', sa.Uuid(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('metadata', JSONB, server_default='{}', nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('input', 'candidate', 'formal', 'failed', 'superseded', 'evidence')", name='ck_workflow_resources_kind'),
    sa.ForeignKeyConstraint(['agent_execution_id'], ['agent_executions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['asset_version_id'], ['asset_versions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['node_run_id'], ['node_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_task_id'], ['collaboration_node_tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['submission_id'], ['task_submissions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedupe_key')
    )
    op.create_index(op.f('ix_workflow_resources_asset_version_id'), 'workflow_resources', ['asset_version_id'], unique=False)
    op.create_index('ix_workflow_resources_project_created', 'workflow_resources', ['project_id', 'created_at', 'id'], unique=False)
    op.create_index('ix_workflow_resources_run_kind', 'workflow_resources', ['run_id', 'kind'], unique=False)

    op.create_table('knowledge_items',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('collection_version_id', sa.Uuid(), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=True),
    sa.Column('asset_version_id', sa.Uuid(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('node_task_id', sa.Uuid(), nullable=True),
    sa.Column('task_id', sa.Uuid(), nullable=True),
    sa.Column('slot_key', sa.String(length=80), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('reusable', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('index_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('extracted_text', sa.Text(), nullable=True),
    sa.Column('index_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("index_status IN ('pending', 'ready', 'failed', 'not_applicable')", name='ck_knowledge_items_index'),
    sa.CheckConstraint("kind IN ('formal', 'history', 'candidate', 'failed', 'evidence')", name='ck_knowledge_items_kind'),
    sa.ForeignKeyConstraint(['asset_version_id'], ['asset_versions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['collection_version_id'], ['knowledge_collection_versions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['node_task_id'], ['collaboration_node_tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resource_id'], ['workflow_resources.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('collection_version_id', 'resource_id', name='uq_knowledge_items_version_resource')
    )
    op.create_index(op.f('ix_knowledge_items_asset_version_id'), 'knowledge_items', ['asset_version_id'], unique=False)
    op.create_index(op.f('ix_knowledge_items_collection_version_id'), 'knowledge_items', ['collection_version_id'], unique=False)

    op.create_table('knowledge_usages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('collection_version_id', sa.Uuid(), nullable=False),
    sa.Column('asset_version_id', sa.Uuid(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('target_project_id', sa.Uuid(), nullable=False),
    sa.Column('target_run_id', sa.Uuid(), nullable=True),
    sa.Column('target_node_task_id', sa.Uuid(), nullable=True),
    sa.Column('target_task_id', sa.Uuid(), nullable=True),
    sa.Column('slot_key', sa.String(length=80), nullable=True),
    sa.Column('usage', sa.String(length=20), nullable=False),
    sa.Column('baseline_snapshot_id', sa.Uuid(), nullable=True),
    sa.Column('used_by_kind', sa.String(length=10), server_default='human', nullable=False),
    sa.Column('used_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('used_by_agent_id', sa.Uuid(), nullable=True),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.Column('confirmation', sa.String(length=20), server_default='none', nullable=False),
    sa.Column('confirmed_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("confirmation IN ('none', 'pending', 'confirmed', 'rejected')", name='ck_knowledge_usages_confirmation'),
    sa.CheckConstraint("usage IN ('reference', 'baseline')", name='ck_knowledge_usages_usage'),
    sa.CheckConstraint("used_by_kind IN ('human', 'agent')", name='ck_knowledge_usages_actor_kind'),
    sa.ForeignKeyConstraint(['asset_version_id'], ['asset_versions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['baseline_snapshot_id'], ['task_baseline_snapshots.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['collection_version_id'], ['knowledge_collection_versions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['confirmed_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['item_id'], ['knowledge_items.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_node_task_id'], ['collaboration_node_tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['target_project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_run_id'], ['workflow_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['target_task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['used_by_agent_id'], ['agent_profiles.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['used_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_knowledge_usages_item_id'), 'knowledge_usages', ['item_id'], unique=False)
    op.create_index('ix_knowledge_usages_target', 'knowledge_usages', ['target_project_id', 'created_at'], unique=False)

    op.add_column('collaboration_runs', sa.Column('knowledge_collection_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_collab_runs_knowledge_collection', 'collaboration_runs', 'knowledge_collections',
                          ['knowledge_collection_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_collab_runs_knowledge_collection', 'collaboration_runs', type_='foreignkey')
    op.drop_column('collaboration_runs', 'knowledge_collection_id')
    op.drop_index('ix_knowledge_usages_target', table_name='knowledge_usages')
    op.drop_index(op.f('ix_knowledge_usages_item_id'), table_name='knowledge_usages')
    op.drop_table('knowledge_usages')
    op.drop_index(op.f('ix_knowledge_items_collection_version_id'), table_name='knowledge_items')
    op.drop_index(op.f('ix_knowledge_items_asset_version_id'), table_name='knowledge_items')
    op.drop_table('knowledge_items')
    op.drop_index('ix_workflow_resources_run_kind', table_name='workflow_resources')
    op.drop_index('ix_workflow_resources_project_created', table_name='workflow_resources')
    op.drop_index(op.f('ix_workflow_resources_asset_version_id'), table_name='workflow_resources')
    op.drop_table('workflow_resources')
    op.drop_constraint('fk_knowledge_collections_current_version', 'knowledge_collections', type_='foreignkey')
    op.drop_index(op.f('ix_knowledge_collection_versions_collection_id'), table_name='knowledge_collection_versions')
    op.drop_table('knowledge_collection_versions')
    op.drop_index(op.f('ix_knowledge_collections_project_id'), table_name='knowledge_collections')
    op.drop_table('knowledge_collections')
