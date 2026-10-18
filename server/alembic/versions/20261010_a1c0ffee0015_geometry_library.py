"""W3 geometry library: part template metadata

Placeholder created by the W0 contract so the parallel work packages share one linear Alembic chain.
W3 needs no schema change: the new ops live in the existing ``model_revisions.ops`` JSONB (old ops dump exactly as
before), and the template provenance (``checks["template"]``) and the independent feature-check report
(``checks["features"]``) live in the existing ``model_revisions.checks`` JSONB. Kept as a no-op.

Revision ID: a1c0ffee0015
Revises: a1c0ffee0014
Create Date: 2026-10-10 10:00:00
"""
from typing import Sequence, Union

revision: str = 'a1c0ffee0015'
down_revision: Union[str, None] = 'a1c0ffee0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
