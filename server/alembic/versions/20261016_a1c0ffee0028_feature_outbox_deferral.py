"""defer outbox events while features are frozen

Revision ID: a1c0ffee0028
Revises: a1c0ffee0027
Create Date: 2026-10-16 14:51:45.927163
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c0ffee0028'
down_revision: Union[str, None] = 'a1c0ffee0027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('outbox_events', sa.Column('deferred_feature', sa.String(length=40), nullable=True))
    op.add_column('outbox_events', sa.Column('deferred_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('outbox_events', 'deferred_at')
    op.drop_column('outbox_events', 'deferred_feature')
