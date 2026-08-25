"""Repositorio de chaves de API (SPEC-0011 secao 4, SPEC-0006).

Implementa a porta `ApiKeyRepository`. O segredo nunca e persistido: a tabela guarda
apenas o `prefix` publico (usado na busca da requisicao) e o `hashed_secret`.
`touch` atualiza `last_used_at` sem materializar a entidade — e caminho quente de
autenticacao, executado a cada requisicao autenticada por chave.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import api_key_apply, api_key_to_domain
from lukato.adapters.persistence.orm import ApiKeyRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError
from lukato.domain.models.identity import ApiKey
from lukato.domain.types import Id, utcnow

__all__ = ["SqlAlchemyApiKeyRepository"]

_MAX_PREFIX_LEN: Final[int] = 64
"""Largura da coluna `prefix`; entradas maiores nunca casam e sao recusadas cedo."""


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


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


class SqlAlchemyApiKeyRepository:
    """Chaves de API em SQL; implementa a porta `ApiKeyRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, api_key: ApiKey) -> ApiKey:
        """Insere a chave; prefixo duplicado gera `ConflictError`."""
        row = ApiKeyRow()
        api_key_apply(row, api_key)
        async with _translate("api_keys.add"):
            self._session.add(row)
            await self._session.flush()
        return api_key_to_domain(row)

    async def get(self, api_key_id: Id) -> ApiKey | None:
        """Busca a chave por identificador; `None` quando nao existe."""
        row = await self._row(api_key_id, operation="api_keys.get")
        return None if row is None else api_key_to_domain(row)

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        """Busca pelo prefixo unico apresentado na requisicao."""
        normalized = prefix.strip()
        if not normalized or len(normalized) > _MAX_PREFIX_LEN:
            return None
        statement = select(ApiKeyRow).where(ApiKeyRow.prefix == normalized).limit(1)
        async with _translate("api_keys.get_by_prefix"):
            result = await self._session.execute(statement)
            row = result.scalars().first()
        return None if row is None else api_key_to_domain(row)

    async def list(
        self, *, is_active: bool | None = None, limit: int = 50, offset: int = 0
    ) -> builtins.list[ApiKey]:
        """Lista chaves da mais recente para a mais antiga, sempre paginadas."""
        bounded_limit, bounded_offset = _page(limit, offset)
        clauses: list[ColumnElement[bool]] = []
        if is_active is not None:
            clauses.append(ApiKeyRow.is_active.is_(bool(is_active)))
        statement = (
            select(ApiKeyRow)
            .where(*clauses)
            .order_by(ApiKeyRow.created_at.desc(), ApiKeyRow.id.desc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("api_keys.list"):
            result = await self._session.execute(statement)
            rows: Sequence[ApiKeyRow] = result.scalars().all()
        return [api_key_to_domain(row) for row in rows]

    async def update(self, api_key: ApiKey) -> ApiKey:
        """Grava a chave existente; ausente gera `NotFoundError`."""
        row = await self._require(api_key.id, operation="api_keys.update")
        api_key_apply(row, api_key)
        async with _translate("api_keys.update"):
            await self._session.flush()
        return api_key_to_domain(row)

    async def delete(self, api_key_id: Id) -> None:
        """Remove a chave; ausente gera `NotFoundError`."""
        row = await self._require(api_key_id, operation="api_keys.delete")
        async with _translate("api_keys.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def touch(self, api_key_id: Id, when: datetime) -> None:
        """Registra o instante do ultimo uso da chave; ausente gera `NotFoundError`."""
        statement = (
            update(ApiKeyRow)
            .where(ApiKeyRow.id == api_key_id)
            .values(last_used_at=when, updated_at=utcnow())
            .execution_options(synchronize_session="fetch")
        )
        async with _translate("api_keys.touch"):
            result = await self._session.execute(statement)
            await self._session.flush()
        if not result.rowcount:
            raise NotFoundError(
                f"chave de API nao encontrada: {api_key_id}",
                details={"api_key_id": api_key_id},
            )

    async def _row(self, api_key_id: Id, *, operation: str) -> ApiKeyRow | None:
        """Carrega a linha bruta da chave, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(ApiKeyRow, api_key_id)

    async def _require(self, api_key_id: Id, *, operation: str) -> ApiKeyRow:
        """Carrega a linha da chave ou levanta `NotFoundError`."""
        row = await self._row(api_key_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"chave de API nao encontrada: {api_key_id}",
                details={"api_key_id": api_key_id},
            )
        return row
