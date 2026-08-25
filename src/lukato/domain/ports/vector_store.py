"""Porta de armazenamento e busca vetorial."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from lukato.domain.models.knowledge import Chunk, SearchHit
from lukato.domain.types import Id, Json

__all__ = ["VectorStorePort"]


class VectorStorePort(Protocol):
    """Contrato do indice vetorial usado pela base de conhecimento."""

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        """Grava ou atualiza os chunks (e seus embeddings); devolve quantos foram afetados."""
        ...

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int = 10,
        filters: Json | None = None,
    ) -> list[SearchHit]:
        """Busca os chunks mais proximos do vetor, do maior para o menor score."""
        ...

    async def delete(self, collection: str, *, document_id: Id | None = None) -> int:
        """Remove a colecao inteira ou apenas os chunks de um documento; devolve o total."""
        ...

    async def collections(self) -> list[str]:
        """Lista as colecoes existentes no indice."""
        ...
