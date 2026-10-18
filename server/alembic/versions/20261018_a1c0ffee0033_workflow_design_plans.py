"""Immutable workflow-scoped concept design authority.

Revision ID: a1c0ffee0033
Revises: a1c0ffee0032
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'a1c0ffee0033'
down_revision = 'a1c0ffee0032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('workflow_design_plan_versions',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('plan_id', sa.Uuid(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('run_id', sa.Uuid(), sa.ForeignKey('workflow_runs.id'), nullable=False),
        sa.Column('node_run_id', sa.Uuid(), sa.ForeignKey('node_runs.id'), nullable=False),
        sa.Column('node_id', sa.String(60), nullable=False),
        sa.Column('authoring_attempt', sa.Integer(), nullable=False),
        sa.Column('content', postgresql.JSONB(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('source_snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('provenance', postgresql.JSONB(), nullable=False),
        sa.Column('authorization', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('version = 1', name='ck_workflow_design_plan_version'),
        sa.CheckConstraint('authoring_attempt > 0', name='ck_workflow_design_plan_attempt'),
        sa.UniqueConstraint('plan_id', 'version', name='uq_workflow_design_plan_version'),
        sa.UniqueConstraint('node_run_id', name='uq_workflow_design_plan_node'))
    op.create_index('ix_workflow_design_plan_versions_run_id', 'workflow_design_plan_versions', ['run_id'])
    op.execute("""CREATE FUNCTION reject_workflow_design_update() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Workflow design history is immutable'; END;
        $$ LANGUAGE plpgsql""")
    op.execute('CREATE TRIGGER immutable_workflow_design_plan_versions BEFORE UPDATE ON workflow_design_plan_versions '
               'FOR EACH ROW EXECUTE FUNCTION reject_workflow_design_update()')


def downgrade() -> None:
    op.drop_table('workflow_design_plan_versions')
    op.execute('DROP FUNCTION reject_workflow_design_update()')
