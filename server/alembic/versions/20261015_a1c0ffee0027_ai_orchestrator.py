"""AI native main agent: conversation autonomy and stored task drafts

Owned by package B of the AI-native round (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §2.1):

- ai_conversations.autonomy (auto | confirm_task | step, default confirm_task): what the main agent asks the person
  before a write tool (nothing / only before creating a task / before every write).
- ai_task_drafts: task drafts the main agent stored, private to their conversation (like the conversation itself),
  editable while open and turned into a task once (used). (operation_id, step_index) is unique: the turn step that made
  the draft finds it again when it is replayed.

Main-agent turns and sub-agents are ordinary ai_operations (ask with request.runtime = orchestrator; model / drawing
operations with request.parent_op_id): no kind or status changes.

Revision ID: a1c0ffee0027
Revises: a1c0ffee0026
Create Date: 2026-10-15 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0027'
down_revision: Union[str, None] = 'a1c0ffee0026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_conversations', sa.Column('autonomy', sa.String(length=20), server_default='confirm_task', nullable=False))
    op.create_check_constraint('ck_ai_conversations_autonomy', 'ai_conversations', "autonomy IN ('auto', 'confirm_task', 'step')")

    op.create_table(
        'ai_task_drafts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('conversation_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('operation_id', sa.Uuid(), nullable=True),
        sa.Column('step_index', sa.Integer(), nullable=False),
        sa.Column('part_id', sa.Uuid(), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('objective', sa.Text(), server_default='', nullable=False),
        sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('suggested_workflow_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=True),
        sa.Column('simulated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('open', 'used', 'discarded')", name='ck_ai_task_drafts_status'),
        sa.ForeignKeyConstraint(['conversation_id'], ['ai_conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['operation_id'], ['ai_operations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['part_id'], ['parts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['suggested_workflow_id'], ['workflows.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('operation_id', 'step_index', name='uq_ai_task_drafts_step'),
    )
    op.create_index('ix_ai_task_drafts_conversation_id', 'ai_task_drafts', ['conversation_id'])


def downgrade() -> None:
    op.drop_index('ix_ai_task_drafts_conversation_id', table_name='ai_task_drafts')
    op.drop_table('ai_task_drafts')
    op.drop_constraint('ck_ai_conversations_autonomy', 'ai_conversations', type_='check')
    op.drop_column('ai_conversations', 'autonomy')
