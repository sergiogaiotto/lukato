"""Casos de uso da base de conhecimento: ingestao, indexacao e busca semantica.

O caminho normativo da SPEC-0007 secao 2 e sempre o mesmo:

```text
Document -> normalizacao -> chunking -> embeddings (lote) -> vector store
```

Duas regras deste modulo merecem destaque porque protegem dados que ja estao
gravados:

* **Idempotencia por checksum.** O `checksum` e o SHA-256 do conteudo
  normalizado. Reingerir o mesmo conteudo atualiza apenas os metadados do
  documento: nenhum chunk novo e criado e nenhum embedding e pedido ao provedor.
* **Guarda de compatibilidade da colecao** (SPEC-0007 secao 1.2). Cada chunk
  carrega `embedding_provider`, `embedding_model` e `embedding_dimensions`.
  Gravar ou buscar em uma colecao produzida por outro embedder e recusado com
  :class:`ValidationError`, porque misturar espacos semanticos diferentes na
  mesma colecao nao gera erro em lugar nenhum — apenas devolve resultados
  errados, para sempre, sem sinal.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

from lukato.application.container import Container
from lukato.application.dto import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import NotFoundError, ValidationError
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.knowledge import Chunk, Document, SearchHit
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.services.matching import LexicalMatcher
from lukato.domain.types import Id, Json, new_id, utcnow

__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SEPARATORS",
    "CHUNK_SIZE",
    "DEFAULT_SEARCH_LIMIT",
    "EMBEDDING_DIMENSIONS_KEY",
    "EMBEDDING_MODEL_KEY",
    "EMBEDDING_PROVIDER_KEY",
    "HASHING_PROVIDER",
    "MAX_SEARCH_LIMIT",
    "RERANK_LEXICAL_WEIGHT",
    "RERANK_VECTOR_WEIGHT",
    "DeleteDocument",
    "DocumentFilter",
    "DocumentInput",
    "EmbeddingFingerprint",
    "GetDocument",
    "IngestDocument",
    "IngestResult",
    "KnowledgeHealth",
    "ListCollections",
    "ListDocuments",
    "ReindexDocument",
    "SearchKnowledge",
    "SearchQuery",
    "chunk_text",
    "content_checksum",
    "normalize_content",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constantes normativas
# ---------------------------------------------------------------------------
CHUNK_SIZE: Final[int] = 1200
"""Tamanho maximo de um chunk, em caracteres (SPEC-0007 secao 2)."""

CHUNK_OVERLAP: Final[int] = 200
"""Sobreposicao entre chunks consecutivos, em caracteres (SPEC-0007 secao 2)."""

CHUNK_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n", ". ", " ")
"""Pontos de quebra preferenciais, do mais forte para o mais fraco."""

MIN_CHUNK_FILL: Final[float] = 0.5
"""Fracao minima da janela que um chunk deve ocupar antes de aceitar uma quebra.

