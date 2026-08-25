"""Repositorio SQLAlchemy das politicas de guardrail (tabela `guardrail_policies`).

Implementa a porta :class:`~lukato.domain.ports.repositories.GuardrailRepository`.
As regras da politica vivem na coluna JSON `rules` e sao reconstruidas pelos mappers,
de modo que o dominio nunca ve uma linha ORM.
"""

from __future__ import annotations

import builtins
from typing import Any, Final, TypeVar

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from lukato.adapters.persistence.mappers import guardrail_apply, guardrail_to_domain
from lukato.adapters.persistence.orm import GuardrailPolicyRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.guardrail import GuardrailPolicy, GuardrailStage
from lukato.domain.types import Id

__all__ = ["SqlAlchemyGuardrailRepository"]

_RowT = TypeVar("_RowT")

_LIKE_ESCAPE: Final[str] = "\\"
_FILTER_KEYS: Final[frozenset[str]] = frozenset(
    {"stage", "is_active", "search", "limit", "offset"}
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
            f"falha ao consultar as politicas de guardrail: {exc}",
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
            f"falha ao gravar a politica de guardrail: {exc}",
            details={**details, "error": type(exc).__name__},
        ) from exc


def _as_stage(value: Any) -> GuardrailStage | None:
    """Normaliza o filtro `stage` para o enum do dominio."""
    if value is None or isinstance(value, GuardrailStage):
        return value
    try:
        return GuardrailStage(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"stage invalido: {value!r}",
            details={"field": "stage", "allowed": [item.value for item in GuardrailStage]},
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
    *, stage: GuardrailStage | None, is_active: bool | None, search: str | None
) -> builtins.list[ColumnElement[bool]]:
    """Traduz os filtros publicos em predicados SQL."""
    conditions: builtins.list[ColumnElement[bool]] = []
    if stage is not None:
        conditions.append(GuardrailPolicyRow.stage == stage.value)
    if is_active is not None:
        conditions.append(GuardrailPolicyRow.is_active.is_(is_active))
    if search:
        pattern = _like(search)
        conditions.append(
            or_(
                GuardrailPolicyRow.slug.ilike(pattern, escape=_LIKE_ESCAPE),
                GuardrailPolicyRow.name.ilike(pattern, escape=_LIKE_ESCAPE),
                GuardrailPolicyRow.description.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        )
    return conditions


class SqlAlchemyGuardrailRepository:
    """Politicas persistidas de guardrail; implementa `GuardrailRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit pertence ao `UnitOfWork`)."""
        self._session = session

    async def add(self, policy: GuardrailPolicy) -> GuardrailPolicy:
        """Insere a politica; slug duplicado gera `ConflictError`."""
        conflict = f"ja existe uma politica de guardrail com o slug '{policy.slug}'"
        details = {"slug": policy.slug}
        if await self._slug_taken(policy.slug):
            raise ConflictError(conflict, details=details)
        row = GuardrailPolicyRow()
        guardrail_apply(row, policy)
        self._session.add(row)
        await _flush(self._session, conflict=conflict, details=details)
        return guardrail_to_domain(row)

    async def get(self, policy_id: Id) -> GuardrailPolicy | None:
        """Busca por identificador."""
        row = await self._load(policy_id)
        return None if row is None else guardrail_to_domain(row)

    async def get_by_slug(self, slug: str) -> GuardrailPolicy | None:
        """Busca pelo slug unico da politica."""
        statement = select(GuardrailPolicyRow).where(GuardrailPolicyRow.slug == slug)
        row = await _first(self._session, statement)
        return None if row is None else guardrail_to_domain(row)

    async def list(
        self,
        *,
        stage: GuardrailStage | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[GuardrailPolicy]:
        """Lista politicas por slug, aplicando os filtros e a paginacao informados."""
        statement = (
            select(GuardrailPolicyRow)
            .where(*_conditions(stage=stage, is_active=is_active, search=search))
            .order_by(GuardrailPolicyRow.slug.asc())
            .limit(max(limit, 0))
            .offset(max(offset, 0))
        )
        return [guardrail_to_domain(row) for row in await _rows(self._session, statement)]

    async def count(self, **filters: Any) -> int:
        """Conta politicas com os mesmos filtros aceitos por `list`."""
        unknown = sorted(set(filters) - _FILTER_KEYS)
        if unknown:
            raise ValidationError(
                f"filtros nao suportados para guardrails: {', '.join(unknown)}",
                details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
            )
        conditions = _conditions(
            stage=_as_stage(filters.get("stage")),
            is_active=_as_bool(filters.get("is_active"), field="is_active"),
            search=filters.get("search"),
        )
        statement = select(func.count()).select_from(GuardrailPolicyRow).where(*conditions)
        result = await _execute(self._session, statement)
        return int(result.scalar_one())

    async def update(self, policy: GuardrailPolicy) -> GuardrailPolicy:
        """Grava a politica existente; ausente gera `NotFoundError`."""
        row = await self._require(policy.id)
        guardrail_apply(row, policy)
        await _flush(
            self._session,
            conflict=f"ja existe uma politica de guardrail com o slug '{policy.slug}'",
            details={"slug": policy.slug, "id": policy.id},
        )
        return guardrail_to_domain(row)

    async def delete(self, policy_id: Id) -> None:
        """Remove a politica pelo identificador; ausente gera `NotFoundError`."""
        row = await self._require(policy_id)
        try:
            await self._session.delete(row)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao remover a politica de guardrail: {exc}",
                details={"id": policy_id, "error": type(exc).__name__},
            ) from exc
        await _flush(
            self._session,
            conflict="politica de guardrail referenciada por outro registro",
            details={"id": policy_id},
        )

    async def _load(self, policy_id: Id) -> GuardrailPolicyRow | None:
        """Carrega a linha pela chave primaria."""
        try:
            return await self._session.get(GuardrailPolicyRow, policy_id)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao carregar a politica de guardrail: {exc}",
                details={"id": policy_id, "error": type(exc).__name__},
            ) from exc

    async def _require(self, policy_id: Id) -> GuardrailPolicyRow:
        """Carrega a linha exigindo que ela exista."""
        row = await self._load(policy_id)
        if row is None:
            raise NotFoundError(
                f"politica de guardrail '{policy_id}' nao encontrada", details={"id": policy_id}
            )
        return row

    async def _slug_taken(self, slug: str) -> bool:
        """Informa se o slug ja pertence a outra politica."""
        statement = select(GuardrailPolicyRow.id).where(GuardrailPolicyRow.slug == slug)
        return await _first(self._session, statement) is not None
