"""Repositorio de documentos e chunks da base de conhecimento (SPEC-0011, SPEC-0007).

Implementa a porta `DocumentRepository`. Os chunks pertencem ao agregado documento:
`add_chunks` insere em lote, `delete_chunks` apaga por documento e a remocao do
documento leva os chunks junto via `ON DELETE CASCADE`.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import (
    chunk_apply,
    chunk_to_domain,
    document_apply,
    document_to_domain,
)
from lukato.adapters.persistence.orm import ChunkRow, DocumentRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.knowledge import Chunk, Document
from lukato.domain.types import Id

__all__ = ["MAX_CHUNKS_PER_DOCUMENT", "MAX_COLLECTIONS", "SqlAlchemyDocumentRepository"]

MAX_CHUNKS_PER_DOCUMENT: Final[int] = 10_000
"""Teto de seguranca de `list_chunks` (o contrato nao expoe paginacao)."""

MAX_COLLECTIONS: Final[int] = 1_000
"""Teto de seguranca de `collections` (o contrato nao expoe paginacao)."""

_FILTER_KEYS: Final[frozenset[str]] = frozenset({"collection", "search"})
_PAGING_KEYS: Final[frozenset[str]] = frozenset({"limit", "offset"})


@asynccontextmanager
async def _translate(operation: str) -> AsyncIterator[None]:
    """Converte erros do driver na hierarquia de erros do dominio."""
    try:
        yield
    except IntegrityError as exc:
        raise ConflictError(
            f"violacao de integridade em {operation}",
            details={"operation": operation, "error": str(exc.orig)},
        ) from exc
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha de persistencia em {operation}: {exc}",
            details={"operation": operation, "error": type(exc).__name__},
        ) from exc


def _conditions(
    *, collection: str | None = None, search: str | None = None
) -> list[ColumnElement[bool]]:
    """Monta as condicoes WHERE compartilhadas por `list` e `count`."""
    clauses: list[ColumnElement[bool]] = []
    if collection:
        clauses.append(DocumentRow.collection == collection)
    if search:
        pattern = f"%{search.strip()}%"
        clauses.append(
            or_(
                DocumentRow.title.ilike(pattern),
                DocumentRow.source.ilike(pattern),
                DocumentRow.content.ilike(pattern),
            )
        )
    return clauses


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


def _filters_from(filters: dict[str, Any]) -> dict[str, Any]:
    """Valida `**filters` de `count`, aceitando os mesmos nomes usados por `list`."""
    unknown = sorted(set(filters) - _FILTER_KEYS - _PAGING_KEYS)
    if unknown:
        raise ValidationError(
            "filtro desconhecido para documentos",
            details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
        )
    return {key: value for key, value in filters.items() if key in _FILTER_KEYS}


class SqlAlchemyDocumentRepository:
    """Documentos e chunks em SQL; implementa a porta `DocumentRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, document: Document) -> Document:
        """Insere o documento e devolve o modelo de dominio gravado."""
        row = DocumentRow()
        document_apply(row, document)
        async with _translate("documents.add"):
            self._session.add(row)
            await self._session.flush()
        return document_to_domain(row)

    async def get(self, document_id: Id) -> Document | None:
        """Busca o documento por identificador; `None` quando nao existe."""
        row = await self._row(document_id, operation="documents.get")
        return None if row is None else document_to_domain(row)

    async def list(
        self,
        *,
        collection: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Document]:
        """Lista documentos do mais recente para o mais antigo, sempre paginados."""
        bounded_limit, bounded_offset = _page(limit, offset)
        statement = (
            select(DocumentRow)
            .where(*_conditions(collection=collection, search=search))
            .order_by(DocumentRow.created_at.desc(), DocumentRow.id.desc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("documents.list"):
            result = await self._session.execute(statement)
            rows: Sequence[DocumentRow] = result.scalars().all()
        return [document_to_domain(row) for row in rows]

    async def count(self, **filters: Any) -> int:
        """Conta documentos com os mesmos filtros aceitos por `list`."""
        statement = select(func.count()).select_from(DocumentRow)
        statement = statement.where(*_conditions(**_filters_from(filters)))
        async with _translate("documents.count"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def update(self, document: Document) -> Document:
        """Grava o documento existente; ausente gera `NotFoundError`."""
        row = await self._require(document.id, operation="documents.update")
        document_apply(row, document)
        async with _translate("documents.update"):
            await self._session.flush()
        return document_to_domain(row)

    async def delete(self, document_id: Id) -> None:
        """Remove o documento e, em cascata, seus chunks; ausente gera `NotFoundError`."""
        row = await self._require(document_id, operation="documents.delete")
        async with _translate("documents.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def add_chunks(self, chunks: Sequence[Chunk]) -> int:
        """Insere os chunks em lote; devolve quantos foram gravados."""
        if not chunks:
            return 0
        rows: list[ChunkRow] = []
        for chunk in chunks:
            row = ChunkRow()
            chunk_apply(row, chunk)
            rows.append(row)
        async with _translate("documents.add_chunks"):
            self._session.add_all(rows)
            await self._session.flush()
        return len(rows)

    async def list_chunks(self, document_id: Id) -> builtins.list[Chunk]:
        """Lista os chunks do documento em ordem de indice."""
        statement = (
            select(ChunkRow)
            .where(ChunkRow.document_id == document_id)
            .order_by(ChunkRow.position.asc(), ChunkRow.id.asc())
            .limit(MAX_CHUNKS_PER_DOCUMENT)
        )
        async with _translate("documents.list_chunks"):
            result = await self._session.execute(statement)
            rows: Sequence[ChunkRow] = result.scalars().all()
        return [chunk_to_domain(row) for row in rows]

    async def count_chunks(self, document_id: Id) -> int:
        """Conta os chunks do documento sem carregar conteudo nem vetores."""
        statement = (
            select(func.count()).select_from(ChunkRow).where(ChunkRow.document_id == document_id)
        )
        async with _translate("documents.count_chunks"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def delete_chunks(self, document_id: Id) -> int:
        """Remove os chunks do documento; devolve quantos foram apagados."""
        statement = delete(ChunkRow).where(ChunkRow.document_id == document_id)
        async with _translate("documents.delete_chunks"):
            # DELETE/UPDATE devolvem CursorResult, o unico com `rowcount`;
            # os stubs do SQLAlchemy tipam `execute` como Result[Any].
            result = cast("CursorResult[Any]", await self._session.execute(statement))
            await self._session.flush()
        return int(result.rowcount or 0)

    async def collections(self) -> builtins.list[str]:
        """Lista as colecoes distintas existentes, em ordem alfabetica."""
        statement = (
            select(DocumentRow.collection)
            .distinct()
            .order_by(DocumentRow.collection.asc())
            .limit(MAX_COLLECTIONS)
        )
        async with _translate("documents.collections"):
            result = await self._session.execute(statement)
            names: Sequence[str] = result.scalars().all()
        return [str(name) for name in names]

    async def _row(self, document_id: Id, *, operation: str) -> DocumentRow | None:
        """Carrega a linha bruta do documento, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(DocumentRow, document_id)

    async def _require(self, document_id: Id, *, operation: str) -> DocumentRow:
        """Carrega a linha do documento ou levanta `NotFoundError`."""
        row = await self._row(document_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"documento nao encontrado: {document_id}",
                details={"document_id": document_id},
            )
        return row
