"""esquema inicial

Revision ID: 0001
Revises:
Create Date: 2026-08-25 01:51:17.627290+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from lukato.adapters.persistence.types import JSONType, VectorType

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """True quando a migracao roda sobre PostgreSQL."""
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # A extensao vem ANTES de qualquer tabela: `chunks` e `ad_fingerprints`
    # declaram colunas VECTOR(1024), e sem a extensao o CREATE TABLE falha com
    # 'type "vector" does not exist'. Criar a extensao so na 0002 funcionava
    # apenas em bancos onde alguem ja a tinha criado a mao — num PostgreSQL
    # limpo, a 0001 morria antes de a 0002 existir.
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("module_id", sa.String(length=36), nullable=False),
        sa.Column("module_slug", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input", JSONType, nullable=False),
        sa.Column("output", JSONType, nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("trace_id", sa.String(length=200), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_runs_module_slug_created_at", ["module_slug", "created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_agent_runs_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_agent_runs_tenant_id_created_at", ["tenant_id", "created_at"], unique=False
        )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("prefix", sa.String(length=64), nullable=False),
        sa.Column("hashed_secret", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("limit_usd", sa.Float(), nullable=False),
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("alert_threshold", sa.Float(), nullable=False),
        sa.Column("hard_stop", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
    )
    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_budgets_scope"), ["scope"], unique=False)

    op.create_table(
        "commercials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("commercial_id", sa.String(length=64), nullable=False),
        sa.Column("campaign", sa.String(length=200), nullable=False),
        sa.Column("brand", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("duration_expected", sa.Float(), nullable=False),
        sa.Column("keywords", JSONType, nullable=False),
        sa.Column("key_phrases", JSONType, nullable=False),
        sa.Column("language", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercials")),
        sa.UniqueConstraint("commercial_id", name=op.f("uq_commercials_commercial_id")),
    )
    with op.batch_alter_table("commercials", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_commercials_brand"), ["brand"], unique=False)
        batch_op.create_index(batch_op.f("ix_commercials_campaign"), ["campaign"], unique=False)
        batch_op.create_index(batch_op.f("ix_commercials_is_active"), ["is_active"], unique=False)

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collection", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("checksum", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_documents_checksum"), ["checksum"], unique=False)
        batch_op.create_index(batch_op.f("ix_documents_collection"), ["collection"], unique=False)

    op.create_table(
        "guardrail_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("rules", JSONType, nullable=False),
        sa.Column("fail_open", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guardrail_policies")),
        sa.UniqueConstraint("slug", name=op.f("uq_guardrail_policies_slug")),
    )
    with op.batch_alter_table("guardrail_policies", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_guardrail_policies_stage"), ["stage"], unique=False)

    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("uri", sa.String(length=1024), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
    )
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_assets_status"), ["status"], unique=False)

    op.create_table(
        "modules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("runtime", sa.String(length=64), nullable=False),
        sa.Column("binding", JSONType, nullable=False),
        sa.Column("config", JSONType, nullable=False),
        sa.Column("tags", JSONType, nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_modules")),
        sa.UniqueConstraint("slug", name=op.f("uq_modules_slug")),
    )
    with op.batch_alter_table("modules", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_modules_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_modules_status"), ["status"], unique=False)

    op.create_table(
        "prompts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("variables", JSONType, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("labels", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompts")),
        sa.UniqueConstraint("slug", "version", name=op.f("uq_prompts_slug")),
    )
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_prompts_slug"), ["slug"], unique=False)

    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("module_slug", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_records")),
    )
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_usage_records_model"), ["model"], unique=False)
        batch_op.create_index(
            "ix_usage_records_module_slug_occurred_at", ["module_slug", "occurred_at"], unique=False
        )
        batch_op.create_index("ix_usage_records_occurred_at", ["occurred_at"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "ad_fingerprints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("commercial_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("token_set", JSONType, nullable=False),
        sa.Column("keywords", JSONType, nullable=False),
        sa.Column("key_phrases", JSONType, nullable=False),
        sa.Column("embedding", VectorType(dim=1024), nullable=True),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("expected_brand", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["commercial_id"],
            ["commercials.id"],
            name=op.f("fk_ad_fingerprints_commercial_id_commercials"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ad_fingerprints")),
        sa.UniqueConstraint("commercial_id", name=op.f("uq_ad_fingerprints_commercial_id")),
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("collection", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("embedding", VectorType(dim=1024), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
    )
    with op.batch_alter_table("chunks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chunks_collection"), ["collection"], unique=False)
        batch_op.create_index(
            "ix_chunks_document_id_position", ["document_id", "position"], unique=False
        )

    op.create_table(
        "detections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_id", sa.String(length=36), nullable=False),
        sa.Column("commercial_id", sa.String(length=36), nullable=False),
        sa.Column("commercial_code", sa.String(length=64), nullable=False),
        sa.Column("campaign", sa.String(length=200), nullable=False),
        sa.Column("brand", sa.String(length=200), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evidence", JSONType, nullable=False),
        sa.Column("refined_by_scene", sa.Boolean(), nullable=False),
        sa.Column("verified_by_vlm", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["commercial_id"],
            ["commercials.id"],
            name=op.f("fk_detections_commercial_id_commercials"),
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_assets.id"],
            name=op.f("fk_detections_media_id_media_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detections")),
    )
    with op.batch_alter_table("detections", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_detections_commercial_id"), ["commercial_id"], unique=False
        )
        batch_op.create_index(
            "ix_detections_media_id_start_seconds", ["media_id", "start_seconds"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_detections_status"), ["status"], unique=False)

    op.create_table(
        "ocr_texts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("bbox", JSONType, nullable=True),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_assets.id"],
            name=op.f("fk_ocr_texts_media_id_media_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ocr_texts")),
    )
    with op.batch_alter_table("ocr_texts", schema=None) as batch_op:
        batch_op.create_index(
            "ix_ocr_texts_media_id_start_seconds", ["media_id", "start_seconds"], unique=False
        )

    op.create_table(
        "run_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input", JSONType, nullable=False),
        sa.Column("output", JSONType, nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_run_steps_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_steps")),
    )
    with op.batch_alter_table("run_steps", schema=None) as batch_op:
        batch_op.create_index("ix_run_steps_run_id_position", ["run_id", "position"], unique=False)

    op.create_table(
        "scene_cuts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_assets.id"],
            name=op.f("fk_scene_cuts_media_id_media_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scene_cuts")),
    )
    with op.batch_alter_table("scene_cuts", schema=None) as batch_op:
        batch_op.create_index(
            "ix_scene_cuts_media_id_position", ["media_id", "position"], unique=False
        )

    op.create_table(
        "transcripts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_id", sa.String(length=36), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=False),
        sa.Column("words", JSONType, nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_assets.id"],
            name=op.f("fk_transcripts_media_id_media_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcripts")),
        sa.UniqueConstraint("media_id", name=op.f("uq_transcripts_media_id")),
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("transcripts")
    with op.batch_alter_table("scene_cuts", schema=None) as batch_op:
        batch_op.drop_index("ix_scene_cuts_media_id_position")

    op.drop_table("scene_cuts")
    with op.batch_alter_table("run_steps", schema=None) as batch_op:
        batch_op.drop_index("ix_run_steps_run_id_position")

    op.drop_table("run_steps")
    with op.batch_alter_table("ocr_texts", schema=None) as batch_op:
        batch_op.drop_index("ix_ocr_texts_media_id_start_seconds")

    op.drop_table("ocr_texts")
    with op.batch_alter_table("detections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_detections_status"))
        batch_op.drop_index("ix_detections_media_id_start_seconds")
        batch_op.drop_index(batch_op.f("ix_detections_commercial_id"))

    op.drop_table("detections")
    with op.batch_alter_table("chunks", schema=None) as batch_op:
        batch_op.drop_index("ix_chunks_document_id_position")
        batch_op.drop_index(batch_op.f("ix_chunks_collection"))

    op.drop_table("chunks")
    op.drop_table("ad_fingerprints")
    op.drop_table("users")
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_index("ix_usage_records_occurred_at")
        batch_op.drop_index("ix_usage_records_module_slug_occurred_at")
        batch_op.drop_index(batch_op.f("ix_usage_records_model"))

    op.drop_table("usage_records")
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompts_slug"))

    op.drop_table("prompts")
    with op.batch_alter_table("modules", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_modules_status"))
        batch_op.drop_index(batch_op.f("ix_modules_kind"))

    op.drop_table("modules")
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_assets_status"))

    op.drop_table("media_assets")
    with op.batch_alter_table("guardrail_policies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_guardrail_policies_stage"))

    op.drop_table("guardrail_policies")
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_documents_collection"))
        batch_op.drop_index(batch_op.f("ix_documents_checksum"))

    op.drop_table("documents")
    with op.batch_alter_table("commercials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_commercials_is_active"))
        batch_op.drop_index(batch_op.f("ix_commercials_campaign"))
        batch_op.drop_index(batch_op.f("ix_commercials_brand"))

    op.drop_table("commercials")
    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_budgets_scope"))

    op.drop_table("budgets")
    op.drop_table("api_keys")
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_runs_tenant_id_created_at")
        batch_op.drop_index(batch_op.f("ix_agent_runs_status"))
        batch_op.drop_index("ix_agent_runs_module_slug_created_at")

    op.drop_table("agent_runs")
    # ### end Alembic commands ###
