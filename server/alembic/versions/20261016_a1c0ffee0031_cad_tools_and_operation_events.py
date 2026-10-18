"""Real CAD tools and private, generation-aware completion events in the outbox.

Revision ID: a1c0ffee0031
Revises: a1c0ffee0030
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c0ffee0031"
down_revision = "a1c0ffee0030"
branch_labels = None
depends_on = None

OLD_KINDS = ("task_draft", "model_create", "model_modify", "drawing_intake", "ask", "drawing_generate",
             "drawing_reconstruct", "workflow_edit", "assembly_create", "assembly_modify", "workflow_create")
NEW_KINDS = ("spec_extract", "drafting_create", "drafting_modify")
FIELDS = ("ai_operation_id", "ai_generation", "ai_event_seq", "ai_phase", "ai_wake_state", "ai_wake_turn_id", "ai_notice_seq", "ai_consumed_at")
SHAPE = ("(kind = 'ai.operation.completed' AND ai_operation_id IS NOT NULL AND ai_generation IS NOT NULL AND ai_generation > 0 "
         "AND ai_event_seq IS NOT NULL AND ai_event_seq > 0 AND ai_phase IS NOT NULL AND ai_phase IN ('terminal','applied','undone') "
         "AND ai_wake_state IS NOT NULL AND ai_wake_state IN ('pending','assigned','consumed','suppressed','delivery_failed','legacy_consumed')) OR "
         "(kind <> 'ai.operation.completed' AND " + " AND ".join(f"{field} IS NULL" for field in FIELDS) + ")")


def _kinds(kinds):
    return "kind IN (" + ",".join(repr(k) for k in kinds) + ")"


def upgrade():
    op.drop_constraint("ck_ai_operations_kind", "ai_operations", type_="check")
    op.create_check_constraint("ck_ai_operations_kind", "ai_operations", _kinds(OLD_KINDS + NEW_KINDS))
    op.add_column("ai_operations", sa.Column("generation", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_ai_operations_generation", "ai_operations", "generation > 0")
    for name, typ in (("ai_operation_id", sa.Uuid()), ("ai_generation", sa.Integer()), ("ai_event_seq", sa.Integer()),
                      ("ai_phase", sa.String(20)), ("ai_wake_state", sa.String(30)), ("ai_wake_turn_id", sa.Uuid()),
                      ("ai_notice_seq", sa.Integer()), ("ai_consumed_at", sa.DateTime(timezone=True))):
        op.add_column("outbox_events", sa.Column(name, typ, nullable=True))
    op.create_foreign_key("fk_outbox_ai_operation", "outbox_events", "ai_operations", ["ai_operation_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_outbox_ai_wake_turn", "outbox_events", "ai_operations", ["ai_wake_turn_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint("ck_outbox_events_ai_shape", "outbox_events", SHAPE)
    op.create_index("uq_outbox_ai_generation_phase", "outbox_events", ["ai_operation_id", "ai_generation", "ai_phase"],
                    unique=True, postgresql_where=sa.text("kind = 'ai.operation.completed'"))
    op.create_index("uq_outbox_ai_sequence", "outbox_events", ["ai_operation_id", "ai_event_seq"],
                    unique=True, postgresql_where=sa.text("kind = 'ai.operation.completed'"))
    op.create_index("ix_outbox_ai_pending", "outbox_events", ["kind", "ai_wake_state", "created_at", "id"])
    op.create_index("ix_outbox_events_ai_operation_id", "outbox_events", ["ai_operation_id"])
    op.create_index("ix_outbox_events_ai_wake_turn_id", "outbox_events", ["ai_wake_turn_id"])


def downgrade():
    op.execute("LOCK TABLE ai_operations, outbox_events IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_operations WHERE generation > 1 OR kind IN ('spec_extract','drafting_create','drafting_modify'))
           OR EXISTS (SELECT 1 FROM outbox_events WHERE kind = 'ai.operation.completed') THEN
            RAISE EXCEPTION 'Cannot downgrade: CAD operation or completion history must be preserved';
        END IF;
    END $$""")
    for name in ("uq_outbox_ai_generation_phase", "uq_outbox_ai_sequence", "ix_outbox_ai_pending",
                 "ix_outbox_events_ai_operation_id", "ix_outbox_events_ai_wake_turn_id"):
        op.drop_index(name, table_name="outbox_events")
    op.drop_constraint("ck_outbox_events_ai_shape", "outbox_events", type_="check")
    op.drop_constraint("fk_outbox_ai_operation", "outbox_events", type_="foreignkey")
    op.drop_constraint("fk_outbox_ai_wake_turn", "outbox_events", type_="foreignkey")
    for field in reversed(FIELDS):
        op.drop_column("outbox_events", field)
    op.drop_constraint("ck_ai_operations_generation", "ai_operations", type_="check")
    op.drop_column("ai_operations", "generation")
    op.drop_constraint("ck_ai_operations_kind", "ai_operations", type_="check")
    op.create_check_constraint("ck_ai_operations_kind", "ai_operations", _kinds(OLD_KINDS))
