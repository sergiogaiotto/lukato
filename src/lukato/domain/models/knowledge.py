"""Modelos de base de conhecimento: documentos, chunks e resultados de busca."""

from __future__ import annotations

from pydantic import Field

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.types import Id, Json, new_id

__all__ = ["Chunk", "Document", "SearchHit"]


class Document(Entity):
    """Documento ingerido em uma colecao da base de conhecimento."""

    collection: str
    title: str
    source: str = ""
    content: str
    metadata: Json = Field(default_factory=dict)
    checksum: str = ""


class Chunk(DomainModel):
    """Fragmento indexavel de um documento, opcionalmente com embedding."""

    id: Id = Field(default_factory=new_id)
    document_id: Id
    collection: str
    index: int
    content: str
    metadata: Json = Field(default_factory=dict)
    embedding: list[float] | None = None
    token_count: int = 0


class SearchHit(DomainModel):
    """Resultado de busca semantica sobre a base de conhecimento."""

    chunk_id: Id
    document_id: Id
    collection: str
    content: str
    score: float
    metadata: Json = Field(default_factory=dict)