Sem esse piso, um separador logo no inicio da janela produziria chunks
minusculos e a sobreposicao perderia sentido.
"""

CHARS_PER_TOKEN: Final[int] = 4
"""Heuristica de contagem de tokens por chunk (mesma razao usada em FinOps)."""

DEFAULT_SEARCH_LIMIT: Final[int] = 10
"""Quantidade de resultados devolvidos por uma busca sem `limit` explicito."""

MAX_SEARCH_LIMIT: Final[int] = 100
"""Teto de resultados por busca: protege o indice de pedidos abusivos."""

RERANK_CANDIDATE_FACTOR: Final[int] = 3
"""Quantos candidatos a mais o rerank busca antes de reordenar."""

RERANK_VECTOR_WEIGHT: Final[float] = 0.6
"""Peso do score vetorial na combinacao do rerank."""

RERANK_LEXICAL_WEIGHT: Final[float] = 0.4
"""Peso da similaridade lexica na combinacao do rerank."""

SCORE_DIGITS: Final[int] = 6
"""Casas decimais dos scores devolvidos ao chamador."""

EMBEDDING_PROVIDER_KEY: Final[str] = "embedding_provider"
EMBEDDING_MODEL_KEY: Final[str] = "embedding_model"
EMBEDDING_DIMENSIONS_KEY: Final[str] = "embedding_dimensions"
"""Chaves de `metadata` que identificam quem produziu cada chunk."""

HASHING_PROVIDER: Final[str] = "hashing"
"""Provedor de embeddings deterministico offline; sempre reportado como degradado."""

COLLECTION_PROBE_DOCUMENTS: Final[int] = 3
"""Documentos inspecionados para descobrir o embedder que produziu uma colecao."""

MAX_COLLECTION_DOCUMENTS: Final[int] = 1_000_000
"""Teto defensivo da contagem de documentos reportada por colecao."""

_CHUNK_NAMESPACE: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://lukato.local/knowledge/chunks"
)
"""Namespace dos identificadores deterministicos de chunk (`documento:indice`)."""

_LINE_ENDINGS = re.compile(r"\r\n?")
_TRAILING_SPACES = re.compile(r"[ \t]+(?=\n)")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Funcoes puras de normalizacao e chunking
# ---------------------------------------------------------------------------
def normalize_content(text: str) -> str:
    """Normaliza o conteudo antes do checksum e do chunking.

    Aplica NFC, uniformiza fins de linha, remove espacos no fim de cada linha e
    colapsa sequencias de linhas em branco. Preserva pontuacao e caixa: o
    documento continua legivel, apenas deixa de variar por detalhe invisivel.
    """
    folded = unicodedata.normalize("NFC", text)
    unified = _LINE_ENDINGS.sub("\n", folded)
    trimmed = _TRAILING_SPACES.sub("", unified)
    return _EXCESS_BLANK_LINES.sub("\n\n", trimmed).strip()


def content_checksum(text: str) -> str:
    """SHA-256 (hex) do conteudo ja normalizado (SPEC-0007 secao 2)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Estima os tokens de um trecho por `len(texto)/4`."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _best_cut(text: str, start: int, end: int, floor: int, separators: Sequence[str]) -> int:
    """Indice de corte da janela `[start, end)`, preferindo o separador mais forte.

    Procura a ultima ocorrencia de cada separador dentro de `[floor, end)` e
    corta logo depois dela; sem nenhum separador aproveitavel, corta em `end`.
    """
    for separator in separators:
        if not separator:
            continue
        found = text.rfind(separator, floor, end)
        if found >= floor:
            return found + len(separator)
    return end


def chunk_text(
    text: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    separators: Sequence[str] = CHUNK_SEPARATORS,
) -> list[str]:
    """Divide o texto em janelas de ate `chunk_size` com `overlap` de sobreposicao.

    A quebra procura o separador mais forte disponivel (`\\n\\n`, `\\n`, `". "`,
    espaco) na segunda metade da janela; sem nenhum deles o corte e feito no
    limite exato. O ultimo trecho e descartado quando ja cabe inteiro dentro da
    sobreposicao do chunk anterior, o que evitaria um fragmento redundante.
    """
    if chunk_size <= 0:
        raise ValidationError(
            "O tamanho do chunk deve ser positivo.",
            details={"chunk_size": chunk_size},
        )
    if overlap < 0 or overlap > chunk_size // 2:
        raise ValidationError(
            "A sobreposicao deve ficar entre 0 e metade do tamanho do chunk.",
            details={"chunk_size": chunk_size, "overlap": overlap},
        )

    body = text.strip()
    if not body:
        return []
    total = len(body)
    if total <= chunk_size:
        return [body]

    minimum_fill = max(1, int(chunk_size * MIN_CHUNK_FILL), overlap + 1)
    chunks: list[str] = []
    start = 0
    while start < total:
        end = start + chunk_size
        if end >= total:
            if not chunks or (total - start) > overlap:
                tail = body[start:].strip()
                if tail:
                    chunks.append(tail)
            break
        cut = _best_cut(body, start, end, start + minimum_fill, separators)
        piece = body[start:cut].strip()
        if piece:
            chunks.append(piece)
        start = max(cut - overlap, start + 1)
    return chunks


