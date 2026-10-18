"""General mechanical engineering W0 contract: execution policy columns, partial runs, new asset types and AI operation kinds

Additive only. Every existing row keeps execution_policy = 'standard' (architecture §10: old runs map to standard).

Revision ID: a1c0ffee0011
Revises: a1c0ffee0010
Create Date: 2026-10-10 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1c0ffee0011'
down_revision: Union[str, None] = 'a1c0ffee0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_TABLES = (
    ("workflow_runs", "ck_workflow_runs_policy"),
    ("collaboration_drafts", "ck_collab_drafts_policy"),
    ("collaboration_runs", "ck_collab_runs_policy"),
    ("ai_operations", "ck_ai_operations_policy"),
)

OLD_RUN = "status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')"
NEW_RUN = "status IN ('queued', 'running', 'waiting', 'succeeded', 'partial', 'failed', 'cancelled')"
OLD_ASSET = "type IN ('part_model', 'drawing', 'process_plan', 'quote', 'document', 'intake', 'process_module')"
NEW_ASSET = ("type IN ('part_model', 'drawing', 'process_plan', 'quote', 'document', 'intake', 'process_module', "
             "'assembly_model', 'assembly_drawing', 'drawing_document', 'engineering_spec', 'delivery_manifest')")
OLD_KINDS = ("kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask', "
             "'drawing_generate', 'drawing_reconstruct', 'workflow_edit')")
NEW_KINDS = ("kind IN ('task_draft', 'model_create', 'model_modify', 'drawing_intake', 'ask', "
             "'drawing_generate', 'drawing_reconstruct', 'workflow_edit', 'spec_extract', 'assembly_create', "
             "'assembly_modify', 'drafting_create')")


def upgrade() -> None:
    for table, ck in POLICY_TABLES:
        op.add_column(table, sa.Column("execution_policy", sa.String(20), server_default="standard", nullable=False))
        op.create_check_constraint(ck, table, "execution_policy IN ('standard', 'turbo')")
    op.drop_constraint('ck_workflow_runs_status', 'workflow_runs', type_='check')
    op.create_check_constraint('ck_workflow_runs_status', 'workflow_runs', NEW_RUN)
    op.drop_constraint('ck_assets_type', 'assets', type_='check')
    op.create_check_constraint('ck_assets_type', 'assets', NEW_ASSET)
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', NEW_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM ai_operations WHERE kind IN ('spec_extract', 'assembly_create', 'assembly_modify', 'drafting_create')")
    op.drop_constraint('ck_ai_operations_kind', 'ai_operations', type_='check')
    op.create_check_constraint('ck_ai_operations_kind', 'ai_operations', OLD_KINDS)
    op.execute("DELETE FROM assets WHERE type IN ('assembly_model', 'assembly_drawing', 'drawing_document', 'engineering_spec', 'delivery_manifest')")
    op.drop_constraint('ck_assets_type', 'assets', type_='check')
    op.create_check_constraint('ck_assets_type', 'assets', OLD_ASSET)
    op.execute("UPDATE workflow_runs SET status = 'failed' WHERE status = 'partial'")
    op.drop_constraint('ck_workflow_runs_status', 'workflow_runs', type_='check')
    op.create_check_constraint('ck_workflow_runs_status', 'workflow_runs', OLD_RUN)
    for table, ck in POLICY_TABLES:
        op.drop_constraint(ck, table, type_='check')
        op.drop_column(table, "execution_policy")
