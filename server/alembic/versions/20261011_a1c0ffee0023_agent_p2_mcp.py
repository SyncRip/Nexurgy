"""P2 MCP tokens: per-attempt token hash and expiry on agent_executions

Owned by the P2 package (.trellis/tasks/10-11-agent-execution-layer/design.md §7.2, §7.4):

- agent_executions.mcp_token_hash: SHA-256 of the attempt's /agent-mcp bearer token (the plaintext is returned once to
  the caller and never stored);
- agent_executions.mcp_token_expires_at: follows the attempt's lease;
- a unique partial index on the hash (the bearer lookup).

Additive only: no execution has a token before P3's workspace worker issues one.

Revision ID: a1c0ffee0023
Revises: a1c0ffee0022
Create Date: 2026-10-11 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1c0ffee0023'
down_revision: Union[str, None] = 'a1c0ffee0022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_executions', sa.Column('mcp_token_hash', sa.LargeBinary(), nullable=True))
    op.add_column('agent_executions', sa.Column('mcp_token_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ux_agent_executions_mcp_token_hash', 'agent_executions', ['mcp_token_hash'], unique=True,
                    postgresql_where=sa.text('mcp_token_hash IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('ux_agent_executions_mcp_token_hash', table_name='agent_executions')
    op.drop_column('agent_executions', 'mcp_token_expires_at')
    op.drop_column('agent_executions', 'mcp_token_hash')
