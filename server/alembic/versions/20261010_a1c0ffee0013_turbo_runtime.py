"""W7 Turbo runtime: unverified completion, assumptions, check / acceptance states

- collaboration_node_tasks.phase gains ``completed_unverified`` (Turbo: real outputs released without review,
  approval or acceptance; never counted as accepted);
- collaboration_drafts.assumptions: the values a Turbo planner adopted (policy.Assumption dumps);
- artifact_checks: append-only engineering checks run later on one exact model revision / drawing (AC-T08).

Additive only; existing rows keep their meaning (standard).

Revision ID: a1c0ffee0013
Revises: a1c0ffee0012
Create Date: 2026-10-10 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0013'
down_revision: Union[str, None] = 'a1c0ffee0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_PHASES = "phase IN ('blocked', 'ready', 'executing', 'submitted', 'in_review', 'rework', 'accepted', 'failed', 'cancelled')"
NEW_PHASES = ("phase IN ('blocked', 'ready', 'executing', 'submitted', 'in_review', 'rework', 'accepted', 'failed', 'cancelled', "
              "'completed_unverified')")
CHECK_STATUSES = "status IN ('not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown')"


def upgrade() -> None:
    op.drop_constraint('ck_collab_node_tasks_phase', 'collaboration_node_tasks', type_='check')
    op.create_check_constraint('ck_collab_node_tasks_phase', 'collaboration_node_tasks', NEW_PHASES)
    op.add_column('collaboration_drafts', sa.Column('assumptions', postgresql.JSONB(astext_type=sa.Text()), server_default='[]',
                                                    nullable=False))
    op.create_table(
        'artifact_checks',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('subject_type', sa.String(length=30), nullable=False),
        sa.Column('model_revision_id', sa.Uuid(), nullable=True),
        sa.Column('drawing_id', sa.Uuid(), nullable=True),
        sa.Column('subject_sha', sa.String(length=64), nullable=True),
        sa.Column('check', sa.String(length=60), nullable=False),
        sa.Column('execution_policy', sa.String(length=20), server_default='standard', nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('requested_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("subject_type IN ('model_revision', 'drawing')", name='ck_artifact_checks_subject'),
        sa.CheckConstraint(CHECK_STATUSES, name='ck_artifact_checks_status'),
        sa.CheckConstraint("execution_policy IN ('standard', 'turbo')", name='ck_artifact_checks_policy'),
        sa.CheckConstraint("(subject_type = 'model_revision' AND model_revision_id IS NOT NULL AND drawing_id IS NULL) OR "
                           "(subject_type = 'drawing' AND drawing_id IS NOT NULL AND model_revision_id IS NULL)",
                           name='ck_artifact_checks_target'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_revision_id'], ['model_revisions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['drawing_id'], ['drawings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artifact_checks_project_id', 'artifact_checks', ['project_id'])
    op.create_index('ix_artifact_checks_revision', 'artifact_checks', ['model_revision_id', 'created_at'])
    op.create_index('ix_artifact_checks_drawing', 'artifact_checks', ['drawing_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_artifact_checks_drawing', table_name='artifact_checks')
    op.drop_index('ix_artifact_checks_revision', table_name='artifact_checks')
    op.drop_index('ix_artifact_checks_project_id', table_name='artifact_checks')
    op.drop_table('artifact_checks')
    op.drop_column('collaboration_drafts', 'assumptions')
    # A Turbo node that completed unverified has no older equivalent: it is closed as failed (never as accepted).
    op.execute("UPDATE collaboration_node_tasks SET phase = 'failed' WHERE phase = 'completed_unverified'")
    op.drop_constraint('ck_collab_node_tasks_phase', 'collaboration_node_tasks', type_='check')
    op.create_check_constraint('ck_collab_node_tasks_phase', 'collaboration_node_tasks', OLD_PHASES)
