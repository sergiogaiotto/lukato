"""Repositorio de usuarios autenticaveis (SPEC-0011 secao 4, SPEC-0006).

Implementa a porta `UserRepository`. O e-mail e a identidade de login: a busca usa
`func.lower` dos dois lados, de modo que `Ana@Claro.com` e `ana@claro.com` resolvem
para o mesmo usuario mesmo em bancos com colacao sensivel a caixa.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import user_apply, user_to_domain
from lukato.adapters.persistence.orm import UserRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.identity import Role, User
from lukato.domain.types import Id

__all__ = ["SqlAlchemyUserRepository"]

_FILTER_KEYS: Final[frozenset[str]] = frozenset({"is_active", "role", "tenant_id", "search"})
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
    *,
    is_active: bool | None = None,
    role: Role | str | None = None,
    tenant_id: str | None = None,
    search: str | None = None,
) -> list[ColumnElement[bool]]:
    """Monta as condicoes WHERE opcionais aceitas por `count`."""
    clauses: list[ColumnElement[bool]] = []
    if is_active is not None:
        clauses.append(UserRow.is_active.is_(bool(is_active)))
    if role is not None:
        clauses.append(UserRow.role == (role.value if isinstance(role, Role) else str(role)))
    if tenant_id:
        clauses.append(UserRow.tenant_id == tenant_id)
    if search:
        pattern = f"%{search.strip()}%"
        clauses.append(or_(UserRow.email.ilike(pattern), UserRow.name.ilike(pattern)))
    return clauses


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


def _filters_from(filters: dict[str, Any]) -> dict[str, Any]:
    """Valida `**filters` de `count`; `limit`/`offset` sao aceitos e ignorados."""
    unknown = sorted(set(filters) - _FILTER_KEYS - _PAGING_KEYS)
    if unknown:
        raise ValidationError(
            "filtro desconhecido para usuarios",
            details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
        )
    return {key: value for key, value in filters.items() if key in _FILTER_KEYS}


class SqlAlchemyUserRepository:
    """Usuarios em SQL; implementa a porta `UserRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, user: User) -> User:
        """Insere o usuario; e-mail duplicado gera `ConflictError`."""
        row = UserRow()
        user_apply(row, user)
        async with _translate("users.add"):
            self._session.add(row)
            await self._session.flush()
        return user_to_domain(row)

    async def get(self, user_id: Id) -> User | None:
        """Busca o usuario por identificador; `None` quando nao existe."""
        row = await self._row(user_id, operation="users.get")
        return None if row is None else user_to_domain(row)

    async def get_by_email(self, email: str) -> User | None:
        """Busca pelo e-mail unico, ignorando diferencas de caixa e espacos nas pontas."""
        normalized = email.strip().lower()
        if not normalized:
            return None
        statement = select(UserRow).where(func.lower(UserRow.email) == normalized).limit(1)
        async with _translate("users.get_by_email"):
            result = await self._session.execute(statement)
            row = result.scalars().first()
        return None if row is None else user_to_domain(row)

    async def list(self, *, limit: int = 50, offset: int = 0) -> builtins.list[User]:
        """Lista usuarios do mais recente para o mais antigo, sempre paginados."""
        bounded_limit, bounded_offset = _page(limit, offset)
        statement = (
            select(UserRow)
            .order_by(UserRow.created_at.desc(), UserRow.id.desc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("users.list"):
            result = await self._session.execute(statement)
            rows: Sequence[UserRow] = result.scalars().all()
        return [user_to_domain(row) for row in rows]

    async def count(self, **filters: Any) -> int:
        """Conta usuarios; sem filtros devolve o total da tabela."""
        statement = select(func.count()).select_from(UserRow)
        statement = statement.where(*_conditions(**_filters_from(filters)))
        async with _translate("users.count"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def update(self, user: User) -> User:
        """Grava o usuario existente; ausente gera `NotFoundError`."""
        row = await self._require(user.id, operation="users.update")
        user_apply(row, user)
        async with _translate("users.update"):
            await self._session.flush()
        return user_to_domain(row)

    async def delete(self, user_id: Id) -> None:
        """Remove o usuario; ausente gera `NotFoundError`."""
        row = await self._require(user_id, operation="users.delete")
        async with _translate("users.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def _row(self, user_id: Id, *, operation: str) -> UserRow | None:
        """Carrega a linha bruta do usuario, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(UserRow, user_id)

    async def _require(self, user_id: Id, *, operation: str) -> UserRow:
        """Carrega a linha do usuario ou levanta `NotFoundError`."""
        row = await self._row(user_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"usuario nao encontrado: {user_id}",
                details={"user_id": user_id},
            )
        return row
