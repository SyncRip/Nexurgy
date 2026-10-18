"""Generation supplement A: engineering spec versions derived by a server rule

Owned by package A of the generation workflow supplement (.trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §3 A
item 3): ck_engineering_spec_versions_origin also allows ``rule`` (a version a server rule derived from a finished one —
Turbo hangs part definitions that hang nowhere under the root — with the parent's sources and chunks; no model call).

Downgrade (development only) turns those versions into ``reextract`` rows, as the S3 migration does for ``continue``.

Revision ID: a1c0ffee0026
Revises: a1c0ffee0025
Create Date: 2026-10-12 14:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1c0ffee0026'
down_revision: Union[str, None] = 'a1c0ffee0025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORIGINS_OLD = "origin IN ('create', 'answer', 'reextract', 'continue')"
ORIGINS_NEW = "origin IN ('create', 'answer', 'reextract', 'continue', 'rule')"


def upgrade() -> None:
    op.drop_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', type_='check')
    op.create_check_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', ORIGINS_NEW)


def downgrade() -> None:
    op.drop_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', type_='check')
    op.execute("UPDATE engineering_spec_versions SET origin = 'reextract' WHERE origin = 'rule'")
    op.create_check_constraint('ck_engineering_spec_versions_origin', 'engineering_spec_versions', ORIGINS_OLD)
