"""Porta de geracao de embeddings vetoriais."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

__all__ = ["EmbeddingPort"]


@runtime_checkable
class EmbeddingPort(Protocol):
    """Contrato de um provedor de embeddings (rede ou fallback por hashing)."""

    @property
    def model(self) -> str:
        """Nome do modelo de embedding em uso."""
        ...

    @property
    def dimensions(self) -> int:
        """Dimensao dos vetores produzidos."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Gera um vetor para cada texto, preservando a ordem de entrada."""
        ...

    async def embed_one(self, text: str) -> list[float]:
        """Gera o vetor de um unico texto."""
        ...

    async def health(self) -> bool:
        """True quando o provedor responde a uma verificacao barata."""
        ...
