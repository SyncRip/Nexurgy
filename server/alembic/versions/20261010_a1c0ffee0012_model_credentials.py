"""W1 model credentials: encrypted direct keys, config / credential versions

Additive. model_configs gains the credential source / status / active version and a config version; secret_ref
becomes the environment reference of env credentials only (NULL for direct keys). New tables: model_credentials
(one row per credential version: env reference or AES-256-GCM ciphertext, never a plaintext key),
model_env_generations (server-side fingerprint of an environment key → generation number) and
model_credential_meta (the fingerprint salt). workflow_runs gains model_snapshot (configs frozen at start).

Every existing config becomes credential version 1 (source env, its current secret_ref), so env references keep
working without re-entering anything.

Downgrade is for test fixtures: it drops every direct key (the ciphertexts are deleted, never turned into
plaintext); configs without an environment reference are disabled and get a placeholder reference. A production
rollback keeps this schema and disables direct-key configs instead (credentials-deploy.zh-CN.md).

Revision ID: a1c0ffee0012
Revises: a1c0ffee0011
Create Date: 2026-10-10 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c0ffee0012'
down_revision: Union[str, None] = 'a1c0ffee0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLACEHOLDER_REF = "env:PARTROUTE_DIRECT_KEY_REMOVED"


def upgrade() -> None:
    op.add_column("model_configs", sa.Column("credential_source", sa.String(10), server_default="env", nullable=False))
    op.add_column("model_configs", sa.Column("credential_status", sa.String(12), server_default="active", nullable=False))
    op.add_column("model_configs", sa.Column("credential_version", sa.Integer(), nullable=True))
    op.add_column("model_configs", sa.Column("config_version", sa.Integer(), server_default="1", nullable=False))
    op.alter_column("model_configs", "secret_ref", existing_type=sa.String(200), nullable=True)
    op.create_check_constraint("ck_model_configs_credential_source", "model_configs", "credential_source IN ('env', 'direct')")
    op.create_check_constraint("ck_model_configs_credential_status", "model_configs",
                               "credential_status IN ('active', 'cleared', 'revoked', 'none')")
    op.create_check_constraint("ck_model_configs_secret_ref", "model_configs",
                               "(credential_source = 'env' AND (secret_ref IS NOT NULL OR credential_status <> 'active')) "
                               "OR (credential_source = 'direct' AND secret_ref IS NULL)")

    op.create_table(
        "model_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("model_config_id", sa.Uuid(), sa.ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("env_ref", sa.String(200), nullable=True),
        sa.Column("algorithm", sa.String(30), nullable=True),
        sa.Column("cipher_format", sa.Integer(), nullable=True),
        sa.Column("key_version", sa.String(32), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(12), server_default="active", nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rekeyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("model_config_id", "version", name="uq_model_credentials_version"),
        sa.CheckConstraint("source IN ('env', 'direct')", name="ck_model_credentials_source"),
        sa.CheckConstraint("status IN ('active', 'replaced', 'cleared', 'revoked')", name="ck_model_credentials_status"),
        sa.CheckConstraint("(source = 'env' AND env_ref IS NOT NULL AND ciphertext IS NULL) OR (source = 'direct' AND env_ref IS NULL)",
                           name="ck_model_credentials_payload"),
    )
    op.create_index("ix_model_credentials_model_config_id", "model_credentials", ["model_config_id"])
    op.create_table(
        "model_env_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("env_ref", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("env_ref", "fingerprint", name="uq_model_env_generations_fp"),
        sa.UniqueConstraint("env_ref", "generation", name="uq_model_env_generations_gen"),
    )
    op.create_table(
        "model_credential_meta",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint_salt", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Existing configs: their environment reference becomes credential version 1.
    op.execute(
        "INSERT INTO model_credentials (id, model_config_id, version, source, env_ref, status, created_at) "
        "SELECT gen_random_uuid(), id, 1, 'env', secret_ref, 'active', now() FROM model_configs WHERE secret_ref IS NOT NULL"
    )
    op.execute("UPDATE model_configs SET credential_version = 1 WHERE secret_ref IS NOT NULL")
    op.add_column("workflow_runs", sa.Column("model_snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "model_snapshot")
    # The checks go first: ck_model_configs_secret_ref forbids a reference on a direct-key config.
    op.drop_constraint("ck_model_configs_secret_ref", "model_configs", type_="check")
    op.drop_constraint("ck_model_configs_credential_status", "model_configs", type_="check")
    op.drop_constraint("ck_model_configs_credential_source", "model_configs", type_="check")
    # No plaintext fallback: direct keys are dropped with their table; such configs are disabled.
    op.execute(f"UPDATE model_configs SET secret_ref = '{PLACEHOLDER_REF}', enabled = false WHERE secret_ref IS NULL")
    op.drop_table("model_credential_meta")
    op.drop_table("model_env_generations")
    op.drop_index("ix_model_credentials_model_config_id", table_name="model_credentials")
    op.drop_table("model_credentials")
    op.alter_column("model_configs", "secret_ref", existing_type=sa.String(200), nullable=False)
    op.drop_column("model_configs", "config_version")
    op.drop_column("model_configs", "credential_version")
    op.drop_column("model_configs", "credential_status")
    op.drop_column("model_configs", "credential_source")
