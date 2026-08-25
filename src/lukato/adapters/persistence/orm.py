"""Mapeamento declarativo completo do lukato (SPEC-0011 secao 4, normativa).

As dezoito tabelas abaixo sao a unica fonte de verdade do esquema relacional; os
repositorios e as migracoes Alembic derivam desta metadata. Regras nao negociaveis:

* chave primaria `String(36)` com UUID em texto (`id_column()`);
* enums do dominio gravados como `String(32)` — nunca `ENUM` nativo;
* JSON via :data:`~lukato.adapters.persistence.types.JSONType` e vetores via
  :class:`~lukato.adapters.persistence.types.VectorType`;
* nenhum atributo de classe chamado `metadata`, `index`, `start` ou `end`:
  o mapeamento obrigatorio e `meta -> "metadata"`, `position -> "position"`,
  `start_seconds`/`end_seconds`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lukato.adapters.persistence.base import Base
from lukato.adapters.persistence.types import (
    DEFAULT_VECTOR_DIM,
    ID_LEN,
    JSONType,
    VectorType,
    id_column,
    utcnow_column,
)
from lukato.config import get_logger, get_settings

__all__ = [
    "VECTOR_DIM",
    "AdFingerprintRow",
    "AgentRunRow",
    "ApiKeyRow",
    "BudgetRow",
    "ChunkRow",
    "CommercialRow",
    "DetectionRow",
    "DocumentRow",
    "GuardrailPolicyRow",
    "MediaAssetRow",
    "ModuleRow",
    "OcrTextRow",
    "PromptRow",
    "RunStepRow",
    "SceneCutRow",
    "TranscriptRow",
    "UsageRecordRow",
    "UserRow",
]

_logger = get_logger(__name__)

ENUM_LEN: Final[int] = 32
"""Largura das colunas que guardam o valor textual de um `StrEnum` do dominio."""

SLUG_LEN: Final[int] = 128
NAME_LEN: Final[int] = 200
SHORT_LEN: Final[int] = 64
URI_LEN: Final[int] = 1024


def _resolve_vector_dim() -> int:
    """Le a dimensao do embedding em `Settings`; cai no padrao se a config falhar."""
    try:
        return int(get_settings().embedding.dimensions)
    except Exception as exc:  # configuracao invalida nao pode quebrar o import
        _logger.warning(
            "vector_dim_fallback", default=DEFAULT_VECTOR_DIM, error=str(exc)
        )
        return DEFAULT_VECTOR_DIM


VECTOR_DIM: Final[int] = _resolve_vector_dim()
"""Dimensionalidade das colunas de embedding, fixada na definicao das tabelas."""


# --------------------------------------------------------------------------- #
# Registry de modulos, prompts e guardrails
# --------------------------------------------------------------------------- #


class ModuleRow(Base):
    """Definicao persistida de um building block (`ModuleDefinition`)."""

    __tablename__ = "modules"

    id: Mapped[str] = id_column()
    slug: Mapped[str] = mapped_column(String(SLUG_LEN), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, index=True)
    runtime: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="langgraph")
    binding: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    tags: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    owner: Mapped[str | None] = mapped_column(String(NAME_LEN), nullable=True)
    version: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="1.0.0")
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


class PromptRow(Base):
    """Versao de um template de prompt (`PromptTemplate`); `slug`+`version` sao unicos."""

    __tablename__ = "prompts"
    __table_args__ = (UniqueConstraint("slug", "version"),)

    id: Mapped[str] = id_column()
    slug: Mapped[str] = mapped_column(String(SLUG_LEN), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, default="system")
    template: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    labels: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


class GuardrailPolicyRow(Base):
    """Politica de guardrail de entrada ou de saida (`GuardrailPolicy`)."""

    __tablename__ = "guardrail_policies"

    id: Mapped[str] = id_column()
    slug: Mapped[str] = mapped_column(String(SLUG_LEN), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, index=True)
    rules: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    fail_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


# --------------------------------------------------------------------------- #
# Runtime de agentes
# --------------------------------------------------------------------------- #


class AgentRunRow(Base):
    """Execucao completa de um building block (`AgentRun`)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_module_slug_created_at", "module_slug", "created_at"),
        Index("ix_agent_runs_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[str] = id_column()
    module_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    module_slug: Mapped[str] = mapped_column(String(SLUG_LEN), nullable=False)
    status: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trace_id: Mapped[str | None] = mapped_column(String(NAME_LEN), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="default")
    actor: Mapped[str | None] = mapped_column(String(NAME_LEN), nullable=True)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[RunStepRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RunStepRow.position",
    )


class RunStepRow(Base):
    """Passo individual registrado durante uma execucao (`RunStep`)."""

    __tablename__ = "run_steps"
    __table_args__ = (Index("ix_run_steps_run_id_position", "run_id", "position"),)

    id: Mapped[str] = id_column()
    run_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    status: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[AgentRunRow] = relationship(back_populates="steps")


# --------------------------------------------------------------------------- #
# FinOps
# --------------------------------------------------------------------------- #


class UsageRecordRow(Base):
    """Registro de consumo faturavel (`UsageRecord`)."""

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_occurred_at", "occurred_at"),
        Index("ix_usage_records_module_slug_occurred_at", "module_slug", "occurred_at"),
    )

    id: Mapped[str] = id_column()
    run_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    module_slug: Mapped[str] = mapped_column(String(SLUG_LEN), nullable=False)
    model: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tenant_id: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="default")
    occurred_at: Mapped[datetime] = utcnow_column()


