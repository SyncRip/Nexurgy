"""Retire unused operation kinds only when their historical row count is zero.

Revision ID: a1c0ffee0029
Revises: a1c0ffee0028
Create Date: 2026-10-16
"""

from alembic import op

revision = "a1c0ffee0029"
down_revision = "a1c0ffee0028"
branch_labels = None
depends_on = None

CURRENT_KINDS = ("task_draft", "model_create", "model_modify", "drawing_intake", "ask", "drawing_generate",
                 "drawing_reconstruct", "workflow_edit", "assembly_create", "assembly_modify", "workflow_create")
RETIRED_KINDS = ("spec_extract", "drafting_create")


def _check(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{kind}'" for kind in kinds) + ")"


def upgrade() -> None:
    # Hold the write barrier from the data preflight through the constraint replacement.
    # Reads remain available. A refusal rolls back this entire migration, including its version.
    op.execute("LOCK TABLE ai_operations IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
        DO $$
        DECLARE historical_counts jsonb;
        BEGIN
            SELECT jsonb_object_agg(kind, total) INTO historical_counts
            FROM (SELECT kind, count(*) AS total FROM ai_operations
                  WHERE kind IN ('spec_extract', 'drafting_create') GROUP BY kind) counts;
            IF historical_counts IS NOT NULL THEN
                RAISE EXCEPTION 'Cannot retire operation kinds: historical rows %. Preserve these records and resolve the retirement before retrying.', historical_counts;
            END IF;
        END $$
    """)
    op.drop_constraint("ck_ai_operations_kind", "ai_operations", type_="check")
    op.create_check_constraint("ck_ai_operations_kind", "ai_operations", _check(CURRENT_KINDS))


def downgrade() -> None:
    op.drop_constraint("ck_ai_operations_kind", "ai_operations", type_="check")
    op.create_check_constraint("ck_ai_operations_kind", "ai_operations", _check(CURRENT_KINDS + RETIRED_KINDS))
