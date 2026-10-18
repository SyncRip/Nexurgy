"""W2 engineering spec: sources, read state, spec versions, facts, questions, assumptions

Three new tables (additive): engineering_specs (project-scoped lineage), engineering_spec_versions
(immutable versions: policy, status, spec JSON, source manifest, checks, validation, model record,
structured error) and engineering_spec_chunks (which chunk of which source the server really sent).

Revision ID: a1c0ffee0014
Revises: a1c0ffee0013
Create Date: 2026-10-10 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0014'
down_revision: Union[str, None] = 'a1c0ffee0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _jsonb(name: str, default: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(f"'{default}'::jsonb"), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        'engineering_specs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'number', name='uq_engineering_specs_project_number'),
    )
    op.create_index('ix_engineering_specs_project_id', 'engineering_specs', ['project_id'])

    op.create_table(
        'engineering_spec_versions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('spec_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('parent_version_id', sa.Uuid(), nullable=True),
        sa.Column('origin', sa.String(length=20), nullable=False),
        sa.Column('execution_policy', sa.String(length=20), server_default='standard', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='extracting', nullable=False),
        sa.Column('model_config_id', sa.Uuid(), nullable=True),
        _jsonb('content', '{}'),
        _jsonb('sources', '[]'),
        _jsonb('checks', '[]'),
        _jsonb('validation', '{}'),
        _jsonb('planning', '{}'),
        sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _jsonb('answers', '[]'),
        sa.Column('simulated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('extracting', 'ready', 'needs_answers', 'failed')", name='ck_engineering_spec_versions_status'),
        sa.CheckConstraint("origin IN ('create', 'answer', 'reextract')", name='ck_engineering_spec_versions_origin'),
        sa.CheckConstraint("execution_policy IN ('standard', 'turbo')", name='ck_engineering_spec_versions_policy'),
        sa.ForeignKeyConstraint(['spec_id'], ['engineering_specs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_version_id'], ['engineering_spec_versions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('spec_id', 'version_no', name='uq_engineering_spec_versions_no'),
    )
    op.create_index('ix_engineering_spec_versions_spec_id', 'engineering_spec_versions', ['spec_id'])
    op.create_index('ix_engineering_spec_versions_project_id', 'engineering_spec_versions', ['project_id'])

    op.create_table(
        'engineering_spec_chunks',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('version_id', sa.Uuid(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.String(length=20), nullable=False),
        sa.Column('source_chunk', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('locator', sa.String(length=300), nullable=False),
        sa.Column('start', sa.Integer(), nullable=True),
        sa.Column('end', sa.Integer(), nullable=True),
        sa.Column('chars', sa.Integer(), server_default='0', nullable=False),
        sa.Column('text_sha256', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('reason', sa.String(length=60), nullable=True),
        sa.Column('ai_call_id', sa.Uuid(), nullable=True),
        sa.Column('inherited_from_version_id', sa.Uuid(), nullable=True),
        _jsonb('meta', '{}'),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'sent', 'failed', 'unread')", name='ck_engineering_spec_chunks_status'),
        sa.CheckConstraint("kind IN ('text', 'drawing', 'attachment')", name='ck_engineering_spec_chunks_kind'),
        sa.ForeignKeyConstraint(['version_id'], ['engineering_spec_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['ai_call_id'], ['ai_calls.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['inherited_from_version_id'], ['engineering_spec_versions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version_id', 'seq', name='uq_engineering_spec_chunks_seq'),
    )
    op.create_index('ix_engineering_spec_chunks_version_id', 'engineering_spec_chunks', ['version_id'])


def downgrade() -> None:
    op.drop_index('ix_engineering_spec_chunks_version_id', table_name='engineering_spec_chunks')
    op.drop_table('engineering_spec_chunks')
    op.drop_index('ix_engineering_spec_versions_project_id', table_name='engineering_spec_versions')
    op.drop_index('ix_engineering_spec_versions_spec_id', table_name='engineering_spec_versions')
    op.drop_table('engineering_spec_versions')
    op.drop_index('ix_engineering_specs_project_id', table_name='engineering_specs')
    op.drop_table('engineering_specs')