@lru_cache(maxsize=1)
def _lexical_matcher() -> LexicalMatcher:
    """Instancia unica do comparador lexico usado pelo rerank."""
    return LexicalMatcher()


def _clamp_score(value: float) -> float:
    """Mantem um score dentro de `[0, 1]` e com casas decimais estaveis."""
    return round(min(1.0, max(0.0, float(value))), SCORE_DIGITS)


def _clamp_limit(limit: int, *, ceiling: int) -> int:
    """Mantem um limite dentro de `1..ceiling`."""
    return max(1, min(int(limit), ceiling))


# ---------------------------------------------------------------------------
# Identidade do embedder que produziu uma colecao
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EmbeddingFingerprint:
    """Provedor, modelo e dimensao que produziram (ou produzirao) uma colecao."""

    provider: str
    model: str
    dimensions: int

    def as_metadata(self) -> Json:
        """Chaves gravadas no `metadata` de cada chunk."""
        return {
            EMBEDDING_PROVIDER_KEY: self.provider,
            EMBEDDING_MODEL_KEY: self.model,
            EMBEDDING_DIMENSIONS_KEY: self.dimensions,
        }

    def to_dict(self) -> Json:
        """Forma serializavel usada por `/knowledge/collections` e `/knowledge/health`."""
        return {
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
        }

    def matches(self, other: EmbeddingFingerprint) -> bool:
        """True quando provedor, modelo e dimensao coincidem."""
        return (
            self.provider == other.provider
            and self.model == other.model
            and self.dimensions == other.dimensions
        )

    @classmethod
    def from_metadata(cls, metadata: Json | None) -> EmbeddingFingerprint | None:
        """Le a identidade gravada no `metadata` de um chunk; `None` se ausente."""
        if not metadata:
            return None
        provider = metadata.get(EMBEDDING_PROVIDER_KEY)
        model = metadata.get(EMBEDDING_MODEL_KEY)
        dimensions = metadata.get(EMBEDDING_DIMENSIONS_KEY)
        if not isinstance(provider, str) or not isinstance(model, str):
            return None
        if not isinstance(dimensions, int) or isinstance(dimensions, bool):
            return None
        return cls(provider=provider, model=model, dimensions=dimensions)


# ---------------------------------------------------------------------------
# DTOs de entrada e de saida
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DocumentInput:
    """Pedido de ingestao de um documento na base de conhecimento."""

    title: str
    content: str
    collection: str = ""
    source: str = ""
    metadata: Json = field(default_factory=dict)
    document_id: Id | None = None