class BudgetRow(Base):
    """Orcamento de custo por escopo (`Budget`)."""

    __tablename__ = "budgets"

    id: Mapped[str] = id_column()
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    scope: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    limit_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    period: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, default="monthly")
    alert_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    hard_stop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


# --------------------------------------------------------------------------- #
# Conhecimento
# --------------------------------------------------------------------------- #


class DocumentRow(Base):
    """Documento ingerido na base de conhecimento (`Document`)."""

    __tablename__ = "documents"

    id: Mapped[str] = id_column()
    collection: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    source: Mapped[str] = mapped_column(String(URI_LEN), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    checksum: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="", index=True)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    chunks: Mapped[list[ChunkRow]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChunkRow.position",
    )


class ChunkRow(Base):
    """Fragmento indexavel de um documento (`Chunk`), com embedding opcional."""

    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_document_id_position", "document_id", "position"),)

    id: Mapped[str] = id_column()
    document_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    collection: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(VECTOR_DIM), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document: Mapped[DocumentRow] = relationship(back_populates="chunks")


# --------------------------------------------------------------------------- #
# Identidade e acesso
# --------------------------------------------------------------------------- #


class UserRow(Base):
    """Usuario autenticavel da plataforma (`User`)."""

    __tablename__ = "users"

    id: Mapped[str] = id_column()
    email: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    role: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, default="viewer")
    password_hash: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tenant_id: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="default")
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


class ApiKeyRow(Base):
    """Chave de API: somente prefixo e hash do segredo sao persistidos (`ApiKey`)."""

    __tablename__ = "api_keys"

    id: Mapped[str] = id_column()
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    prefix: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, unique=True)
    hashed_secret: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    role: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, default="operator")
    tenant_id: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="default")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


# --------------------------------------------------------------------------- #
# AdWatch — catalogo
# --------------------------------------------------------------------------- #


class CommercialRow(Base):
    """Comercial catalogado (`Commercial`); `commercial_id` e o codigo de negocio."""

    __tablename__ = "commercials"

    id: Mapped[str] = id_column()
    commercial_id: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, unique=True)
    campaign: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    brand: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_expected: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    keywords: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    key_phrases: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    language: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="pt-BR")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    fingerprint: Mapped[AdFingerprintRow | None] = relationship(
        back_populates="commercial",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class AdFingerprintRow(Base):
    """Assinatura de matching derivada de um comercial (`AdFingerprint`)."""

    __tablename__ = "ad_fingerprints"

    id: Mapped[str] = id_column()
    commercial_id: Mapped[str] = mapped_column(
        String(ID_LEN),
        ForeignKey("commercials.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    token_set: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    keywords: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    key_phrases: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(VECTOR_DIM), nullable=True)
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    expected_brand: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    commercial: Mapped[CommercialRow] = relationship(back_populates="fingerprint")


# --------------------------------------------------------------------------- #
# AdWatch — midia e artefatos derivados
# --------------------------------------------------------------------------- #


class MediaAssetRow(Base):
    """Ativo de midia registrado para ingestao e analise (`MediaAsset`)."""

    __tablename__ = "media_assets"

    id: Mapped[str] = id_column()
    uri: Mapped[str] = mapped_column(String(URI_LEN), nullable=False)
    kind: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, default="video")
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    title: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(ENUM_LEN), nullable=False, default="registered", index=True
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    transcript: Mapped[TranscriptRow | None] = relationship(
        back_populates="media",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    scenes: Mapped[list[SceneCutRow]] = relationship(
        back_populates="media",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SceneCutRow.position",
    )
    ocr_texts: Mapped[list[OcrTextRow]] = relationship(
        back_populates="media",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OcrTextRow.start_seconds",
    )
    detections: Mapped[list[DetectionRow]] = relationship(
        back_populates="media",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DetectionRow.start_seconds",
    )


class TranscriptRow(Base):
    """Transcricao alinhada no tempo de um ativo de midia (`Transcript`)."""

    __tablename__ = "transcripts"

    id: Mapped[str] = id_column()
    media_id: Mapped[str] = mapped_column(
        String(ID_LEN),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    language: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="pt")
    words: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="import")
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    media: Mapped[MediaAssetRow] = relationship(back_populates="transcript")


class SceneCutRow(Base):
    """Corte de cena detectado no video (`SceneCut`)."""

    __tablename__ = "scene_cuts"
    __table_args__ = (Index("ix_scene_cuts_media_id_position", "media_id", "position"),)

    id: Mapped[str] = id_column()
    media_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    kind: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, default="cut")

    media: Mapped[MediaAssetRow] = relationship(back_populates="scenes")


class OcrTextRow(Base):
    """Texto reconhecido em quadros do video (`OcrText`)."""

    __tablename__ = "ocr_texts"
    __table_args__ = (Index("ix_ocr_texts_media_id_start_seconds", "media_id", "start_seconds"),)

    id: Mapped[str] = id_column()
    media_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    bbox: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)

    media: Mapped[MediaAssetRow] = relationship(back_populates="ocr_texts")


class DetectionRow(Base):
    """Deteccao consolidada de um comercial dentro de um ativo (`Detection`)."""

    __tablename__ = "detections"
    __table_args__ = (
        Index("ix_detections_media_id_start_seconds", "media_id", "start_seconds"),
    )

    id: Mapped[str] = id_column()
    media_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    commercial_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("commercials.id"), nullable=False, index=True
    )
    commercial_code: Mapped[str] = mapped_column(String(SHORT_LEN), nullable=False, default="")
    campaign: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    brand: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(ENUM_LEN), nullable=False, index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    refined_by_scene: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_by_vlm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    media: Mapped[MediaAssetRow] = relationship(back_populates="detections")
