"""Escolha do adaptador de embeddings a partir de `Settings` (SPEC-0007, ADR-0003).

A decisao vem de `settings.embedding.effective_provider`. Como o modo degradado nao
tem qualidade semantica real, o log de selecao e explicito: quem le `/readyz`, o
console ou o log sabe se a busca esta rodando com Qwen3 ou com hashing local.
"""

from __future__ import annotations

from typing import Final

from lukato.adapters.embeddings.hashing import HashingEmbedder
from lukato.adapters.embeddings.qwen import QwenEmbedder
from lukato.config import Settings, get_logger
from lukato.domain.ports.embeddings import EmbeddingPort

__all__ = ["HASHING_REASONS", "build_embedder", "build_embedder_with_health"]

_logger = get_logger(__name__)

HASHING_REASONS: Final[dict[str, str]] = {
    "hashing": (
        "LUKATO_EMBEDDING__PROVIDER=hashing: o adaptador deterministico offline foi "
        "pedido explicitamente na configuracao"
    ),
    "missing_base_url": (
        "LUKATO_EMBEDDING__BASE_URL vazio: sem endpoint nao ha como chamar o hub Qwen3, "
        "entao o adaptador deterministico offline assume no lugar"
    ),
}
"""Motivos possiveis para a busca rodar com `HashingEmbedder`, prontos para log e UI."""


def _hashing_reason(settings: Settings) -> str:
    """Explica por que o hashing foi escolhido: pedido explicito ou falta de endpoint."""
    if settings.embedding.provider == "hashing":
        return HASHING_REASONS["hashing"]
    return HASHING_REASONS["missing_base_url"]


def build_embedder(settings: Settings) -> EmbeddingPort:
    """Constroi o adaptador de embeddings correspondente ao provedor efetivo."""
    if settings.embedding.effective_provider == "hashing":
        adapter = HashingEmbedder(settings)
        _logger.info(
            "embedding_adapter_selected",
            adapter=HashingEmbedder.provider,
            configured_provider=settings.embedding.provider,
            model=adapter.model,
            dimensions=adapter.dimensions,
            collection=settings.embedding.collection,
            offline=True,
            semantic_quality=False,
            reason=_hashing_reason(settings),
        )
        return adapter
    qwen = QwenEmbedder(settings)
    _logger.info(
        "embedding_adapter_selected",
        adapter=QwenEmbedder.provider,
        configured_provider=settings.embedding.provider,
        model=qwen.model,
        dimensions=qwen.dimensions,
        collection=settings.embedding.collection,
        endpoint=qwen.endpoint,
        offline=False,
        semantic_quality=True,
        reason="endpoint do hub Qwen3 configurado",
    )
    return qwen


async def build_embedder_with_health(settings: Settings) -> tuple[EmbeddingPort, bool]:
    """Constroi o adaptador e ja devolve o resultado do `health()`, sem levantar."""
    adapter = build_embedder(settings)
    try:
        healthy = await adapter.health()
    except Exception as exc:
        _logger.warning(
            "embedding_health_check_failed",
            adapter=type(adapter).__name__,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return adapter, False
    _logger.info("embedding_health_checked", adapter=type(adapter).__name__, healthy=healthy)
    return adapter, healthy