@dataclass(frozen=True, slots=True)
class DocumentFilter:
    """Filtros de listagem de documentos."""

    collection: str | None = None
    search: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao vinda da borda HTTP."""
        object.__setattr__(self, "limit", _clamp_limit(self.limit, ceiling=MAX_PAGE_LIMIT))
        object.__setattr__(self, "offset", max(0, int(self.offset)))


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Pedido de busca semantica sobre uma colecao."""

    query: str
    collection: str = ""
    limit: int = DEFAULT_SEARCH_LIMIT
    filters: Json = field(default_factory=dict)
    rerank: bool = False


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Resultado de uma ingestao ou reindexacao."""

    document: Document
    chunks: int
    embedded: bool
    reindexed: bool
    fingerprint: EmbeddingFingerprint

    @property
    def idempotent(self) -> bool:
        """True quando o conteudo ja estava indexado e nada foi re-embeddado."""
        return not self.embedded

    def to_dict(self) -> Json:
        """Forma serializavel para a resposta de `POST /knowledge/documents`."""
        return {
            "document": self.document.model_dump(mode="json"),
            "chunks": self.chunks,
            "embedded": self.embedded,
            "reindexed": self.reindexed,
            "idempotent": self.idempotent,
            "embedding": self.fingerprint.to_dict(),
        }


# ---------------------------------------------------------------------------
# Base dos casos de uso
# ---------------------------------------------------------------------------
class _KnowledgeUseCase:
    """Base dos casos de uso de conhecimento: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container

    # -- embedder corrente -------------------------------------------------
    def fingerprint(self) -> EmbeddingFingerprint:
        """Identidade do embedder configurado nesta instalacao."""
        embeddings = self._container.embeddings
        settings = self._container.settings
        provider = getattr(embeddings, "provider", "") or settings.embedding.effective_provider
        return EmbeddingFingerprint(
            provider=str(provider),
            model=str(embeddings.model),
            dimensions=int(embeddings.dimensions),
        )

    def collection_of(self, requested: str | None) -> str:
        """Colecao pedida ou a padrao de `Settings` quando o chamador omite."""
        candidate = (requested or "").strip()
        return candidate or self._container.settings.embedding.collection

    # -- guarda de compatibilidade (SPEC-0007 secao 1.2) -------------------
    async def collection_fingerprint(
        self, uow: UnitOfWork, collection: str
    ) -> EmbeddingFingerprint | None:
        """Descobre qual embedder produziu a colecao; `None` quando ela esta vazia."""
        documents = await uow.documents.list(
            collection=collection, limit=COLLECTION_PROBE_DOCUMENTS
        )
        for document in documents:
            for chunk in await uow.documents.list_chunks(document.id):
                found = EmbeddingFingerprint.from_metadata(chunk.metadata)
                if found is not None:
                    return found
        return None

    async def ensure_compatible(
        self, uow: UnitOfWork, collection: str, *, action: str
    ) -> EmbeddingFingerprint:
        """Recusa operar em colecao produzida por outro embedder.

        O dano seria silencioso: vetores de espacos semanticos diferentes convivem
        sem erro no indice e so aparecem como resultados errados. Por isso a
        divergencia vira :class:`ValidationError` com o caminho de saida
        explicito.
        """
        current = self.fingerprint()
        existing = await self.collection_fingerprint(uow, collection)
        if existing is None or existing.matches(current):
            return current
        raise ValidationError(
            f"Nao e possivel {action} na colecao '{collection}': ela foi produzida por "
            f"'{existing.provider}'/'{existing.model}' com {existing.dimensions} dimensoes, "
            f"e o embedder configurado e '{current.provider}'/'{current.model}' com "
            f"{current.dimensions} dimensoes. Reindexe a colecao inteira com o embedder "
            f"atual ou volte a configuracao anterior.",
            details={
                "collection": collection,
                "action": action,
                "collection_embedding": existing.to_dict(),
                "configured_embedding": current.to_dict(),
                "remediation": "reindexar a colecao inteira ou restaurar a configuracao anterior",
            },
        )

    # -- documentos --------------------------------------------------------
    @staticmethod
    async def require_document(uow: UnitOfWork, document_id: Id) -> Document:
        """Carrega o documento ou levanta :class:`NotFoundError`."""
        found = await uow.documents.get(document_id)
        if found is None:
            raise NotFoundError(
                f"Documento '{document_id}' nao encontrado.",
                details={"document_id": document_id},
            )
        return found

    # -- embeddings em lote ------------------------------------------------
    async def embed_batches(self, texts: Sequence[str]) -> list[list[float]]:
        """Gera os embeddings dos trechos em lotes de `settings.embedding.batch_size`."""
        if not texts:
            return []
        size = max(1, int(self._container.settings.embedding.batch_size))
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            batch = list(texts[start : start + size])
            produced = await self._container.embeddings.embed(batch)
            if len(produced) != len(batch):
                raise ValidationError(
                    "O provedor de embeddings devolveu uma quantidade de vetores "
                    "diferente da quantidade de textos enviados.",
                    details={"expected": len(batch), "received": len(produced)},
                )
            vectors.extend(list(vector) for vector in produced)
        return vectors

    def build_chunks(
        self,
        document: Document,
        pieces: Sequence[str],
        vectors: Sequence[Sequence[float]],
        fingerprint: EmbeddingFingerprint,
    ) -> list[Chunk]:
        """Monta os chunks do documento com metadados de rastreio e de embedder."""
        base: Json = dict(document.metadata)
        base.update(
            {
                "document_title": document.title,
                "source": document.source,
                "checksum": document.checksum,
            }
        )
        base.update(fingerprint.as_metadata())
        chunks: list[Chunk] = []
        for index, (piece, vector) in enumerate(zip(pieces, vectors, strict=True)):
            metadata = dict(base)
            metadata["chunk_index"] = index
            chunks.append(
                Chunk(
                    id=_chunk_id(document.id, index),
                    document_id=document.id,
                    collection=document.collection,
                    index=index,
                    content=piece,
                    metadata=metadata,
                    embedding=list(vector),
                    token_count=estimate_tokens(piece),
                )
            )
        return chunks

    async def index_document(self, document: Document, fingerprint: EmbeddingFingerprint) -> int:
        """Recorta, embedda e regrava os chunks do documento; devolve quantos ficaram."""
        pieces = chunk_text(document.content)
        if not pieces:
            raise ValidationError(
                "O documento nao produziu nenhum trecho indexavel.",
                details={"document_id": document.id, "collection": document.collection},
            )
        vectors = await self.embed_batches(pieces)
        chunks = self.build_chunks(document, pieces, vectors, fingerprint)
        store = self._container.vector_store
        await store.delete(document.collection, document_id=document.id)
        return await store.upsert(document.collection, chunks)


