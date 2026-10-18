"""W5 assembly drawings: assembly drawing packages

Additive: ``assembly_drawings`` (immutable GA + detail drawing packages of one exact assembly revision; a layout
overlay is the next version of the same package) and ``asset_versions.assembly_drawing_id`` so every package
version is catalogued as an ``assembly_drawing`` asset. ``ck_asset_versions_one_target`` is extended from the
constraint as it stands (whatever earlier packages added, e.g. W4's ``assembly_revision_id``), so the chain stays
correct in any merge order. Downgrade removes the packages, their asset versions and the column again.

Revision ID: a1c0ffee0018
Revises: a1c0ffee0017
Create Date: 2026-10-10 10:00:00
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0018'
down_revision: Union[str, None] = 'a1c0ffee0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TARGET = 'ck_asset_versions_one_target'
COLUMN = 'assembly_drawing_id'
POLICY = "execution_policy IN ('standard', 'turbo')"
COMPLETENESS = "completeness IN ('complete', 'partial')"
CHECK = "check_status IN ('not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown')"


def _jsonb(name: str, default: str | None, nullable: bool = False) -> sa.Column:
    if default is None:
        return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=nullable)
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(f"'{default}'::jsonb"), nullable=nullable)


def _target_columns() -> list[str]:
    """Columns of the current one-target check (num_nonnulls(a, b, ...) = 1)."""
    expr = op.get_bind().execute(sa.text(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'asset_versions' AND c.conname = :name"), {"name": TARGET}).scalar()
    m = re.search(r"num_nonnulls\(([^)]*)\)", expr or "")
    if m is None:
        raise RuntimeError(f"{TARGET} has an unexpected definition: {expr!r}")
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _retarget(columns: list[str]) -> None:
    op.drop_constraint(TARGET, 'asset_versions', type_='check')
    op.create_check_constraint(TARGET, 'asset_versions', f"num_nonnulls({', '.join(columns)}) = 1")


def upgrade() -> None:
    op.create_table(
        'assembly_drawings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('assembly_id', sa.Uuid(), nullable=False),
        sa.Column('assembly_revision_id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=True),
        sa.Column('execution_policy', sa.String(length=20), nullable=False),
        sa.Column('completeness', sa.String(length=20), nullable=False),
        sa.Column('check_status', sa.String(length=20), nullable=False),
        _jsonb('config', None),
        _jsonb('overlay', None, nullable=True),
        _jsonb('pages', None),
        _jsonb('documents', '[]'),
        _jsonb('manifest', None),
        sa.Column('manifest_sha256', sa.String(length=64), nullable=False),
        sa.Column('bom_sha256', sa.String(length=64), nullable=True),
        _jsonb('checks', '{}'),
        _jsonb('verification', '{}'),
        _jsonb('assumptions', '[]'),
        _jsonb('errors', '[]'),
        _jsonb('warnings', '[]'),
        _jsonb('generation', '{}'),
        sa.Column('node_run_id', sa.Uuid(), nullable=True),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(POLICY, name='ck_assembly_drawings_policy'),
        sa.CheckConstraint(COMPLETENESS, name='ck_assembly_drawings_completeness'),
        sa.CheckConstraint(CHECK, name='ck_assembly_drawings_check'),
        sa.CheckConstraint('version >= 1 AND number >= 1', name='ck_assembly_drawings_numbers'),
        sa.CheckConstraint('parent_id IS NULL OR parent_id <> id', name='ck_assembly_drawings_parent'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assembly_id'], ['assemblies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assembly_revision_id'], ['assembly_revisions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_id'], ['assembly_drawings.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['node_run_id'], ['node_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('assembly_id', 'number', 'version', name='uq_assembly_drawings_version'),
        sa.UniqueConstraint('parent_id', name='uq_assembly_drawings_parent'),
    )
    op.create_index(op.f('ix_assembly_drawings_project_id'), 'assembly_drawings', ['project_id'], unique=False)
    op.create_index(op.f('ix_assembly_drawings_assembly_id'), 'assembly_drawings', ['assembly_id'], unique=False)
    op.create_index(op.f('ix_assembly_drawings_assembly_revision_id'), 'assembly_drawings', ['assembly_revision_id'], unique=False)

    op.add_column('asset_versions', sa.Column(COLUMN, sa.Uuid(), nullable=True))
    op.create_unique_constraint('asset_versions_assembly_drawing_id_key', 'asset_versions', [COLUMN])
    op.create_foreign_key('asset_versions_assembly_drawing_id_fkey', 'asset_versions', 'assembly_drawings',
                          [COLUMN], ['id'], ondelete='CASCADE')
    columns = _target_columns()
    if COLUMN not in columns:
        _retarget(columns + [COLUMN])


PACKAGE_AVS = f"SELECT id FROM asset_versions WHERE {COLUMN} IS NOT NULL"
#: Tables whose asset_version_id references asset_versions without ON DELETE (rows bound to a package go; same list
#: as a1c0ffee0016).
NO_ACTION_REFS = ("run_input_bindings", "submission_items", "task_baseline_items", "task_deliverable_drafts", "approval_requests")


def downgrade() -> None:
    # Rows that point at an assembly drawing asset version (NO ACTION foreign keys) would block the delete below.
    op.execute(f"UPDATE milestone_review_items SET asset_version_id = NULL "
               f"WHERE asset_version_id IN ({PACKAGE_AVS}) AND submission_id IS NOT NULL")
    op.execute(f"DELETE FROM milestone_review_items WHERE asset_version_id IN ({PACKAGE_AVS})")
    for table in NO_ACTION_REFS:
        op.execute(f"DELETE FROM {table} WHERE asset_version_id IN ({PACKAGE_AVS})")
    op.execute(f"DELETE FROM asset_versions WHERE {COLUMN} IS NOT NULL")
    op.execute("DELETE FROM assets WHERE type = 'assembly_drawing'")
    _retarget([c for c in _target_columns() if c != COLUMN])
    op.drop_constraint('asset_versions_assembly_drawing_id_fkey', 'asset_versions', type_='foreignkey')
    op.drop_constraint('asset_versions_assembly_drawing_id_key', 'asset_versions', type_='unique')
    op.drop_column('asset_versions', COLUMN)
    op.drop_index(op.f('ix_assembly_drawings_assembly_revision_id'), table_name='assembly_drawings')
    op.drop_index(op.f('ix_assembly_drawings_assembly_id'), table_name='assembly_drawings')
    op.drop_index(op.f('ix_assembly_drawings_project_id'), table_name='assembly_drawings')
    op.drop_table('assembly_drawings')
