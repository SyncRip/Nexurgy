"""Immutable agent design versions and bound human responses.

Revision ID: a1c0ffee0032
Revises: a1c0ffee0031
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'a1c0ffee0032'
down_revision = 'a1c0ffee0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ai_design_choices',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('root_operation_id', sa.Uuid(), sa.ForeignKey('ai_operations.id'), nullable=False),
        sa.Column('operation_id', sa.Uuid(), sa.ForeignKey('ai_operations.id'), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('plan_version_id', sa.Uuid(), nullable=False),
        sa.Column('client_request_id', sa.Uuid(), nullable=False),
        sa.Column('request', postgresql.JSONB(), nullable=False),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('parameter_overrides', postgresql.JSONB(), nullable=False),
        sa.Column('resume_operation_id', sa.Uuid(), sa.ForeignKey('ai_operations.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('root_operation_id', 'client_request_id', name='uq_ai_design_choice_request'),
        sa.UniqueConstraint('plan_version_id', name='uq_ai_design_choice_version'))
    op.create_table('ai_design_plan_versions',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('plan_id', sa.Uuid(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('parent_version_id', sa.Uuid(), sa.ForeignKey('ai_design_plan_versions.id')),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('conversation_id', sa.Uuid(), sa.ForeignKey('ai_conversations.id'), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('root_operation_id', sa.Uuid(), sa.ForeignKey('ai_operations.id'), nullable=False),
        sa.Column('authoring_operation_id', sa.Uuid(), sa.ForeignKey('ai_operations.id'), nullable=False),
        sa.Column('choice_id', sa.Uuid(), sa.ForeignKey('ai_design_choices.id')),
        sa.Column('content', postgresql.JSONB(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('source_snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('provenance', postgresql.JSONB(), nullable=False),
        sa.Column('authorization', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('version > 0', name='ck_ai_design_plan_version'),
        sa.UniqueConstraint('plan_id', 'version', name='uq_ai_design_plan_version'),
        sa.UniqueConstraint('root_operation_id', 'choice_id', name='uq_ai_design_plan_choice'))
    for field in ('conversation_id', 'root_operation_id'):
        op.create_index('ix_ai_design_plan_versions_' + field, 'ai_design_plan_versions', [field])
    op.execute("""CREATE FUNCTION reject_ai_design_update() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'AI design history is immutable'; END;
        $$ LANGUAGE plpgsql""")
    for table in ('ai_design_plan_versions', 'ai_design_choices'):
        op.execute(f'CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION reject_ai_design_update()')


def downgrade() -> None:
    op.drop_table('ai_design_plan_versions')
    op.drop_table('ai_design_choices')
    op.execute('DROP FUNCTION reject_ai_design_update()')