def _chunk_id(document_id: Id, index: int) -> Id:
    """Identificador deterministico do chunk: reindexar sobrescreve, nunca duplica."""
    return str(uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}:{index}"))


# ---------------------------------------------------------------------------
# Ingestao
# ---------------------------------------------------------------------------
class IngestDocument(_KnowledgeUseCase):
    """Ingere um documento: normaliza, recorta, embedda e indexa.

    Reingerir o mesmo conteudo (mesmo `checksum`) e idempotente: os metadados sao
    atualizados, nenhum embedding e pedido e nenhum chunk e duplicado.
    """

    async def execute(self, data: DocumentInput, principal: Principal) -> IngestResult:
        """Grava o documento e o indexa; devolve o resultado da operacao."""
        authorize(principal, Permission.KNOWLEDGE_WRITE, "ingerir documentos")
        title = data.title.strip()
        if not title:
            raise ValidationError(
                "O documento precisa de um titulo.",
                details={"field": "title"},
            )
        content = normalize_content(data.content)
        if not content:
            raise ValidationError(
                "O documento nao tem conteudo aproveitavel.",
                details={"field": "content", "title": title},
            )
        collection = self.collection_of(data.collection)
        checksum = content_checksum(content)
        source = data.source.strip()
        metadata = dict(data.metadata or {})

        async with self._container.uow_factory() as uow:
            fingerprint = await self.ensure_compatible(uow, collection, action="ingerir")
            existing = await self._find_existing(uow, collection, data, title=title, source=source)
            if existing is not None and existing.checksum == checksum:
                return await self._refresh_metadata(
                    uow,
                    existing,
                    title=title,
                    source=source,
                    metadata=metadata,
                    fingerprint=fingerprint,
                )
            previous_id = existing.id if existing is not None else None

        document = Document(
            id=previous_id or data.document_id or new_id(),
            collection=collection,
            title=title,
            source=source,
            content=content,
            metadata=metadata,
            checksum=checksum,
        )
        stored = await self._persist(document, previous_id=previous_id)
        indexed = await self.index_document(stored, fingerprint)
        _logger.info(
            "document_ingested",
            document_id=stored.id,
            collection=collection,
            chunks=indexed,
            reindexed=previous_id is not None,
            embedding_model=fingerprint.model,
        )
        return IngestResult(
            document=stored,
            chunks=indexed,
            embedded=True,
            reindexed=previous_id is not None,
            fingerprint=fingerprint,
        )

    async def _find_existing(
        self,
        uow: UnitOfWork,
        collection: str,
        data: DocumentInput,
        *,
        title: str,
        source: str,
    ) -> Document | None:
        """Resolve o documento que esta sendo reingerido.

        A identidade e, nesta ordem: o `document_id` informado, a mesma `source`
        dentro da colecao e, por fim, o mesmo titulo dentro da colecao.
        """
        if data.document_id:
            found = await uow.documents.get(data.document_id)
            if found is not None:
                return found
        for needle, attribute in ((source, "source"), (title, "title")):
            if not needle:
                continue
            candidates = await uow.documents.list(
                collection=collection, search=needle, limit=MAX_PAGE_LIMIT
            )
            for candidate in candidates:
                if getattr(candidate, attribute) == needle:
                    return candidate
        return None

    async def _refresh_metadata(
        self,
        uow: UnitOfWork,
        existing: Document,
        *,
        title: str,
        source: str,
        metadata: Json,
        fingerprint: EmbeddingFingerprint,
    ) -> IngestResult:
        """Caminho idempotente: mesmo checksum, so os metadados sao atualizados."""
        merged: Json = {**existing.metadata, **metadata}
        updated = existing.model_copy(
            update={
                "title": title,
                "source": source or existing.source,
                "metadata": merged,
                "updated_at": utcnow(),
            }
        )
        stored = await uow.documents.update(updated)
        await uow.commit()
        chunks = len(await uow.documents.list_chunks(stored.id))
        _logger.info(
            "document_ingest_idempotent",
            document_id=stored.id,
            collection=stored.collection,
            checksum=stored.checksum,
            chunks=chunks,
        )
        return IngestResult(
            document=stored,
            chunks=chunks,
            embedded=False,
            reindexed=False,
            fingerprint=fingerprint,
        )

    async def _persist(self, document: Document, *, previous_id: Id | None) -> Document:
        """Insere ou atualiza a linha do documento em uma transacao curta."""
        async with self._container.uow_factory() as uow:
            if previous_id is not None:
                current = await uow.documents.get(previous_id)
                if current is not None:
                    updated = current.model_copy(
                        update={
                            "collection": document.collection,
                            "title": document.title,
                            "source": document.source,
                            "content": document.content,
                            "metadata": document.metadata,
                            "checksum": document.checksum,
                            "updated_at": utcnow(),
                        }
                    )
                    stored = await uow.documents.update(updated)
                    await uow.commit()
                    return stored
            stored = await uow.documents.add(document)
            await uow.commit()
            return stored


