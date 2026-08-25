"""Adaptador de persistencia do lukato: PostgreSQL 16 + pgvector com fallback SQLite.

Reexporta a base declarativa, as dezoito tabelas do esquema (SPEC-0011 secao 4), os
helpers de engine/sessao e a unidade de trabalho. Importar este pacote registra toda
a metadata — e o que o Alembic e `create_all` consomem — sem abrir conexao alguma.
"""

from __future__ import annotations

from lukato.adapters.persistence.base import NAMING, Base, metadata
from lukato.adapters.persistence.orm import (
    VECTOR_DIM,
    AdFingerprintRow,
    AgentRunRow,
    ApiKeyRow,
    BudgetRow,
    ChunkRow,
    CommercialRow,
    DetectionRow,
    DocumentRow,
    GuardrailPolicyRow,
    MediaAssetRow,
    ModuleRow,
    OcrTextRow,
    PromptRow,
    RunStepRow,
    SceneCutRow,
    TranscriptRow,
    UsageRecordRow,
    UserRow,
)
from lukato.adapters.persistence.session import (
    build_engine,
    build_sessionmaker,
    create_all,
    dispose_engine,
    ensure_pgvector,
    is_postgres,
    is_sqlite,
    ping,
    resolve_engine,
)
from lukato.adapters.persistence.types import (
    DEFAULT_VECTOR_DIM,
    ID_LEN,
    JSONType,
    VectorType,
    id_column,
    utcnow_column,
)
from lukato.adapters.persistence.uow import (
    REPOSITORY_ATTRS,
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactoryImpl,
)

__all__ = [
    "DEFAULT_VECTOR_DIM",
    "ID_LEN",
    "NAMING",
    "REPOSITORY_ATTRS",
    "VECTOR_DIM",
    "AdFingerprintRow",
    "AgentRunRow",
    "ApiKeyRow",
    "Base",
    "BudgetRow",
    "ChunkRow",
    "CommercialRow",
    "DetectionRow",
    "DocumentRow",
    "GuardrailPolicyRow",
    "JSONType",
    "MediaAssetRow",
    "ModuleRow",
    "OcrTextRow",
    "PromptRow",
    "RunStepRow",
    "SceneCutRow",
    "SqlAlchemyUnitOfWork",
    "TranscriptRow",
    "UnitOfWorkFactoryImpl",
    "UsageRecordRow",
    "UserRow",
    "VectorType",
    "build_engine",
    "build_sessionmaker",
    "create_all",
    "dispose_engine",
    "ensure_pgvector",
    "id_column",
    "is_postgres",
    "is_sqlite",
    "metadata",
    "ping",
    "resolve_engine",
    "utcnow_column",
]
