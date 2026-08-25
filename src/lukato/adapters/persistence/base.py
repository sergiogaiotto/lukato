"""Base declarativa unica do esquema relacional do lukato (SPEC-0011 §3.1)."""

from __future__ import annotations

from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["NAMING", "Base", "metadata"]

NAMING: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Convencao de nomes de constraints, obrigatoria para migracoes deterministicas."""


class Base(DeclarativeBase):
    """Base declarativa compartilhada por todas as tabelas do lukato."""

    metadata = MetaData(naming_convention=NAMING)


metadata: Final[MetaData] = Base.metadata
"""Atalho para `Base.metadata`, usado pelo Alembic (`target_metadata`)."""