class ReindexDocument(_KnowledgeUseCase):
    """Recalcula chunks e embeddings de um documento ja gravado."""

    async def execute(self, document_id: Id, principal: Principal) -> IngestResult:
        """Reindexa o documento com o embedder corrente."""
        authorize(principal, Permission.KNOWLEDGE_WRITE, "reindexar documentos")
        async with self._container.uow_factory() as uow:
            document = await self.require_document(uow, document_id)
            fingerprint = await self.ensure_compatible(uow, document.collection, action="reindexar")
        indexed = await self.index_document(document, fingerprint)
        _logger.info(
            "document_reindexed",
            document_id=document.id,
            collection=document.collection,
            chunks=indexed,
        )
        return IngestResult(
            document=document,
            chunks=indexed,
            embedded=True,
            reindexed=True,
            fingerprint=fingerprint,
        )


# ---------------------------------------------------------------------------
# Leitura e remocao
# ---------------------------------------------------------------------------
class GetDocument(_KnowledgeUseCase):
    """Busca um documento pelo identificador."""

    async def execute(self, document_id: Id, principal: Principal) -> Document:
        """Devolve o documento; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.KNOWLEDGE_READ, "ler documentos")
        async with self._container.uow_factory() as uow:
            return await self.require_document(uow, document_id)


class ListDocuments(_KnowledgeUseCase):
    """Lista documentos paginados, do mais recente para o mais antigo."""

    async def execute(self, filters: DocumentFilter, principal: Principal) -> Page[Document]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, Permission.KNOWLEDGE_READ, "listar documentos")
        criteria: Json = {}
        if filters.collection:
            criteria["collection"] = filters.collection
        if filters.search:
            criteria["search"] = filters.search
        async with self._container.uow_factory() as uow:
            items = await uow.documents.list(**criteria, limit=filters.limit, offset=filters.offset)
            total = await uow.documents.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class DeleteDocument(_KnowledgeUseCase):
    """Remove um documento e os seus chunks do indice."""

    async def execute(self, document_id: Id, principal: Principal) -> None:
        """Apaga vetores e documento; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.KNOWLEDGE_WRITE, "remover documentos")
        async with self._container.uow_factory() as uow:
            document = await self.require_document(uow, document_id)
        removed = await self._container.vector_store.delete(
            document.collection, document_id=document.id
        )
        async with self._container.uow_factory() as uow:
            await uow.documents.delete(document.id)
            await uow.commit()
        _logger.info(
            "document_deleted",
            document_id=document.id,
            collection=document.collection,
            chunks=removed,
        )


