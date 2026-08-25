"""Tipos de coluna cross-dialect usados por todo o esquema relacional (SPEC-0011 §3).

O mesmo mapeamento declarativo precisa rodar em **PostgreSQL 16 + pgvector** (producao)
e em **SQLite/aiosqlite** (desenvolvimento, testes e modo offline). Por isso:

* JSON vira `JSONB` no PostgreSQL e `JSON` nos demais dialetos;
* vetores viram `vector(dim)` no PostgreSQL e uma lista JSON nos demais;
* chaves primarias sao sempre `String(36)` com UUID em texto — nunca `UUID` nativo.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import MappedColumn, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

from lukato.domain.types import utcnow

__all__ = [
    "DEFAULT_VECTOR_DIM",
    "ID_LEN",
    "POSTGRES_DIALECT",
    "JSONType",
    "VectorType",
    "id_column",
    "utcnow_column",
]

ID_LEN: Final[int] = 36
"""Tamanho da coluna de identificador: UUID4 em hex-dash (`str(uuid.uuid4())`)."""

DEFAULT_VECTOR_DIM: Final[int] = 1024
"""Dimensao padrao de embedding quando `Settings` nao esta disponivel."""

POSTGRES_DIALECT: Final[str] = "postgresql"
"""Nome do dialeto SQLAlchemy que habilita `JSONB` e `pgvector`."""

JSONType: Final[TypeEngine[Any]] = JSON().with_variant(JSONB, POSTGRES_DIALECT)
"""Coluna JSON portatil: `JSONB` no PostgreSQL, `JSON` em SQLite e demais dialetos."""


class VectorType(TypeDecorator[list[float]]):
    """Coluna de embedding portatil: `vector(dim)` no PostgreSQL, `JSON` nos demais.

    No PostgreSQL a coluna aceita os operadores de distancia do pgvector; em SQLite os
    valores sao gravados como lista JSON e a similaridade e calculada em memoria.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int = DEFAULT_VECTOR_DIM) -> None:
        """Cria o tipo com a dimensionalidade fixa do vetor."""
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Escolhe `pgvector.Vector` no PostgreSQL e `JSON` em qualquer outro dialeto."""
        if dialect.name == POSTGRES_DIALECT:
            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(
        self, value: Sequence[float] | None, dialect: Dialect
    ) -> list[float] | None:
        """Normaliza o vetor para uma lista de floats antes de gravar."""
        if value is None:
            return None
        return [float(item) for item in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        """Converte o valor lido (lista JSON ou `numpy.ndarray`) em `list[float]`."""
        if value is None:
            return None
        if hasattr(value, "tolist"):
            value = value.tolist()
        return [float(item) for item in value]

    def __repr__(self) -> str:
        return f"VectorType(dim={self.dim})"


def id_column() -> MappedColumn[str]:
    """Coluna de chave primaria: UUID em texto, `String(36)`."""
    return mapped_column(String(ID_LEN), primary_key=True)


def utcnow_column(onupdate: bool = False) -> MappedColumn[datetime]:
    """Coluna `DateTime(timezone=True)` em UTC; com `onupdate` renova a cada escrita."""
    if onupdate:
        return mapped_column(
            DateTime(timezone=True),
            nullable=False,
            default=utcnow,
            onupdate=utcnow,
        )
    return mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
