"""Repositorio SQLAlchemy do catalogo de modulos (SPEC-0011 secao 4, tabela `modules`).

Implementa a porta :class:`~lukato.domain.ports.repositories.ModuleRepository`. Todas
as leituras passam pelos mappers, de modo que nenhuma linha ORM escapa do adaptador.
O filtro por `tags` compara o texto JSON da coluna (`"tag"` entre aspas), estrategia
que funciona tanto em `JSONB` no PostgreSQL quanto em `JSON` no SQLite.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from typing import Any, Final, TypeVar

from sqlalchemy import Select, Text, cast, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from lukato.adapters.persistence.mappers import module_apply, module_to_domain
from lukato.adapters.persistence.orm import ModuleRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.module import ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.types import Id

__all__ = ["SqlAlchemyModuleRepository"]

_RowT = TypeVar("_RowT")

_LIKE_ESCAPE: Final[str] = "\\"
_FILTER_KEYS: Final[frozenset[str]] = frozenset(
    {"kind", "status", "search", "tags", "limit", "offset"}
)


def _like(term: str) -> str:
    """Monta o padrao `%termo%` neutralizando os curingas do proprio termo."""
    escaped = term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _execute(session: AsyncSession, statement: Any) -> Any:
    """Executa a instrucao traduzindo qualquer falha do driver em `ProviderError`."""
    try:
        return await session.execute(statement)
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha ao consultar o catalogo de modulos: {exc}",
            details={"error": type(exc).__name__},
        ) from exc


async def _rows(session: AsyncSession, statement: Select[tuple[_RowT]]) -> builtins.list[_RowT]:
    """Devolve todas as linhas escalares da consulta."""
    result = await _execute(session, statement)
    return list(result.scalars().all())


async def _first(session: AsyncSession, statement: Select[tuple[_RowT]]) -> _RowT | None:
    """Devolve a primeira linha escalar da consulta, ou `None`."""
    result = await _execute(session, statement)
    return result.scalars().first()


async def _flush(session: AsyncSession, *, conflict: str, details: dict[str, Any]) -> None:
    """Descarrega a sessao convertendo os erros do driver na hierarquia do dominio."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(conflict, details={**details, "error": str(exc.orig)}) from exc
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha ao gravar o modulo: {exc}",
            details={**details, "error": type(exc).__name__},
        ) from exc


def _as_kind(value: Any) -> ModuleKind | None:
    """Normaliza o filtro `kind` para o enum do dominio."""
    if value is None or isinstance(value, ModuleKind):
        return value
    try:
        return ModuleKind(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"kind invalido: {value!r}",
            details={"field": "kind", "allowed": [item.value for item in ModuleKind]},
        ) from exc


def _as_status(value: Any) -> ModuleStatus | None:
    """Normaliza o filtro `status` para o enum do dominio."""
    if value is None or isinstance(value, ModuleStatus):
        return value
    try:
        return ModuleStatus(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"status invalido: {value!r}",
            details={"field": "status", "allowed": [item.value for item in ModuleStatus]},
        ) from exc


