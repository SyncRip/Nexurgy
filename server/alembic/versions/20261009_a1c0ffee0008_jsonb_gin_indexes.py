"""GIN (jsonb_path_ops) indexes for blob authorisation lookups

``GET /blobs/{digest}`` finds the projects that reference a digest with JSONB containment (``@>``): drawing package
pages / manifests (page files, check reports), revision checks (the derived DXF of a DWG source) and node outputs
(the derived DXF of a reconstruction that waits for an answer). Without these indexes every blob request scans the
tables.

Revision ID: a1c0ffee0008
Revises: a1c0ffee0007
Create Date: 2026-10-09 18:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1c0ffee0008'
down_revision: Union[str, None] = 'a1c0ffee0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = (
    ("ix_drawings_pages_gin", "drawings", "pages"),
    ("ix_drawings_manifest_gin", "drawings", "manifest"),
    ("ix_model_revisions_checks_gin", "model_revisions", "checks"),
    ("ix_node_runs_outputs_gin", "node_runs", "outputs"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], postgresql_using="gin", postgresql_ops={column: "jsonb_path_ops"})


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
