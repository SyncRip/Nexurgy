"""unified AI workspace: conversations, messages, operations, context builds

Design: .trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §2. Messages and
operations reference each other: the message → operation link is added after
both tables exist.

Revision ID: a1c0ffee0004
Revises: a1c0ffee0003
Create Date: 2026-10-09 09:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0004'
down_revision: Union[str, None] = 'a1c0ffee0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text('now()')


def upgrade() -> None:
    op.create_table('ai_conversations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=200), server_default='', nullable=False),
    sa.Column('composer', JSONB, server_default=sa.text("""'{"target_key": null, "references": []}'::jsonb"""), nullable=False),
    sa.Column('constraints', JSONB, server_default='[]', nullable=False),
    sa.Column('summary', JSONB, nullable=True),
    sa.Column('summary_through_seq', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_seq', sa.Integer(), server_default='1', nullable=False),
    sa.Column('forked_from', JSONB, nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ai_conversations_user_project', 'ai_conversations', ['user_id', 'project_id', 'last_activity_at'], unique=False)
    op.create_index(op.f('ix_ai_conversations_project_id'), 'ai_conversations', ['project_id'], unique=False)
    op.create_index(op.f('ix_ai_conversations_task_id'), 'ai_conversations', ['task_id'], unique=False)

    op.create_table('ai_messages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('text', sa.Text(), server_default='', nullable=False),
    sa.Column('target', JSONB, nullable=True),
    sa.Column('references', JSONB, server_default='[]', nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=True),
    sa.Column('payload', JSONB, server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant')", name='ck_ai_messages_role'),
    sa.CheckConstraint("kind IN ('request', 'operation', 'answer', 'choice', 'notice')", name='ck_ai_messages_kind'),
    sa.ForeignKeyConstraint(['conversation_id'], ['ai_conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'seq', name='uq_ai_messages_conversation_seq')
    )

    op.create_table('ai_operations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=True),
    sa.Column('part_id', sa.Uuid(), nullable=True),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('client_request_id', sa.Uuid(), nullable=False),
    sa.Column('request', JSONB, nullable=False),
    sa.Column('question', JSONB, nullable=True),
    sa.Column('result', JSONB, nullable=True),
    sa.Column('error', JSONB, nullable=True),
    sa.Column('simulated', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('retry_of', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask')", name='ck_ai_operations_kind'),
    sa.CheckConstraint("status IN ('pending', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled')", name='ck_ai_operations_status'),
    sa.ForeignKeyConstraint(['conversation_id'], ['ai_conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['ai_messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['part_id'], ['parts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['retry_of'], ['ai_operations.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'client_request_id', name='uq_ai_operations_request')
    )
    op.create_index(op.f('ix_ai_operations_conversation_id'), 'ai_operations', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_ai_operations_run_id'), 'ai_operations', ['run_id'], unique=False)
    op.create_index('ix_ai_operations_part_status', 'ai_operations', ['part_id', 'status'], unique=False)
    op.create_foreign_key('fk_ai_messages_operation', 'ai_messages', 'ai_operations', ['operation_id'], ['id'], ondelete='SET NULL')

    op.create_table('ai_context_builds',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('model_config_id', sa.Uuid(), nullable=True),
    sa.Column('model_label', sa.String(length=160), server_default='', nullable=False),
    sa.Column('budget', JSONB, server_default='{}', nullable=False),
    sa.Column('items', JSONB, server_default='[]', nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error', JSONB, nullable=True),
    sa.Column('summary_through_seq', sa.Integer(), nullable=True),
    sa.Column('usage', JSONB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.ForeignKeyConstraint(['operation_id'], ['ai_operations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ai_context_builds_operation_id'), 'ai_context_builds', ['operation_id'], unique=False)

    op.add_column('ai_calls', sa.Column('ai_operation_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_ai_calls_ai_operation', 'ai_calls', 'ai_operations', ['ai_operation_id'], ['id'], ondelete='SET NULL')
    op.create_index(op.f('ix_ai_calls_ai_operation_id'), 'ai_calls', ['ai_operation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ai_calls_ai_operation_id'), table_name='ai_calls')
    op.drop_constraint('fk_ai_calls_ai_operation', 'ai_calls', type_='foreignkey')
    op.drop_column('ai_calls', 'ai_operation_id')
    op.drop_index(op.f('ix_ai_context_builds_operation_id'), table_name='ai_context_builds')
    op.drop_table('ai_context_builds')
    op.drop_constraint('fk_ai_messages_operation', 'ai_messages', type_='foreignkey')
    op.drop_index('ix_ai_operations_part_status', table_name='ai_operations')
    op.drop_index(op.f('ix_ai_operations_run_id'), table_name='ai_operations')
    op.drop_index(op.f('ix_ai_operations_conversation_id'), table_name='ai_operations')
    op.drop_table('ai_operations')
    op.drop_table('ai_messages')
    op.drop_index(op.f('ix_ai_conversations_task_id'), table_name='ai_conversations')
    op.drop_index(op.f('ix_ai_conversations_project_id'), table_name='ai_conversations')
    op.drop_index('ix_ai_conversations_user_project', table_name='ai_conversations')
    op.drop_table('ai_conversations')
