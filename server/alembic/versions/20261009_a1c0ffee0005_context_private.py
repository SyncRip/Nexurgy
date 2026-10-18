"""ai_context_builds.private_context: the private part of a modeling context

The run graph (node params) is visible to every project member, so the private
conversation context of an AI workspace modeling operation (pinned constraints,
memory, other references) is stored with the owner's context build instead and
read by the model_3d handler of that operation's own run only.

Revision ID: a1c0ffee0005
Revises: a1c0ffee0004
Create Date: 2026-10-09 18:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1c0ffee0005'
down_revision: Union[str, None] = 'a1c0ffee0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_context_builds', sa.Column('private_context', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_context_builds', 'private_context')
