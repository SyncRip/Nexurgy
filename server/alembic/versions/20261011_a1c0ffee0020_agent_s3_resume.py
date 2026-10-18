"""S3 resume from checkpoint: spec chunk findings, assembly build steps, drawing package continuation

Additive:

- ``engineering_spec_chunks.findings`` (JSONB) and ``call_id`` (→ ai_calls): what the model reported for a chunk and the
  call that returned it, stored as soon as the chunk is read, so "continue extraction" reuses it;
- ``engineering_spec_versions`` origin ``continue`` (a new version continuing a failed extraction);
- ``assembly_build_steps``: per part definition of one W8 build scope (node run), the exact revision it produced or why
  it was not built, so a retry skips the definitions already built;
- ``assembly_drawings.resumed_from_id``: the partial package a package continued (one continuation per package).

Downgrade (development only) drops them again; versions of origin ``continue`` become ``reextract``.

Revision ID: a1c0ffee0020
Revises: a1c0ffee0019
Create Date: 2026-10-11 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0020'
down_revision: Union[str, None] = 'a1c0ffee0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORIGINS_OLD = "origin IN ('create', 'answer', 'reextract')"
ORIGINS_NEW = "origin IN ('create', 'answer', 'reextract', 'continue')"
STEP_STATUS = "status IN ('built', 'failed', 'blocked', 'needs_clarification', 'not_started')"
POLICY = "execution_policy IN ('standard', 'turbo')"


def upgrade() -> None:
    op.add_column('engineering_spec_chunks', sa.Column('findings', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('engineering_spec_chunks', sa.Column('call_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('engineering_spec_chunks_call_id_fkey', 'engineering_spec_chunks', 'ai_calls', ['call_id'], ['id'],
                          ondelete='SET NULL')
    op.drop_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', type_='check')
    op.create_check_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', ORIGINS_NEW)

    op.create_table(
        'assembly_build_steps',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('spec_version_id', sa.Uuid(), nullable=False),
        sa.Column('scope', sa.String(length=120), nullable=False),
        sa.Column('definition_key', sa.String(length=60), nullable=False),
        sa.Column('execution_policy', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=True),
        sa.Column('part_id', sa.Uuid(), nullable=True),
        sa.Column('revision_id', sa.Uuid(), nullable=True),
        sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('outcome', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('simulated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='1', nullable=False),
        sa.Column('node_run_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(STEP_STATUS, name='ck_assembly_build_steps_status'),
        sa.CheckConstraint(POLICY, name='ck_assembly_build_steps_policy'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['spec_version_id'], ['engineering_spec_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['part_id'], ['parts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['revision_id'], ['model_revisions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['node_run_id'], ['node_runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope', 'spec_version_id', 'definition_key', name='uq_assembly_build_steps_key'),
    )
    op.create_index(op.f('ix_assembly_build_steps_project_id'), 'assembly_build_steps', ['project_id'], unique=False)
    op.create_index(op.f('ix_assembly_build_steps_spec_version_id'), 'assembly_build_steps', ['spec_version_id'], unique=False)

    op.add_column('assembly_drawings', sa.Column('resumed_from_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('assembly_drawings_resumed_from_id_fkey', 'assembly_drawings', 'assembly_drawings', ['resumed_from_id'], ['id'],
                          ondelete='SET NULL')
    op.create_unique_constraint('uq_assembly_drawings_resumed_from', 'assembly_drawings', ['resumed_from_id'])


def downgrade() -> None:
    op.drop_constraint('uq_assembly_drawings_resumed_from', 'assembly_drawings', type_='unique')
    op.drop_constraint('assembly_drawings_resumed_from_id_fkey', 'assembly_drawings', type_='foreignkey')
    op.drop_column('assembly_drawings', 'resumed_from_id')

    op.drop_index(op.f('ix_assembly_build_steps_spec_version_id'), table_name='assembly_build_steps')
    op.drop_index(op.f('ix_assembly_build_steps_project_id'), table_name='assembly_build_steps')
    op.drop_table('assembly_build_steps')

    op.drop_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', type_='check')
    op.execute("UPDATE engineering_spec_versions SET origin = 'reextract' WHERE origin = 'continue'")
    op.create_check_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', ORIGINS_OLD)
    op.drop_constraint('engineering_spec_chunks_call_id_fkey', 'engineering_spec_chunks', type_='foreignkey')
    op.drop_column('engineering_spec_chunks', 'call_id')
    op.drop_column('engineering_spec_chunks', 'findings')
