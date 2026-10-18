"""W6 standalone drafting: drawing documents and versions

Tables ``drawing_documents`` (project-scoped, no part / model revision) and ``drawing_document_versions``
(immutable: a trigger refuses UPDATE), plus the asset target column
``asset_versions.drawing_document_version_id``. The one-target CHECK of ``asset_versions`` is rebuilt from the
column list the database currently has, so other work packages adding their own target column in an earlier or
later migration compose with this one in any order.

Revision ID: a1c0ffee0017
Revises: a1c0ffee0016
Create Date: 2026-10-10 10:00:00
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0017'
down_revision: Union[str, None] = 'a1c0ffee0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ONE_TARGET = "ck_asset_versions_one_target"
COLUMN = "drawing_document_version_id"
POLICIES = "execution_policy IN ('standard', 'turbo')"
ORIGINS = "origin IN ('empty', 'text', 'file', 'image', 'edit', 'restore', 'calibrate')"
SCALE = "scale_status IN ('unknown', 'calibrated', 'assumed', 'native')"
CHECKS = "check_status IN ('not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown')"
ACCEPT = "acceptance_status IN ('not_requested', 'pending', 'accepted', 'rejected')"
OUTPUT = "output_status IN ('succeeded', 'partial')"


def _jsonb_list(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False)


def _jsonb_dict(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False)


def _target_columns() -> list[str]:
    """The columns of the live one-target CHECK (``num_nonnulls(a, b, …) = 1``)."""
    definition = op.get_bind().execute(
        sa.text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"), {"name": ONE_TARGET}
    ).scalar()
    m = re.search(r"num_nonnulls\(([^)]*)\)", definition or "")
    if m is None:
        raise RuntimeError(f"{ONE_TARGET} has an unexpected definition: {definition!r}")
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _set_targets(columns: list[str]) -> None:
    op.drop_constraint(ONE_TARGET, "asset_versions", type_="check")
    op.create_check_constraint(ONE_TARGET, "asset_versions", f"num_nonnulls({', '.join(columns)}) = 1")


def upgrade() -> None:
    op.create_table(
        "drawing_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.String(80), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("head_version_id", sa.Uuid(), nullable=True),
        sa.Column("head_version_no", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "number", name="uq_drawing_documents_project_number"),
        sa.CheckConstraint("status IN ('draft', 'released')", name="ck_drawing_documents_status"),
    )
    op.create_index("ix_drawing_documents_project_id", "drawing_documents", ["project_id"])
    op.create_table(
        "drawing_document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("drawing_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), sa.ForeignKey("drawing_document_versions.id"), nullable=True),
        sa.Column("restored_from_id", sa.Uuid(), sa.ForeignKey("drawing_document_versions.id"), nullable=True),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("execution_policy", sa.String(20), server_default="standard", nullable=False),
        _jsonb_list("sources"),
        _jsonb_list("sheets"),
        _jsonb_list("layers"),
        _jsonb_list("entities"),
        _jsonb_list("blocks"),
        _jsonb_list("underlays"),
        sa.Column("next_entity_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column("scale_status", sa.String(20), nullable=False),
        _jsonb_dict("calibration"),
        _jsonb_dict("provenance"),
        _jsonb_list("conflicts"),
        _jsonb_list("assumptions"),
        _jsonb_list("open_questions"),
        _jsonb_list("unsupported"),
        _jsonb_list("commands"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("check_status", sa.String(20), nullable=False),
        sa.Column("acceptance_status", sa.String(20), server_default="not_requested", nullable=False),
        _jsonb_dict("checks"),
        _jsonb_dict("verification"),
        sa.Column("output_status", sa.String(20), nullable=False),
        _jsonb_list("files"),
        _jsonb_dict("dwg"),
        sa.Column("dxf_sha", sa.String(64), sa.ForeignKey("blobs.sha256"), nullable=False),
        sa.Column("dwg_sha", sa.String(64), sa.ForeignKey("blobs.sha256"), nullable=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("labels", postgresql.ARRAY(sa.String(40)), server_default="{}", nullable=False),
        sa.Column("ai", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_drawing_document_versions_no"),
        sa.CheckConstraint(POLICIES, name="ck_drawing_document_versions_policy"),
        sa.CheckConstraint(ORIGINS, name="ck_drawing_document_versions_origin"),
        sa.CheckConstraint(SCALE, name="ck_drawing_document_versions_scale"),
        sa.CheckConstraint(CHECKS, name="ck_drawing_document_versions_check"),
        sa.CheckConstraint(ACCEPT, name="ck_drawing_document_versions_acceptance"),
        sa.CheckConstraint(OUTPUT, name="ck_drawing_document_versions_output"),
    )
    op.create_index("ix_drawing_document_versions_document_id", "drawing_document_versions", ["document_id"])
    op.create_foreign_key("fk_drawing_documents_head", "drawing_documents", "drawing_document_versions",
                          ["head_version_id"], ["id"], ondelete="SET NULL")
    # History is immutable: a version row is written once and never updated.
    op.execute("""
        CREATE FUNCTION drawing_document_versions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'drawing document versions are immutable (version %)', OLD.id;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_drawing_document_versions_immutable
        BEFORE UPDATE ON drawing_document_versions
        FOR EACH ROW EXECUTE FUNCTION drawing_document_versions_immutable()
    """)

    op.add_column("asset_versions", sa.Column(COLUMN, sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_asset_versions_drawing_document_version", "asset_versions", "drawing_document_versions",
                          [COLUMN], ["id"], ondelete="CASCADE")
    op.create_unique_constraint("asset_versions_drawing_document_version_id_key", "asset_versions", [COLUMN])
    columns = _target_columns()
    if COLUMN not in columns:
        _set_targets([*columns, COLUMN])


DOCUMENT_AVS = f"SELECT id FROM asset_versions WHERE {COLUMN} IS NOT NULL"
#: Tables whose asset_version_id references asset_versions without ON DELETE (rows bound to a drawing document go;
#: same list as a1c0ffee0016).
NO_ACTION_REFS = ("run_input_bindings", "submission_items", "task_baseline_items", "task_deliverable_drafts", "approval_requests")


def downgrade() -> None:
    # Rows that point at a drawing document asset version (NO ACTION foreign keys) would block the delete below.
    op.execute(f"UPDATE milestone_review_items SET asset_version_id = NULL "
               f"WHERE asset_version_id IN ({DOCUMENT_AVS}) AND submission_id IS NOT NULL")
    op.execute(f"DELETE FROM milestone_review_items WHERE asset_version_id IN ({DOCUMENT_AVS})")
    for table in NO_ACTION_REFS:
        op.execute(f"DELETE FROM {table} WHERE asset_version_id IN ({DOCUMENT_AVS})")
    op.execute(f"DELETE FROM asset_versions WHERE {COLUMN} IS NOT NULL")
    op.execute("DELETE FROM assets WHERE type = 'drawing_document'")
    _set_targets([c for c in _target_columns() if c != COLUMN])
    op.drop_constraint("asset_versions_drawing_document_version_id_key", "asset_versions", type_="unique")
    op.drop_constraint("fk_asset_versions_drawing_document_version", "asset_versions", type_="foreignkey")
    op.drop_column("asset_versions", COLUMN)

    op.execute("DROP TRIGGER IF EXISTS trg_drawing_document_versions_immutable ON drawing_document_versions")
    op.execute("DROP FUNCTION IF EXISTS drawing_document_versions_immutable()")
    op.drop_constraint("fk_drawing_documents_head", "drawing_documents", type_="foreignkey")
    op.drop_index("ix_drawing_document_versions_document_id", table_name="drawing_document_versions")
    op.drop_table("drawing_document_versions")
    op.drop_index("ix_drawing_documents_project_id", table_name="drawing_documents")
    op.drop_table("drawing_documents")
