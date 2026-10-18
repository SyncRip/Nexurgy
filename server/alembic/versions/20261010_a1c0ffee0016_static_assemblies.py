"""W4 static assemblies: assemblies, revisions, instances, mates, BOM, checks

Additive: assemblies, immutable assembly revisions, their direct child instances (exact part model revision or
exact child assembly revision, stable instance ids, local transforms), mates, part BOM attributes and
process / quote roll-ups; asset_versions gets assembly_revision_id so assembly revisions are catalogued as
assembly_model assets. Downgrade drops the assembly data (and its asset versions) again, with the rows that
reference those asset versions without ON DELETE (run inputs, submission / baseline items, deliverable drafts,
approval requests, milestone review items).

Revision ID: a1c0ffee0016
Revises: a1c0ffee0015
Create Date: 2026-10-10 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0016'
down_revision: Union[str, None] = 'a1c0ffee0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_AV_TARGET = ("num_nonnulls(model_revision_id, drawing_id, process_plan_id, quote_id, file_version_id, intake_id, "
                 "process_module_id) = 1")
NEW_AV_TARGET = ("num_nonnulls(model_revision_id, drawing_id, process_plan_id, quote_id, file_version_id, intake_id, "
                 "process_module_id, assembly_revision_id) = 1")

PART_STATUSES = "status IN ('draft', 'in_design', 'in_review', 'released', 'archived')"
POLICY = "execution_policy IN ('standard', 'turbo')"
CHECK = "check_status IN ('not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown')"
ACCEPTANCE = "acceptance_status IN ('not_requested', 'pending', 'accepted', 'rejected')"
MATE_KINDS = "kind IN ('fixed', 'coaxial', 'planar', 'parallel', 'perpendicular', 'offset', 'angle', 'pattern_match')"
MATE_STATUSES = ("status IN ('pending', 'solved', 'redundant', 'conflict', 'broken', 'invalid', 'dropped', 'suppressed', "
                 "'skipped_turbo')")


def _jsonb(name: str, default: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(f"'{default}'::jsonb"), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        'assemblies',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.String(length=80), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
        sa.Column('instance_counter', sa.Integer(), server_default='0', nullable=False),
        sa.Column('mate_counter', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(PART_STATUSES, name='ck_assemblies_status'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'number', name='uq_assemblies_project_number'),
    )
    op.create_index(op.f('ix_assemblies_project_id'), 'assemblies', ['project_id'], unique=False)

    op.create_table(
        'assembly_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('assembly_id', sa.Uuid(), nullable=False),
        sa.Column('rev_no', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('execution_policy', sa.String(length=20), server_default='standard', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='ready', nullable=False),
        sa.Column('check_status', sa.String(length=20), server_default='not_requested', nullable=False),
        sa.Column('acceptance_status', sa.String(length=20), server_default='not_requested', nullable=False),
        sa.Column('content_sha256', sa.String(length=64), nullable=False),
        sa.Column('spec_version_id', sa.Uuid(), nullable=True),
        sa.Column('spec_ref', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _jsonb('stats', '{}'),
        _jsonb('solve', '{}'),
        _jsonb('checks', '{}'),
        _jsonb('verification', '{}'),
        _jsonb('assumptions', '[]'),
        _jsonb('bom', '{}'),
        sa.Column('bom_sha256', sa.String(length=64), nullable=True),
        sa.Column('step_sha', sa.String(length=64), nullable=True),
        sa.Column('parts_zip_sha', sa.String(length=64), nullable=True),
        sa.Column('source_file_version_id', sa.Uuid(), nullable=True),
        _jsonb('notes', '[]'),
        _jsonb('change', '{}'),
        sa.Column('note', sa.Text(), server_default='', nullable=False),
        sa.Column('node_run_id', sa.Uuid(), nullable=True),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("source IN ('manual', 'import', 'ai', 'copy')", name='ck_assembly_revisions_source'),
        sa.CheckConstraint("status IN ('ready', 'failed')", name='ck_assembly_revisions_status'),
        sa.CheckConstraint(POLICY, name='ck_assembly_revisions_policy'),
        sa.CheckConstraint(CHECK, name='ck_assembly_revisions_check'),
        sa.CheckConstraint(ACCEPTANCE, name='ck_assembly_revisions_acceptance'),
        sa.CheckConstraint('parent_id IS NULL OR parent_id <> id', name='ck_assembly_revisions_parent'),
        sa.ForeignKeyConstraint(['assembly_id'], ['assemblies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_id'], ['assembly_revisions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['step_sha'], ['blobs.sha256']),
        sa.ForeignKeyConstraint(['parts_zip_sha'], ['blobs.sha256']),
        sa.ForeignKeyConstraint(['source_file_version_id'], ['file_versions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['node_run_id'], ['node_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('assembly_id', 'rev_no', name='uq_assembly_revisions_rev'),
    )
    op.create_index(op.f('ix_assembly_revisions_assembly_id'), 'assembly_revisions', ['assembly_id'], unique=False)
    op.create_index(op.f('ix_assembly_revisions_spec_version_id'), 'assembly_revisions', ['spec_version_id'], unique=False)

    op.create_table(
        'assembly_components',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('revision_id', sa.Uuid(), nullable=False),
        sa.Column('instance_id', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('definition_kind', sa.String(length=20), nullable=False),
        sa.Column('part_id', sa.Uuid(), nullable=True),
        sa.Column('model_revision_id', sa.Uuid(), nullable=True),
        sa.Column('child_assembly_id', sa.Uuid(), nullable=True),
        sa.Column('child_revision_id', sa.Uuid(), nullable=True),
        sa.Column('transform', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('fixed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('suppressed', sa.Boolean(), server_default='false', nullable=False),
        _jsonb('placement', '{}'),
        _jsonb('display', '{}'),
        sa.Column('source_ref', sa.String(length=200), nullable=True),
        _jsonb('meta', '{}'),
        sa.CheckConstraint("definition_kind IN ('part', 'assembly')", name='ck_assembly_components_kind'),
        sa.CheckConstraint(
            "(definition_kind = 'part' AND model_revision_id IS NOT NULL AND part_id IS NOT NULL AND child_revision_id IS NULL "
            "AND child_assembly_id IS NULL) OR (definition_kind = 'assembly' AND child_revision_id IS NOT NULL "
            "AND child_assembly_id IS NOT NULL AND model_revision_id IS NULL AND part_id IS NULL)",
            name='ck_assembly_components_definition'),
        sa.ForeignKeyConstraint(['revision_id'], ['assembly_revisions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['part_id'], ['parts.id']),
        sa.ForeignKeyConstraint(['model_revision_id'], ['model_revisions.id']),
        sa.ForeignKeyConstraint(['child_assembly_id'], ['assemblies.id']),
        sa.ForeignKeyConstraint(['child_revision_id'], ['assembly_revisions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('revision_id', 'instance_id', name='uq_assembly_components_instance'),
    )
    op.create_index(op.f('ix_assembly_components_revision_id'), 'assembly_components', ['revision_id'], unique=False)
    op.create_index('ix_assembly_components_model_revision', 'assembly_components', ['model_revision_id'], unique=False)
    op.create_index('ix_assembly_components_part', 'assembly_components', ['part_id'], unique=False)
    op.create_index('ix_assembly_components_child_revision', 'assembly_components', ['child_revision_id'], unique=False)
    op.create_index('ix_assembly_components_child_assembly', 'assembly_components', ['child_assembly_id'], unique=False)

    op.create_table(
        'assembly_mates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('revision_id', sa.Uuid(), nullable=False),
        sa.Column('mate_id', sa.String(length=40), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('a', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('b', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _jsonb('params', '{}'),
        sa.Column('priority', sa.Integer(), server_default='100', nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('suppressed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('source', sa.String(length=20), server_default='user', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        _jsonb('result', '{}'),
        _jsonb('meta', '{}'),
        sa.CheckConstraint(MATE_KINDS, name='ck_assembly_mates_kind'),
        sa.CheckConstraint(MATE_STATUSES, name='ck_assembly_mates_status'),
        sa.ForeignKeyConstraint(['revision_id'], ['assembly_revisions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('revision_id', 'mate_id', name='uq_assembly_mates_mate'),
    )
    op.create_index(op.f('ix_assembly_mates_revision_id'), 'assembly_mates', ['revision_id'], unique=False)

    op.create_table(
        'assembly_part_props',
        sa.Column('part_id', sa.Uuid(), nullable=False),
        sa.Column('procurement', sa.String(length=20), server_default='unknown', nullable=False),
        sa.Column('representation', sa.String(length=20), server_default='exact', nullable=False),
        sa.Column('designation', sa.String(length=200), server_default='', nullable=False),
        _jsonb('source', '{}'),
        sa.Column('updated_by', sa.Uuid(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("procurement IN ('make', 'buy', 'standard', 'unknown')", name='ck_assembly_part_props_procurement'),
        sa.CheckConstraint("representation IN ('exact', 'nominal', 'simplified')", name='ck_assembly_part_props_representation'),
        sa.ForeignKeyConstraint(['part_id'], ['parts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('part_id'),
    )

    op.create_table(
        'assembly_rollups',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('revision_id', sa.Uuid(), nullable=False),
        sa.Column('execution_policy', sa.String(length=20), server_default='standard', nullable=False),
        sa.Column('bom_sha256', sa.String(length=64), nullable=True),
        sa.Column('inputs_hash', sa.String(length=64), nullable=False),
        _jsonb('params', '{}'),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(POLICY, name='ck_assembly_rollups_policy'),
        sa.ForeignKeyConstraint(['revision_id'], ['assembly_revisions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_assembly_rollups_revision_id'), 'assembly_rollups', ['revision_id'], unique=False)

    op.add_column('asset_versions', sa.Column('assembly_revision_id', sa.Uuid(), nullable=True))
    op.create_unique_constraint('asset_versions_assembly_revision_id_key', 'asset_versions', ['assembly_revision_id'])
    op.create_foreign_key('asset_versions_assembly_revision_id_fkey', 'asset_versions', 'assembly_revisions',
                          ['assembly_revision_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint('ck_asset_versions_one_target', 'asset_versions', type_='check')
    op.create_check_constraint('ck_asset_versions_one_target', 'asset_versions', NEW_AV_TARGET)


ASSEMBLY_AVS = "SELECT id FROM asset_versions WHERE assembly_revision_id IS NOT NULL"
#: Tables whose asset_version_id references asset_versions without ON DELETE (rows bound to an assembly version go).
NO_ACTION_REFS = ("run_input_bindings", "submission_items", "task_baseline_items", "task_deliverable_drafts", "approval_requests")


def downgrade() -> None:
    # Rows that point at an assembly asset version (NO ACTION foreign keys) would block the delete below.
    op.execute(f"UPDATE milestone_review_items SET asset_version_id = NULL "
               f"WHERE asset_version_id IN ({ASSEMBLY_AVS}) AND submission_id IS NOT NULL")
    op.execute(f"DELETE FROM milestone_review_items WHERE asset_version_id IN ({ASSEMBLY_AVS})")
    for table in NO_ACTION_REFS:
        op.execute(f"DELETE FROM {table} WHERE asset_version_id IN ({ASSEMBLY_AVS})")
    op.execute("DELETE FROM asset_versions WHERE assembly_revision_id IS NOT NULL")
    op.execute("DELETE FROM assets WHERE type = 'assembly_model'")
    op.drop_constraint('ck_asset_versions_one_target', 'asset_versions', type_='check')
    op.create_check_constraint('ck_asset_versions_one_target', 'asset_versions', OLD_AV_TARGET)
    op.drop_constraint('asset_versions_assembly_revision_id_fkey', 'asset_versions', type_='foreignkey')
    op.drop_constraint('asset_versions_assembly_revision_id_key', 'asset_versions', type_='unique')
    op.drop_column('asset_versions', 'assembly_revision_id')
    op.drop_index(op.f('ix_assembly_rollups_revision_id'), table_name='assembly_rollups')
    op.drop_table('assembly_rollups')
    op.drop_table('assembly_part_props')
    op.drop_index(op.f('ix_assembly_mates_revision_id'), table_name='assembly_mates')
    op.drop_table('assembly_mates')
    for ix in ('ix_assembly_components_child_assembly', 'ix_assembly_components_child_revision', 'ix_assembly_components_part',
               'ix_assembly_components_model_revision'):
        op.drop_index(ix, table_name='assembly_components')
    op.drop_index(op.f('ix_assembly_components_revision_id'), table_name='assembly_components')
    op.drop_table('assembly_components')
    op.drop_index(op.f('ix_assembly_revisions_spec_version_id'), table_name='assembly_revisions')
    op.drop_index(op.f('ix_assembly_revisions_assembly_id'), table_name='assembly_revisions')
    op.drop_table('assembly_revisions')
    op.drop_index(op.f('ix_assemblies_project_id'), table_name='assemblies')
    op.drop_table('assemblies')
