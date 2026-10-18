"""S5 policy consistency: project default policy, run policy source, assembly / spec assumption decisions

Owned by the S5 package (docs/agent/stability-fixes.zh-CN.md §S5, execution-layer design §3.4):

- projects.default_execution_policy: the project's "default Turbo" switch (standard = off, the default);
- workflow_runs.policy_source: where a run's frozen policy came from (explicit / inherited / project_default /
  default; NULL for runs started before this revision);
- artifact_checks.assembly_revision_id / spec_version_id: a person's assumption decisions on an assembly revision
  or an engineering spec version (append-only, like the model revision and drawing records).

Additive only; existing rows keep their meaning (standard, no source recorded).

Revision ID: a1c0ffee0021
Revises: a1c0ffee0020
Create Date: 2026-10-11 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1c0ffee0021'
down_revision: Union[str, None] = 'a1c0ffee0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_SUBJECTS = "subject_type IN ('model_revision', 'drawing')"
NEW_SUBJECTS = "subject_type IN ('model_revision', 'drawing', 'assembly_revision', 'engineering_spec_version')"
OLD_TARGET = ("(subject_type = 'model_revision' AND model_revision_id IS NOT NULL AND drawing_id IS NULL) OR "
              "(subject_type = 'drawing' AND drawing_id IS NOT NULL AND model_revision_id IS NULL)")
NEW_TARGET = (
    "(subject_type = 'model_revision' AND model_revision_id IS NOT NULL AND drawing_id IS NULL "
    "AND assembly_revision_id IS NULL AND spec_version_id IS NULL) OR "
    "(subject_type = 'drawing' AND drawing_id IS NOT NULL AND model_revision_id IS NULL "
    "AND assembly_revision_id IS NULL AND spec_version_id IS NULL) OR "
    "(subject_type = 'assembly_revision' AND assembly_revision_id IS NOT NULL AND model_revision_id IS NULL "
    "AND drawing_id IS NULL AND spec_version_id IS NULL) OR "
    "(subject_type = 'engineering_spec_version' AND spec_version_id IS NOT NULL AND model_revision_id IS NULL "
    "AND drawing_id IS NULL AND assembly_revision_id IS NULL)"
)
SOURCES = "policy_source IS NULL OR policy_source IN ('explicit', 'inherited', 'project_default', 'default')"


def upgrade() -> None:
    op.add_column('projects', sa.Column('default_execution_policy', sa.String(length=20), server_default='standard', nullable=False))
    op.create_check_constraint('ck_projects_default_policy', 'projects', "default_execution_policy IN ('standard', 'turbo')")

    op.add_column('workflow_runs', sa.Column('policy_source', sa.String(length=20), nullable=True))
    op.create_check_constraint('ck_workflow_runs_policy_source', 'workflow_runs', SOURCES)

    op.add_column('artifact_checks', sa.Column('assembly_revision_id', sa.Uuid(), nullable=True))
    op.add_column('artifact_checks', sa.Column('spec_version_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_artifact_checks_assembly_revision', 'artifact_checks', 'assembly_revisions', ['assembly_revision_id'], ['id'],
                          ondelete='CASCADE')
    op.create_foreign_key('fk_artifact_checks_spec_version', 'artifact_checks', 'engineering_spec_versions', ['spec_version_id'], ['id'],
                          ondelete='CASCADE')
    op.drop_constraint('ck_artifact_checks_subject', 'artifact_checks', type_='check')
    op.create_check_constraint('ck_artifact_checks_subject', 'artifact_checks', NEW_SUBJECTS)
    op.drop_constraint('ck_artifact_checks_target', 'artifact_checks', type_='check')
    op.create_check_constraint('ck_artifact_checks_target', 'artifact_checks', NEW_TARGET)
    op.create_index('ix_artifact_checks_assembly_revision', 'artifact_checks', ['assembly_revision_id', 'created_at'])
    op.create_index('ix_artifact_checks_spec_version', 'artifact_checks', ['spec_version_id', 'created_at'])


def downgrade() -> None:
    # Decisions on assembly revisions / spec versions have no older equivalent: they are removed with their columns.
    op.execute("DELETE FROM artifact_checks WHERE subject_type IN ('assembly_revision', 'engineering_spec_version')")
    op.drop_index('ix_artifact_checks_spec_version', table_name='artifact_checks')
    op.drop_index('ix_artifact_checks_assembly_revision', table_name='artifact_checks')
    op.drop_constraint('ck_artifact_checks_target', 'artifact_checks', type_='check')
    op.drop_constraint('ck_artifact_checks_subject', 'artifact_checks', type_='check')
    op.drop_constraint('fk_artifact_checks_spec_version', 'artifact_checks', type_='foreignkey')
    op.drop_constraint('fk_artifact_checks_assembly_revision', 'artifact_checks', type_='foreignkey')
    op.drop_column('artifact_checks', 'spec_version_id')
    op.drop_column('artifact_checks', 'assembly_revision_id')
    op.create_check_constraint('ck_artifact_checks_subject', 'artifact_checks', OLD_SUBJECTS)
    op.create_check_constraint('ck_artifact_checks_target', 'artifact_checks', OLD_TARGET)

    op.drop_constraint('ck_workflow_runs_policy_source', 'workflow_runs', type_='check')
    op.drop_column('workflow_runs', 'policy_source')

    op.drop_constraint('ck_projects_default_policy', 'projects', type_='check')
    op.drop_column('projects', 'default_execution_policy')