def _as_tags(value: Any) -> Sequence[str] | None:
    """Normaliza o filtro `tags` para uma sequencia de textos."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    raise ValidationError(f"tags invalidas: {value!r}", details={"field": "tags"})


def _conditions(
    *,
    kind: ModuleKind | None,
    status: ModuleStatus | None,
    search: str | None,
    tags: Sequence[str] | None,
) -> builtins.list[ColumnElement[bool]]:
    """Traduz os filtros publicos em predicados SQL."""
    conditions: builtins.list[ColumnElement[bool]] = []
    if kind is not None:
        conditions.append(ModuleRow.kind == kind.value)
    if status is not None:
        conditions.append(ModuleRow.status == status.value)
    if search:
        pattern = _like(search)
        conditions.append(
            or_(
                ModuleRow.slug.ilike(pattern, escape=_LIKE_ESCAPE),
                ModuleRow.name.ilike(pattern, escape=_LIKE_ESCAPE),
                ModuleRow.description.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        )
    for tag in tags or ():
        conditions.append(cast(ModuleRow.tags, Text).like(_like(f'"{tag}"'), escape=_LIKE_ESCAPE))
    return conditions


class SqlAlchemyModuleRepository:
    """Catalogo persistido de `ModuleDefinition`; implementa `ModuleRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit pertence ao `UnitOfWork`)."""
        self._session = session

    async def add(self, module: ModuleDefinition) -> ModuleDefinition:
        """Insere a definicao; slug duplicado gera `ConflictError`."""
        if await self._slug_taken(module.slug):
            raise ConflictError(
                f"ja existe um modulo com o slug '{module.slug}'",
                details={"slug": module.slug},
            )
        row = ModuleRow()
        module_apply(row, module)
        self._session.add(row)
        await _flush(
            self._session,
            conflict=f"ja existe um modulo com o slug '{module.slug}'",
            details={"slug": module.slug},
        )
        return module_to_domain(row)

    async def get(self, module_id: Id) -> ModuleDefinition | None:
        """Busca por identificador."""
        row = await self._load(module_id)
        return None if row is None else module_to_domain(row)

    async def get_by_slug(self, slug: str) -> ModuleDefinition | None:
        """Busca pelo slug unico do modulo."""
        row = await _first(self._session, select(ModuleRow).where(ModuleRow.slug == slug))
        return None if row is None else module_to_domain(row)

    async def list(
        self,
        *,
        kind: ModuleKind | None = None,
        status: ModuleStatus | None = None,
        search: str | None = None,
        tags: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[ModuleDefinition]:
        """Lista definicoes por slug, aplicando os filtros e a paginacao informados.

        `tags` exige que a definicao possua **todas** as etiquetas indicadas.
        """
        statement = (
            select(ModuleRow)
            .where(*_conditions(kind=kind, status=status, search=search, tags=tags))
            .order_by(ModuleRow.slug.asc())
            .limit(max(limit, 0))
            .offset(max(offset, 0))
        )
        return [module_to_domain(row) for row in await _rows(self._session, statement)]

    async def count(self, **filters: Any) -> int:
        """Conta definicoes com os mesmos filtros aceitos por `list`."""
        unknown = sorted(set(filters) - _FILTER_KEYS)
        if unknown:
            raise ValidationError(
                f"filtros nao suportados para modulos: {', '.join(unknown)}",
                details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
            )
        conditions = _conditions(
            kind=_as_kind(filters.get("kind")),
            status=_as_status(filters.get("status")),
            search=filters.get("search"),
            tags=_as_tags(filters.get("tags")),
        )
        statement = select(func.count()).select_from(ModuleRow).where(*conditions)
        result = await _execute(self._session, statement)
        return int(result.scalar_one())

    async def update(self, module: ModuleDefinition) -> ModuleDefinition:
        """Grava a definicao existente; ausente gera `NotFoundError`."""
        row = await self._require(module.id)
        module_apply(row, module)
        await _flush(
            self._session,
            conflict=f"ja existe um modulo com o slug '{module.slug}'",
            details={"slug": module.slug, "id": module.id},
        )
        return module_to_domain(row)

    async def delete(self, module_id: Id) -> None:
        """Remove a definicao pelo identificador; ausente gera `NotFoundError`."""
        row = await self._require(module_id)
        try:
            await self._session.delete(row)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao remover o modulo: {exc}",
                details={"id": module_id, "error": type(exc).__name__},
            ) from exc
        await _flush(
            self._session,
            conflict="modulo referenciado por outro registro",
            details={"id": module_id},
        )

    async def _load(self, module_id: Id) -> ModuleRow | None:
        """Carrega a linha pela chave primaria."""
        try:
            return await self._session.get(ModuleRow, module_id)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao carregar o modulo: {exc}",
                details={"id": module_id, "error": type(exc).__name__},
            ) from exc

    async def _require(self, module_id: Id) -> ModuleRow:
        """Carrega a linha exigindo que ela exista."""
        row = await self._load(module_id)
        if row is None:
            raise NotFoundError(f"modulo '{module_id}' nao encontrado", details={"id": module_id})
        return row

    async def _slug_taken(self, slug: str) -> bool:
        """Informa se o slug ja pertence a outra definicao."""
        found = await _first(self._session, select(ModuleRow.id).where(ModuleRow.slug == slug))
        return found is not None
