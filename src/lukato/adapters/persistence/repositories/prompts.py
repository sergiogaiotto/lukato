"""Repositorio SQLAlchemy da biblioteca de prompts (SPEC-0011 secao 4, tabela `prompts`).

Implementa a porta :class:`~lukato.domain.ports.repositories.PromptRepository`. O par
(`slug`, `version`) e unico: `get_by_slug` resolve sempre a **versao ativa de maior
numero**, e `list_versions` devolve o historico da mais recente para a mais antiga.
"""

from __future__ import annotations

import builtins
from typing import Any, Final, TypeVar

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from lukato.adapters.persistence.mappers import prompt_apply, prompt_to_domain
from lukato.adapters.persistence.orm import PromptRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.types import Id

__all__ = ["SqlAlchemyPromptRepository"]

_RowT = TypeVar("_RowT")

_LIKE_ESCAPE: Final[str] = "\\"
_FILTER_KEYS: Final[frozenset[str]] = frozenset({"search", "is_active", "limit", "offset"})


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
            f"falha ao consultar a biblioteca de prompts: {exc}",
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
            f"falha ao gravar o prompt: {exc}",
            details={**details, "error": type(exc).__name__},
        ) from exc


def _as_bool(value: Any, *, field: str) -> bool | None:
    """Normaliza um filtro booleano vindo de `**filters`."""
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise ValidationError(f"{field} invalido: {value!r}", details={"field": field})


def _conditions(
    *, search: str | None, is_active: bool | None
) -> builtins.list[ColumnElement[bool]]:
    """Traduz os filtros publicos em predicados SQL."""
    conditions: builtins.list[ColumnElement[bool]] = []
    if is_active is not None:
        conditions.append(PromptRow.is_active.is_(is_active))
    if search:
        pattern = _like(search)
        conditions.append(
            or_(
                PromptRow.slug.ilike(pattern, escape=_LIKE_ESCAPE),
                PromptRow.name.ilike(pattern, escape=_LIKE_ESCAPE),
                PromptRow.description.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        )
    return conditions


class SqlAlchemyPromptRepository:
    """Biblioteca persistida de `PromptTemplate`; implementa `PromptRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit pertence ao `UnitOfWork`)."""
        self._session = session

    async def add(self, prompt: PromptTemplate) -> PromptTemplate:
        """Insere uma versao de prompt; (`slug`, `version`) duplicado gera `ConflictError`."""
        conflict = f"ja existe o prompt '{prompt.slug}' na versao {prompt.version}"
        details = {"slug": prompt.slug, "version": prompt.version}
        if await self._version_taken(prompt.slug, prompt.version):
            raise ConflictError(conflict, details=details)
        row = PromptRow()
        prompt_apply(row, prompt)
        self._session.add(row)
        await _flush(self._session, conflict=conflict, details=details)
        return prompt_to_domain(row)

    async def get(self, prompt_id: Id) -> PromptTemplate | None:
        """Busca por identificador."""
        row = await self._load(prompt_id)
        return None if row is None else prompt_to_domain(row)

    async def get_by_slug(self, slug: str) -> PromptTemplate | None:
        """Devolve a versao ativa de maior numero para o slug."""
        statement = (
            select(PromptRow)
            .where(PromptRow.slug == slug, PromptRow.is_active.is_(True))
            .order_by(PromptRow.version.desc())
            .limit(1)
        )
        row = await _first(self._session, statement)
        return None if row is None else prompt_to_domain(row)

    async def list(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[PromptTemplate]:
        """Lista prompts por slug (versao mais recente primeiro), aplicando os filtros."""
        statement = (
            select(PromptRow)
            .where(*_conditions(search=search, is_active=is_active))
            .order_by(PromptRow.slug.asc(), PromptRow.version.desc())
            .limit(max(limit, 0))
            .offset(max(offset, 0))
        )
        return [prompt_to_domain(row) for row in await _rows(self._session, statement)]

    async def count(self, **filters: Any) -> int:
        """Conta prompts com os mesmos filtros aceitos por `list`."""
        unknown = sorted(set(filters) - _FILTER_KEYS)
        if unknown:
            raise ValidationError(
                f"filtros nao suportados para prompts: {', '.join(unknown)}",
                details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
            )
        conditions = _conditions(
            search=filters.get("search"),
            is_active=_as_bool(filters.get("is_active"), field="is_active"),
        )
        statement = select(func.count()).select_from(PromptRow).where(*conditions)
        result = await _execute(self._session, statement)
        return int(result.scalar_one())

    async def update(self, prompt: PromptTemplate) -> PromptTemplate:
        """Grava a versao existente do prompt; ausente gera `NotFoundError`."""
        row = await self._require(prompt.id)
        prompt_apply(row, prompt)
        await _flush(
            self._session,
            conflict=f"ja existe o prompt '{prompt.slug}' na versao {prompt.version}",
            details={"slug": prompt.slug, "version": prompt.version, "id": prompt.id},
        )
        return prompt_to_domain(row)

    async def delete(self, prompt_id: Id) -> None:
        """Remove uma versao de prompt; ausente gera `NotFoundError`."""
        row = await self._require(prompt_id)
        try:
            await self._session.delete(row)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao remover o prompt: {exc}",
                details={"id": prompt_id, "error": type(exc).__name__},
            ) from exc
        await _flush(
            self._session,
            conflict="prompt referenciado por outro registro",
            details={"id": prompt_id},
        )

    async def list_versions(self, slug: str) -> builtins.list[PromptTemplate]:
        """Lista todas as versoes do slug, da mais recente para a mais antiga."""
        statement = (
            select(PromptRow).where(PromptRow.slug == slug).order_by(PromptRow.version.desc())
        )
        return [prompt_to_domain(row) for row in await _rows(self._session, statement)]

    async def _load(self, prompt_id: Id) -> PromptRow | None:
        """Carrega a linha pela chave primaria."""
        try:
            return await self._session.get(PromptRow, prompt_id)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao carregar o prompt: {exc}",
                details={"id": prompt_id, "error": type(exc).__name__},
            ) from exc

    async def _require(self, prompt_id: Id) -> PromptRow:
        """Carrega a linha exigindo que ela exista."""
        row = await self._load(prompt_id)
        if row is None:
            raise NotFoundError(f"prompt '{prompt_id}' nao encontrado", details={"id": prompt_id})
        return row

    async def _version_taken(self, slug: str, version: int) -> bool:
        """Informa se o par (`slug`, `version`) ja esta gravado."""
        statement = select(PromptRow.id).where(PromptRow.slug == slug, PromptRow.version == version)
        return await _first(self._session, statement) is not None
