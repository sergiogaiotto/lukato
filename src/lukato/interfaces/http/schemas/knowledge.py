"""Schemas do recurso `/api/v1/knowledge`: documentos, colecoes e busca semantica.

A identidade do embedder (provedor, modelo, dimensao) acompanha cada colecao: uma
colecao indexada por um embedder e consultada por outro devolveria vizinhos sem
sentido, entao a incompatibilidade e informada explicitamente.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from lukato.application.use_cases.knowledge import DocumentInput, IngestResult, SearchQuery
from lukato.domain.models.knowledge import Document, SearchHit
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import InSchema, OutSchema

__all__ = [
    "CollectionOut",
    "DocumentCreate",
    "DocumentOut",
    "EmbeddingInfo",
    "IngestResponse",
    "KnowledgeHealthOut",
    "SearchHitOut",
    "SearchRequest",
    "SearchResponse",
]


class EmbeddingInfo(OutSchema):
    """Identidade do embedder que produziu (ou consultara) uma colecao."""

    provider: str = Field(default="", description="Provedor de embeddings.")
    model: str = Field(default="", description="Modelo usado.")
    dimensions: int = Field(default=0, ge=0, description="Dimensao do vetor.")

    @classmethod
    def from_result(cls, payload: Json | None) -> EmbeddingInfo:
        """Converte o mapa devolvido por `EmbeddingFingerprint.to_dict`."""
        data = payload or {}
        return cls(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            dimensions=int(data.get("dimensions", 0) or 0),
        )


class DocumentCreate(InSchema):
    """Corpo de `POST /api/v1/knowledge/documents`."""

    title: str = Field(min_length=1, description="Titulo do documento.")
    content: str = Field(min_length=1, description="Texto integral a indexar.")
    collection: str = Field(default="", description="Colecao de destino; ausente usa a padrao.")
    source: str = Field(default="", description="Origem do documento (URL, arquivo, sistema).")
    metadata: Json = Field(default_factory=dict, description="Metadados livres do documento.")
    document_id: Id | None = Field(
        default=None, description="Id para reingestao idempotente do mesmo documento."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Politica de cobranca",
                "content": "A fatura vence no dia 10 de cada mes...",
                "collection": "agente_evidence",
                "source": "intranet/politicas/cobranca.md",
                "metadata": {"area": "financeiro"},
            }
        },
    )

    def to_input(self) -> DocumentInput:
        """Converte para o DTO do caso de uso `IngestDocument`."""
        return DocumentInput(
            title=self.title,
            content=self.content,
            collection=self.collection,
            source=self.source,
            metadata=dict(self.metadata),
            document_id=self.document_id,
        )


class DocumentOut(OutSchema):
    """Documento indexado, sem os chunks nem os vetores."""

    id: Id
    collection: str
    title: str
    source: str = ""
    content: str
    metadata: Json = Field(default_factory=dict)
    checksum: str = Field(default="", description="Impressao do conteudo, base da idempotencia.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, document: Document) -> DocumentOut:
        """Converte a entidade de dominio."""
        return cls(
            id=document.id,
            collection=document.collection,
            title=document.title,
            source=document.source,
            content=document.content,
            metadata=dict(document.metadata),
            checksum=document.checksum,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class IngestResponse(OutSchema):
    """Resultado de uma ingestao ou reindexacao."""

    document: DocumentOut
    chunks: int = Field(default=0, ge=0, description="Fragmentos gerados.")
    embedded: bool = Field(default=False, description="False quando nada precisou ser reembeddado.")
    reindexed: bool = Field(default=False, description="True quando o indice foi refeito.")
    idempotent: bool = Field(
        default=False, description="True quando o conteudo ja estava indexado."
    )
    embedding: EmbeddingInfo = Field(
        default_factory=EmbeddingInfo, description="Embedder usado nesta ingestao."
    )

    @classmethod
    def from_result(cls, result: IngestResult) -> IngestResponse:
        """Converte o DTO do caso de uso `IngestDocument`."""
        return cls(
            document=DocumentOut.from_domain(result.document),
            chunks=result.chunks,
            embedded=result.embedded,
            reindexed=result.reindexed,
            idempotent=result.idempotent,
            embedding=EmbeddingInfo.from_result(result.fingerprint.to_dict()),
        )


class SearchRequest(InSchema):
    """Corpo de `POST /api/v1/knowledge/search`."""

    query: str = Field(min_length=1, description="Texto da consulta.")
    collection: str = Field(default="", description="Colecao a consultar; ausente usa a padrao.")
    limit: int = Field(default=5, ge=1, le=100, description="Quantidade maxima de trechos.")
    filters: Json = Field(default_factory=dict, description="Filtros por metadado do chunk.")
    rerank: bool = Field(
        default=False, description="Combina o score vetorial com similaridade lexical."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "query": "quando vence a fatura?",
                "collection": "agente_evidence",
                "limit": 5,
                "filters": {"area": "financeiro"},
                "rerank": True,
            }
        },
    )

    def to_query(self) -> SearchQuery:
        """Converte para o DTO do caso de uso `SearchKnowledge`."""
        return SearchQuery(
            query=self.query,
            collection=self.collection,
            limit=self.limit,
            filters=dict(self.filters),
            rerank=self.rerank,
        )


class SearchHitOut(OutSchema):
    """Trecho recuperado pela busca semantica."""

    chunk_id: Id
    document_id: Id
    collection: str
    content: str
    score: float = Field(description="Relevancia do trecho, de 0 a 1.")
    metadata: Json = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, hit: SearchHit) -> SearchHitOut:
        """Converte o resultado de dominio."""
        return cls(
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            collection=hit.collection,
            content=hit.content,
            score=hit.score,
            metadata=dict(hit.metadata),
        )


class SearchResponse(OutSchema):
    """Resposta da busca: os trechos e o recorte consultado."""

    query: str = Field(description="Consulta exatamente como enviada.")
    collection: str = Field(default="", description="Colecao efetivamente consultada.")
    hits: list[SearchHitOut] = Field(
        default_factory=list, description="Trechos, do mais relevante."
    )
    total: int = Field(default=0, ge=0, description="Quantidade de trechos devolvidos.")
    reranked: bool = Field(default=False, description="Se houve reordenacao lexical.")

    @classmethod
    def of(
        cls, query: str, collection: str, hits: list[SearchHit], *, reranked: bool = False
    ) -> SearchResponse:
        """Monta a resposta a partir dos resultados de dominio."""
        items = [SearchHitOut.from_domain(hit) for hit in hits]
        return cls(
            query=query, collection=collection, hits=items, total=len(items), reranked=reranked
        )


class CollectionOut(OutSchema):
    """Colecao da base, com a identidade do embedder que a produziu."""

    name: str = Field(description="Nome da colecao.")
    documents: int = Field(default=0, ge=0, description="Documentos indexados.")
    is_default: bool = Field(default=False, description="Se e a colecao padrao da instalacao.")
    embedding: EmbeddingInfo = Field(default_factory=EmbeddingInfo)
    indexed: bool = Field(default=False, description="False quando a colecao ainda esta vazia.")
    compatible: bool = Field(
        default=True, description="False quando outro embedder produziu esta colecao."
    )

    @classmethod
    def from_result(cls, entry: Json) -> CollectionOut:
        """Converte uma entrada de `ListCollections`."""
        return cls(
            name=str(entry.get("name", "")),
            documents=int(entry.get("documents", 0) or 0),
            is_default=bool(entry.get("is_default", False)),
            embedding=EmbeddingInfo.from_result(entry.get("embedding")),
            indexed=bool(entry.get("indexed", False)),
            compatible=bool(entry.get("compatible", True)),
        )


class KnowledgeHealthOut(OutSchema):
    """Resposta de `GET /api/v1/knowledge/health` (SPEC-0007 secao 4)."""

    provider: str = Field(default="", description="Provedor de embeddings em uso.")
    model: str = Field(default="", description="Modelo em uso.")
    dimensions: int = Field(default=0, ge=0, description="Dimensao do vetor em uso.")
    degraded: bool = Field(default=False, description="True no modo hashing ou provedor fora.")
    healthy: bool = Field(default=True, description="Resultado da sonda do provedor.")
    configured_provider: str = Field(default="", description="Provedor pedido na configuracao.")
    default_collection: str = Field(default="", description="Colecao padrao da instalacao.")
    batch_size: int = Field(default=0, ge=0, description="Tamanho do lote de embedding.")
    chunk_size: int = Field(default=0, ge=0, description="Tamanho do fragmento em caracteres.")
    chunk_overlap: int = Field(default=0, ge=0, description="Sobreposicao entre fragmentos.")
    reason: str = Field(default="", description="Por que a base esta degradada, quando estiver.")
    collections: list[CollectionOut] = Field(
        default_factory=list, description="Colecoes ja indexadas e sua compatibilidade."
    )

    @classmethod
    def from_result(cls, report: Json) -> KnowledgeHealthOut:
        """Converte o mapa devolvido pelo caso de uso `KnowledgeHealth`."""
        return cls(
            provider=str(report.get("provider", "")),
            model=str(report.get("model", "")),
            dimensions=int(report.get("dimensions", 0) or 0),
            degraded=bool(report.get("degraded", False)),
            healthy=bool(report.get("healthy", True)),
            configured_provider=str(report.get("configured_provider", "")),
            default_collection=str(report.get("default_collection", "")),
            batch_size=int(report.get("batch_size", 0) or 0),
            chunk_size=int(report.get("chunk_size", 0) or 0),
            chunk_overlap=int(report.get("chunk_overlap", 0) or 0),
            reason=str(report.get("reason") or ""),
            collections=[
                CollectionOut.from_result(item) for item in report.get("collections") or []
            ],
        )
