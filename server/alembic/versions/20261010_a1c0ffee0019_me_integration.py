"""W8 integration: delivery manifests and orchestration

Additive: ``delivery_manifests`` (required vs actual files of one delivery, hashes, source versions, units,
representation levels, assumptions, verification, missing items, complete | partial) and
``asset_versions.delivery_manifest_id`` so manifests are catalogued as ``delivery_manifest`` assets.

``ck_asset_versions_one_target`` is rebuilt from the constraint as it stands (so the columns earlier packages added,
e.g. W4's ``assembly_revision_id``, stay in it) plus ``delivery_manifest_id``. Downgrade drops the manifests (and their
asset versions) again and removes only that column from the constraint.

Revision ID: a1c0ffee0019
Revises: a1c0ffee0018
Create Date: 2026-10-10 10:00:00
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0019'
down_revision: Union[str, None] = 'a1c0ffee0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMN = "delivery_manifest_id"
POLICY = "execution_policy IN ('standard', 'turbo')"
CHECK = "check_status IN ('not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown')"
COMPLETENESS = "completeness IN ('complete', 'partial')"


def _target_columns() -> list[str]:
    """Columns of ck_asset_versions_one_target as the database has it now."""
    row = op.get_bind().execute(sa.text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_asset_versions_one_target'")).scalar()
    m = re.search(r"num_nonnulls\((.*?)\)", row or "")
    if m is None:
        raise RuntimeError(f"unexpected ck_asset_versions_one_target definition: {row!r}")
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _set_target(columns: list[str]) -> None:
    op.drop_constraint('ck_asset_versions_one_target', 'asset_versions', type_='check')
    op.create_check_constraint('ck_asset_versions_one_target', 'asset_versions', f"num_nonnulls({', '.join(columns)}) = 1")


def upgrade() -> None:
    op.create_table(
        'delivery_manifests',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('assembly_revision_id', sa.Uuid(), nullable=True),
        sa.Column('spec_version_id', sa.Uuid(), nullable=True),
        sa.Column('drawing_package_id', sa.Uuid(), nullable=True),
        sa.Column('execution_policy', sa.String(length=20), server_default='standard', nullable=False),
        sa.Column('completeness', sa.String(length=20), nullable=False),
        sa.Column('check_status', sa.String(length=20), server_default='not_requested', nullable=False),
        sa.Column('simulated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('manifest_sha256', sa.String(length=64), nullable=False),
        sa.Column('zip_sha256', sa.String(length=64), nullable=False),
        sa.Column('node_run_id', sa.Uuid(), nullable=True),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(COMPLETENESS, name='ck_delivery_manifests_completeness'),
        sa.CheckConstraint(POLICY, name='ck_delivery_manifests_policy'),
        sa.CheckConstraint(CHECK, name='ck_delivery_manifests_check'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assembly_revision_id'], ['assembly_revisions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['spec_version_id'], ['engineering_spec_versions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['node_run_id'], ['node_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'number', name='uq_delivery_manifests_project_number'),
    )
    op.create_index(op.f('ix_delivery_manifests_project_id'), 'delivery_manifests', ['project_id'], unique=False)
    op.create_index(op.f('ix_delivery_manifests_assembly_revision_id'), 'delivery_manifests', ['assembly_revision_id'], unique=False)

    op.add_column('asset_versions', sa.Column(COLUMN, sa.Uuid(), nullable=True))
    op.create_unique_constraint('asset_versions_delivery_manifest_id_key', 'asset_versions', [COLUMN])
    op.create_foreign_key('asset_versions_delivery_manifest_id_fkey', 'asset_versions', 'delivery_manifests',
                          [COLUMN], ['id'], ondelete='CASCADE')
    columns = _target_columns()
    if COLUMN not in columns:
        _set_target(columns + [COLUMN])


def downgrade() -> None:
    if 'delivery_manifests' not in sa.inspect(op.get_bind()).get_table_names():
        return  # a database stamped while this file was still the W0 no-op placeholder
    op.execute(f"DELETE FROM asset_versions WHERE {COLUMN} IS NOT NULL")
    op.execute("DELETE FROM assets WHERE type = 'delivery_manifest'")
    _set_target([c for c in _target_columns() if c != COLUMN])
    op.drop_constraint('asset_versions_delivery_manifest_id_fkey', 'asset_versions', type_='foreignkey')
    op.drop_constraint('asset_versions_delivery_manifest_id_key', 'asset_versions', type_='unique')
    op.drop_column('asset_versions', COLUMN)
    op.drop_index(op.f('ix_delivery_manifests_assembly_revision_id'), table_name='delivery_manifests')
    op.drop_index(op.f('ix_delivery_manifests_project_id'), table_name='delivery_manifests')
    op.drop_table('delivery_manifests')
