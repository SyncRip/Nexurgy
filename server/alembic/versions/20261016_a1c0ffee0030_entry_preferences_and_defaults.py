"""Account preferences, model purpose defaults and independent import provenance.

Revision ID: a1c0ffee0030
Revises: a1c0ffee0029
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1c0ffee0030"
down_revision = "a1c0ffee0029"
branch_labels = None
depends_on = None
PURPOSES = ("assistant", "modeling", "intake", "planning")


def upgrade() -> None:
    op.execute("LOCK TABLE model_configs IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""DO $$ DECLARE ids text; BEGIN
      SELECT string_agg(id::text, ', ' ORDER BY id) INTO ids FROM model_configs WHERE is_default
      HAVING count(*) > 1;
      IF ids IS NOT NULL THEN RAISE EXCEPTION 'Duplicate model defaults; resolve before migration: %', ids; END IF;
    END $$""")
    op.add_column("users", sa.Column("ai_autonomy", sa.String(20), nullable=False, server_default="auto"))
    op.add_column("users", sa.Column("preferences_version", sa.Integer(), nullable=False, server_default="1"))
    op.execute("""UPDATE users u SET ai_autonomy = CASE WHEN latest.autonomy = 'step' THEN 'step' ELSE 'auto' END
      FROM (SELECT DISTINCT ON (user_id) user_id, autonomy FROM ai_conversations
            ORDER BY user_id, created_at DESC, id DESC) latest WHERE latest.user_id = u.id""")
    op.create_check_constraint("ck_users_ai_autonomy", "users", "ai_autonomy IN ('auto', 'step')")
    op.create_check_constraint("ck_users_preferences_version", "users", "preferences_version > 0")
    op.add_column("model_configs", sa.Column("default_for", postgresql.ARRAY(sa.String(30)), nullable=False, server_default="{}"))
    op.create_check_constraint("ck_model_configs_default_for", "model_configs",
                               "default_for <@ ARRAY['assistant','modeling','intake','planning']::varchar[]")
    op.create_index("uq_model_configs_global_default", "model_configs", [sa.text("(1)")], unique=True,
                    postgresql_where=sa.text("is_default"))
    for purpose in PURPOSES:
        op.create_index(f"uq_model_configs_default_{purpose}", "model_configs", [sa.text("(1)")], unique=True,
                        postgresql_where=sa.text(f"'{purpose}' = ANY(default_for)"))
    op.add_column("workflows", sa.Column("import_source", postgresql.JSONB()))
    op.add_column("workflows", sa.Column("import_request_id", sa.Uuid()))
    op.create_index("uq_workflows_import_request", "workflows", ["project_id", "created_by", "import_request_id"],
                    unique=True, postgresql_where=sa.text("import_request_id IS NOT NULL"))
    op.add_column("workflow_template_versions", sa.Column("import_source", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("workflow_template_versions", "import_source")
    op.drop_index("uq_workflows_import_request", table_name="workflows")
    op.drop_column("workflows", "import_request_id")
    op.drop_column("workflows", "import_source")
    for purpose in PURPOSES:
        op.drop_index(f"uq_model_configs_default_{purpose}", table_name="model_configs")
    op.drop_index("uq_model_configs_global_default", table_name="model_configs")
    op.drop_constraint("ck_model_configs_default_for", "model_configs", type_="check")
    op.drop_column("model_configs", "default_for")
    op.drop_constraint("ck_users_preferences_version", "users", type_="check")
    op.drop_constraint("ck_users_ai_autonomy", "users", type_="check")
    op.drop_column("users", "preferences_version")
    op.drop_column("users", "ai_autonomy")
