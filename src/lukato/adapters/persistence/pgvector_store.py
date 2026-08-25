"""Indice vetorial sobre a tabela `chunks` (SPEC-0011 secao 7, SPEC-0007).

`PgVectorStore` implementa a porta `VectorStorePort` com dois caminhos de busca:

* **PostgreSQL** — distancia cosseno nativa do pgvector
  (`ChunkRow.embedding.cosine_distance(vector)`), ordenada de forma crescente pela
  distancia; o `score` devolvido e `1 - distancia`. Com o indice HNSW criado pela
  migracao `0002` a ordenacao acontece dentro do banco.
* **SQLite e demais dialetos** — modo varredura: carrega ate `sqlite_scan_limit`
  chunks da colecao que tenham embedding, calcula o cosseno com `numpy` e ordena em
  memoria. E o caminho offline determinista exigido pelo projeto; cada consulta
  registra DEBUG avisando que esta em varredura.

Divergencia entre a dimensao do vetor recebido e a dimensao configurada nunca e
silenciada: vira `ValidationError` com mensagem explicita (SPEC-0007 secao 1), pois
gravar embeddings de tamanhos diferentes corromperia a colecao inteira.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import ColumnElement, Float, delete, literal, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lukato.adapters.persistence.mappers import chunk_apply, chunk_to_domain
from lukato.adapters.persistence.orm import ChunkRow
from lukato.adapters.persistence.session import is_postgres
from lukato.config import get_logger
from lukato.domain.errors import ConflictError, ProviderError, ValidationError
from lukato.domain.models.knowledge import Chunk, SearchHit
from lukato.domain.types import Id, Json

__all__ = ["DEFAULT_SQLITE_SCAN_LIMIT", "MAX_COLLECTIONS", "PgVectorStore"]

_logger = get_logger(__name__)

DEFAULT_SQLITE_SCAN_LIMIT: Final[int] = 10_000
"""Quantos chunks o modo varredura carrega, no maximo, por consulta."""

MAX_COLLECTIONS: Final[int] = 1_000
"""Teto de seguranca de `collections` (nenhum select do lukato roda sem LIMIT)."""

_DOCUMENT_ID_KEY: Final[str] = "document_id"
_COLLECTION_KEY: Final[str] = "collection"
_COLUMN_FILTER_KEYS: Final[frozenset[str]] = frozenset({_DOCUMENT_ID_KEY, _COLLECTION_KEY})


async def _rollback(session: AsyncSession) -> None:
    """Desfaz a transacao da sessao dedicada, sem deixar o erro original se perder."""
    try:
        await session.rollback()
    except SQLAlchemyError as exc:
        _logger.warning("vector_store_rollback_failed", error=str(exc))


def _metadata_condition(key: str, value: Any) -> ColumnElement[bool]:
    """Igualdade sobre uma chave do JSON `metadata`, tipada conforme o valor buscado."""
    element = ChunkRow.meta[key]
    if isinstance(value, bool):
        return element.as_boolean() == value
    if isinstance(value, int):
        return element.as_integer() == value
    if isinstance(value, float):
        return element.as_float() == value
    return element.as_string() == str(value)


def _split_filters(filters: Json | None) -> tuple[list[ColumnElement[bool]], Json]:
    """Separa filtros de coluna (`document_id`, `collection`) dos filtros de metadata."""
    columns: list[ColumnElement[bool]] = []
    metadata: Json = {}
    for key, value in (filters or {}).items():
        if key == _DOCUMENT_ID_KEY:
            columns.append(ChunkRow.document_id == str(value))
        elif key == _COLLECTION_KEY:
            columns.append(ChunkRow.collection == str(value))
        else:
            metadata[key] = value
    return columns, metadata


def _cosine_distance(vector: Sequence[float], dimensions: int) -> ColumnElement[float]:
    """Expressao SQL da distancia cosseno do pgvector para a coluna `chunks.embedding`.

    `ChunkRow.embedding` e um `TypeDecorator` portatil (pgvector no PostgreSQL, JSON
    nos demais dialetos). Quando o comparador nativo esta exposto no atributo mapeado,
    usa-se `ChunkRow.embedding.cosine_distance(vector)` diretamente; caso contrario
    emite-se o mesmo operador `<=>` com o vetor ligado como literal `vector(dim)`,
    sem converter a coluna — assim o indice HNSW continua utilizavel.
    """
    values = [float(item) for item in vector]
    native = getattr(ChunkRow.embedding, "cosine_distance", None)
    if callable(native):
        distance: ColumnElement[float] = native(values)
        return distance
    operand = literal(values, Vector(dimensions))
    return ChunkRow.embedding.op("<=>", return_type=Float)(operand)


def _clamp(score: float) -> float:
    """Mantem o score dentro de `[-1, 1]`, protegendo contra ruido de ponto flutuante."""
    return max(-1.0, min(1.0, score))


def _hit(row: ChunkRow, score: float) -> SearchHit:
    """Converte a linha em `SearchHit` pelo mapper — nenhum objeto ORM escapa daqui."""
    chunk = chunk_to_domain(row)
    return SearchHit(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        collection=chunk.collection,
        content=chunk.content,
        score=_clamp(float(score)),
        metadata=dict(chunk.metadata),
    )


class PgVectorStore:
    """Indice vetorial da base de conhecimento; implementa a porta `VectorStorePort`.

    Recebe uma fabrica de sessoes (e nao uma sessao) porque cada operacao e uma
    transacao curta e independente, fora da unidade de trabalho dos casos de uso.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dimensions: int,
        sqlite_scan_limit: int = DEFAULT_SQLITE_SCAN_LIMIT,
    ) -> None:
        """Guarda a fabrica de sessoes, a dimensao esperada e o teto da varredura."""
        if dimensions <= 0:
            raise ValidationError(
                f"dimensao de embedding invalida: {dimensions}",
                details={"dimensions": dimensions},
            )
        self._session_factory = session_factory
        self._dimensions = int(dimensions)
        self._sqlite_scan_limit = max(1, int(sqlite_scan_limit))

    @property
    def dimensions(self) -> int:
        """Dimensionalidade exigida de todo vetor gravado ou consultado."""
        return self._dimensions

    @property
    def sqlite_scan_limit(self) -> int:
        """Quantos chunks o modo varredura carrega, no maximo, por consulta."""
        return self._sqlite_scan_limit

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        """Grava ou atualiza os chunks da colecao; devolve quantos foram afetados."""
        if not chunks:
            return 0
        for chunk in chunks:
            self._check_dimension(chunk.embedding, origin=f"chunk {chunk.id}")

        async with self._open("vector_store.upsert") as session:
            existing = await self._existing_rows(session, [chunk.id for chunk in chunks])
            for chunk in chunks:
                row = existing.get(chunk.id)
                if row is None:
                    row = ChunkRow()
                    chunk_apply(row, chunk)
                    row.collection = collection
                    session.add(row)
                else:
                    chunk_apply(row, chunk)
                    row.collection = collection
            await session.flush()
            await session.commit()
        return len(chunks)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int = 10,
        filters: Json | None = None,
    ) -> builtins.list[SearchHit]:
        """Busca os chunks mais proximos do vetor, do maior para o menor score."""
        self._check_dimension(vector, origin="vetor de consulta")
        bounded_limit = max(1, int(limit))
        column_filters, metadata_filters = _split_filters(filters)

        async with self._open("vector_store.search") as session:
            if is_postgres(session.get_bind()):
                return await self._search_native(
                    session,
                    collection,
                    vector,
                    limit=bounded_limit,
                    column_filters=column_filters,
                    metadata_filters=metadata_filters,
                )
            return await self._search_scan(
                session,
                collection,
                vector,
                limit=bounded_limit,
                column_filters=column_filters,
                metadata_filters=metadata_filters,
            )

    async def delete(self, collection: str, *, document_id: Id | None = None) -> int:
        """Remove a colecao inteira ou apenas os chunks de um documento."""
        statement = delete(ChunkRow).where(ChunkRow.collection == collection)
        if document_id is not None:
            statement = statement.where(ChunkRow.document_id == document_id)
        async with self._open("vector_store.delete") as session:
            result = await session.execute(statement)
            await session.commit()
            return int(result.rowcount or 0)

    async def collections(self) -> builtins.list[str]:
        """Lista as colecoes distintas presentes no indice, em ordem alfabetica."""
        statement = (
            select(ChunkRow.collection)
            .distinct()
            .order_by(ChunkRow.collection.asc())
            .limit(MAX_COLLECTIONS)
        )
        async with self._open("vector_store.collections") as session:
            result = await session.execute(statement)
            names: Sequence[str] = result.scalars().all()
        return [str(name) for name in names]

    # ----------------------------------------------------------------- interno

    @asynccontextmanager
    async def _open(self, operation: str) -> AsyncIterator[AsyncSession]:
        """Abre uma sessao dedicada e traduz erros do driver para o dominio."""
        session = self._session_factory()
        try:
            yield session
        except IntegrityError as exc:
            await _rollback(session)
            raise ConflictError(
                f"violacao de integridade em {operation}",
                details={"operation": operation, "error": str(exc.orig)},
            ) from exc
        except SQLAlchemyError as exc:
            await _rollback(session)
            raise ProviderError(
                f"falha de persistencia em {operation}: {exc}",
                details={"operation": operation, "error": type(exc).__name__},
            ) from exc
        except BaseException:
            await _rollback(session)
            raise
        finally:
            await session.close()

    def _check_dimension(self, vector: Sequence[float] | None, *, origin: str) -> None:
        """Recusa vetores com dimensao diferente da configurada (SPEC-0007 secao 1)."""
        if vector is None:
            return
        size = len(vector)
        if size != self._dimensions:
            raise ValidationError(
                f"dimensao de embedding incompativel em {origin}: "
                f"recebido {size}, esperado {self._dimensions}. "
                "Trocar a dimensao exige re-embeddar a colecao inteira.",
                details={"origin": origin, "received": size, "expected": self._dimensions},
            )

    async def _existing_rows(
        self, session: AsyncSession, chunk_ids: Sequence[Id]
    ) -> dict[Id, ChunkRow]:
        """Carrega em lote as linhas ja existentes dos chunks informados."""
        statement = select(ChunkRow).where(ChunkRow.id.in_(list(chunk_ids)))
        result = await session.execute(statement)
        return {row.id: row for row in result.scalars().all()}

    async def _search_native(
        self,
        session: AsyncSession,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int,
        column_filters: Sequence[ColumnElement[bool]],
        metadata_filters: Json,
    ) -> builtins.list[SearchHit]:
        """Busca com o operador de distancia cosseno do pgvector, ordenada no banco."""
        distance = _cosine_distance(vector, self._dimensions).label("distance")
        conditions: list[ColumnElement[bool]] = [
            ChunkRow.collection == collection,
            ChunkRow.embedding.is_not(None),
            *column_filters,
            *(_metadata_condition(key, value) for key, value in metadata_filters.items()),
        ]
        statement = (
            select(ChunkRow, distance).where(*conditions).order_by(distance.asc()).limit(limit)
        )
        result = await session.execute(statement)
        return [_hit(row, 1.0 - float(value)) for row, value in result.all()]

    async def _search_scan(
        self,
        session: AsyncSession,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int,
        column_filters: Sequence[ColumnElement[bool]],
        metadata_filters: Json,
    ) -> builtins.list[SearchHit]:
        """Modo varredura: cosseno com `numpy` em memoria, para SQLite e afins."""
        conditions: list[ColumnElement[bool]] = [
            ChunkRow.collection == collection,
            ChunkRow.embedding.is_not(None),
            *column_filters,
        ]
        statement = (
            select(ChunkRow)
            .where(*conditions)
            .order_by(ChunkRow.document_id.asc(), ChunkRow.position.asc())
            .limit(self._sqlite_scan_limit)
        )
        result = await session.execute(statement)
        rows = [
            row
            for row in result.scalars().all()
            if self._matches_metadata(row, metadata_filters)
            and row.embedding is not None
            and len(row.embedding) == self._dimensions
        ]
        _logger.debug(
            "vector_store_scan_mode",
            collection=collection,
            scanned=len(rows),
            scan_limit=self._sqlite_scan_limit,
            limit=limit,
            reason="dialeto sem pgvector: cosseno calculado em memoria com numpy",
        )
        if not rows:
            return []

        matrix = np.asarray([row.embedding for row in rows], dtype=np.float64)
        query = np.asarray([float(item) for item in vector], dtype=np.float64)
        query_norm = float(np.linalg.norm(query))
        row_norms = np.linalg.norm(matrix, axis=1)
        denominator = row_norms * query_norm
        safe = denominator > 0.0
        scores = np.zeros(len(rows), dtype=np.float64)
        np.divide(matrix @ query, denominator, out=scores, where=safe)

        ranked = sorted(zip(rows, scores.tolist(), strict=True), key=lambda pair: -pair[1])
        return [_hit(row, score) for row, score in ranked[:limit]]

    @staticmethod
    def _matches_metadata(row: ChunkRow, metadata_filters: Json) -> bool:
        """Aplica em Python a igualdade sobre o JSON `metadata` (caminho SQLite)."""
        if not metadata_filters:
            return True
        metadata = row.meta if isinstance(row.meta, dict) else {}
        return all(metadata.get(key) == value for key, value in metadata_filters.items())