class ListCollections(_KnowledgeUseCase):
    """Lista as colecoes existentes e o embedder que produziu cada uma."""

    async def execute(self, principal: Principal) -> list[Json]:
        """Devolve, por colecao, contagem de documentos e identidade do embedder."""
        authorize(principal, Permission.KNOWLEDGE_READ, "listar colecoes")
        current = self.fingerprint()
        default = self._container.settings.embedding.collection
        names: set[str] = {default}
        async with self._container.uow_factory() as uow:
            names.update(await uow.documents.collections())
            names.update(await self._container.vector_store.collections())
            items: list[Json] = []
            for name in sorted(names):
                total = await uow.documents.count(collection=name)
                produced = await self.collection_fingerprint(uow, name)
                items.append(
                    {
                        "name": name,
                        "documents": min(int(total), MAX_COLLECTION_DOCUMENTS),
                        "is_default": name == default,
                        "embedding": (produced or current).to_dict(),
                        "indexed": produced is not None,
                        "compatible": produced is None or produced.matches(current),
                    }
                )
        return items


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------
class SearchKnowledge(_KnowledgeUseCase):
    """Busca semantica sobre uma colecao, com rerank lexical opcional."""

    async def execute(self, query: SearchQuery, principal: Principal) -> list[SearchHit]:
        """Embedda a consulta, busca no indice e (opcionalmente) reordena.

        Com `rerank=True` o score final combina o score vetorial com a
        similaridade lexica do texto do chunk
        (`0.6 * vetorial + 0.4 * lexico`), o que resgata trechos com o termo
        exato que o vizinho mais proximo teria deixado para tras.
        """
        authorize(principal, Permission.KNOWLEDGE_READ, "buscar na base de conhecimento")
        text = query.query.strip()
        if not text:
            raise ValidationError(
                "A consulta de busca nao pode ser vazia.",
                details={"field": "query"},
            )
        collection = self.collection_of(query.collection)
        limit = _clamp_limit(query.limit, ceiling=MAX_SEARCH_LIMIT)

        async with self._container.uow_factory() as uow:
            await self.ensure_compatible(uow, collection, action="buscar")

        vector = await self._container.embeddings.embed_one(text)
        fetch = limit * RERANK_CANDIDATE_FACTOR if query.rerank else limit
        hits = await self._container.vector_store.search(
            collection,
            vector,
            limit=_clamp_limit(fetch, ceiling=MAX_SEARCH_LIMIT * RERANK_CANDIDATE_FACTOR),
            filters=dict(query.filters) if query.filters else None,
        )
        ranked = self._rerank(text, hits) if query.rerank else self._normalized(hits)
        return ranked[:limit]

    async def search(
        self,
        query: str,
        principal: Principal,
        *,
        collection: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        filters: Json | None = None,
        rerank: bool = False,
    ) -> list[SearchHit]:
        """Variante por argumentos soltos, conveniente para a camada `interfaces`."""
        return await self.execute(
            SearchQuery(
                query=query,
                collection=collection or "",
                limit=limit,
                filters=dict(filters) if filters else {},
                rerank=rerank,
            ),
            principal,
        )

    @staticmethod
    def _normalized(hits: Sequence[SearchHit]) -> list[SearchHit]:
        """Garante `score` em `[0, 1]` sem alterar a ordem devolvida pelo indice."""
        return [hit.model_copy(update={"score": _clamp_score(hit.score)}) for hit in hits]

    @staticmethod
    def _rerank(query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        """Reordena combinando o score vetorial com a similaridade lexica do chunk."""
        matcher = _lexical_matcher()
        reranked: list[SearchHit] = []
        for hit in hits:
            vector_score = _clamp_score(hit.score)
            lexical_score = _clamp_score(matcher.score(query, hit.content))
            combined = _clamp_score(
                RERANK_VECTOR_WEIGHT * vector_score + RERANK_LEXICAL_WEIGHT * lexical_score
            )
            metadata: Json = dict(hit.metadata)
            metadata["rerank"] = {
                "vector_score": vector_score,
                "lexical_score": lexical_score,
                "backend": matcher.backend,
            }
            reranked.append(hit.model_copy(update={"score": combined, "metadata": metadata}))
        reranked.sort(key=lambda item: (-item.score, item.chunk_id))
        return reranked


# ---------------------------------------------------------------------------
# Saude
# ---------------------------------------------------------------------------
class KnowledgeHealth(_KnowledgeUseCase):
    """Estado do embedder corrente e das colecoes ja indexadas."""

    async def execute(self, principal: Principal) -> Json:
        """Devolve provedor, modelo, dimensao, modo degradado e mapa de colecoes."""
        authorize(principal, Permission.KNOWLEDGE_READ, "consultar a saude do conhecimento")
        current = self.fingerprint()
        settings = self._container.settings
        healthy = await self._probe()
        degraded = current.provider == HASHING_PROVIDER or not healthy
        report: Json = {
            "provider": current.provider,
            "model": current.model,
            "dimensions": current.dimensions,
            "degraded": degraded,
            "healthy": healthy,
            "configured_provider": settings.embedding.provider,
            "default_collection": settings.embedding.collection,
            "batch_size": settings.embedding.batch_size,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
            "reason": _degraded_reason(current.provider, healthy=healthy),
        }
        report["collections"] = await self._collections(current)
        return report

    async def _probe(self) -> bool:
        """Consulta `embeddings.health()`; provedor fora do ar nao derruba o relatorio."""
        try:
            return bool(await self._container.embeddings.health())
        except Exception as exc:
            _logger.warning("knowledge_health_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return False

    async def _collections(self, current: EmbeddingFingerprint) -> list[Json]:
        """Mapa `colecao -> embedder que a produziu`, tolerante a falha do banco."""
        try:
            async with self._container.uow_factory() as uow:
                names = sorted(set(await uow.documents.collections()))
                items: list[Json] = []
                for name in names:
                    produced = await self.collection_fingerprint(uow, name)
                    items.append(
                        {
                            "name": name,
                            "embedding": (produced or current).to_dict(),
                            "indexed": produced is not None,
                            "compatible": produced is None or produced.matches(current),
                        }
                    )
                return items
        except Exception as exc:
            _logger.warning(
                "knowledge_health_collections_failed", error=f"{type(exc).__name__}: {exc}"
            )
            return []


def _degraded_reason(provider: str, *, healthy: bool) -> str:
    """Explica em uma linha por que o embedder esta (ou nao) degradado."""
    if provider == HASHING_PROVIDER:
        return (
            "embedder deterministico offline: os vetores nao tem qualidade semantica "
            "real e nao devem ser misturados a uma colecao de producao"
        )
    if not healthy:
        return "o provedor de embeddings nao respondeu a verificacao de saude"
    return "embedder de producao ativo"
