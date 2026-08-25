"""Bases comuns dos modelos de dominio (pydantic v2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from lukato.domain.types import Id, new_id, utcnow

__all__ = ["DomainModel", "Entity"]


class DomainModel(BaseModel):
    """Modelo de dominio puro: proibe campos extras e preserva enums."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=False,
        use_enum_values=False,
    )


class Entity(DomainModel):
    """Entidade persistida: identidade propria e carimbos de criacao/atualizacao."""

    id: Id = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        """Marca a entidade como alterada agora (UTC)."""
        self.updated_at = utcnow()
