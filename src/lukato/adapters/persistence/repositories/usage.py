"""Repositorio de registros de consumo faturavel (SPEC-0011 secao 4, SPEC-0005).

Implementa a porta `UsageRepository`. A agregacao de custo (`summary` e
`total_since`) e feita **em SQL** com `func.sum`/`func.count`, jamais carregando a
tabela inteira em memoria: e a consulta que alimenta a barra de status do console.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import usage_apply, usage_to_domain
from lukato.adapters.persistence.orm import UsageRecordRow
from lukato.domain.errors import ConflictError, ProviderError, ValidationError
from lukato.domain.models.finops import CostSummary, UsageRecord

__all__ = ["SqlAlchemyUsageRepository"]

_COST_DIGITS: Final[int] = 8
"""Casas decimais do custo agregado (SPEC-0005 secao 2)."""

_GLOBAL_SCOPE: Final[str] = "global"
_MODULE_PREFIX: Final[str] = "module:"
_TENANT_PREFIX: Final[str] = "tenant:"

_FILTER_KEYS: Final[frozenset[str]] = frozenset(
    {"since", "until", "module_slug", "model", "tenant_id"}
)
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
    since: datetime | None = None,
    until: datetime | None = None,
    module_slug: str | None = None,
    model: str | None = None,
    tenant_id: str | None = None,
) -> list[ColumnElement[bool]]:
    """Monta as condicoes WHERE compartilhadas por `list`, `count` e `summary`."""
    clauses: list[ColumnElement[bool]] = []
    if since is not None:
        clauses.append(UsageRecordRow.occurred_at >= since)
    if until is not None:
        clauses.append(UsageRecordRow.occurred_at <= until)
    if module_slug:
        clauses.append(UsageRecordRow.module_slug == module_slug)
    if model:
        clauses.append(UsageRecordRow.model == model)
    if tenant_id:
        clauses.append(UsageRecordRow.tenant_id == tenant_id)
    return clauses


def _scope_condition(scope: str) -> list[ColumnElement[bool]]:
    """Traduz o escopo textual do orcamento em condicao SQL."""
    if scope == _GLOBAL_SCOPE:
        return []
    if scope.startswith(_MODULE_PREFIX):
        return [UsageRecordRow.module_slug == scope[len(_MODULE_PREFIX) :]]
    if scope.startswith(_TENANT_PREFIX):
        return [UsageRecordRow.tenant_id == scope[len(_TENANT_PREFIX) :]]
    raise ValidationError(
        f"escopo de custo invalido: {scope!r}",
        details={"scope": scope, "supported": ["global", "module:<slug>", "tenant:<id>"]},
    )


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


def _filters_from(filters: dict[str, Any]) -> dict[str, Any]:
    """Valida `**filters` de `count`, aceitando os mesmos nomes usados por `list`."""
    unknown = sorted(set(filters) - _FILTER_KEYS - _PAGING_KEYS)
    if unknown:
        raise ValidationError(
            "filtro desconhecido para registros de consumo",
            details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
        )
    return {key: value for key, value in filters.items() if key in _FILTER_KEYS}


class SqlAlchemyUsageRepository:
    """Registros de consumo em SQL; implementa a porta `UsageRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, record: UsageRecord) -> UsageRecord:
        """Insere um registro de consumo e devolve o modelo de dominio gravado."""
        row = UsageRecordRow()
        usage_apply(row, record)
        async with _translate("usage.add"):
            self._session.add(row)
            await self._session.flush()
        return usage_to_domain(row)

    async def list(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        module_slug: str | None = None,
        model: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[UsageRecord]:
        """Lista registros do mais recente para o mais antigo, sempre paginados."""
        bounded_limit, bounded_offset = _page(limit, offset)
        statement = (
            select(UsageRecordRow)
            .where(
                *_conditions(
                    since=since,
                    until=until,
                    module_slug=module_slug,
                    model=model,
                    tenant_id=tenant_id,
                )
            )
            .order_by(UsageRecordRow.occurred_at.desc(), UsageRecordRow.id.desc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("usage.list"):
            result = await self._session.execute(statement)
            rows: Sequence[UsageRecordRow] = result.scalars().all()
        return [usage_to_domain(row) for row in rows]

    async def count(self, **filters: Any) -> int:
        """Conta registros com os mesmos filtros aceitos por `list`."""
        statement = select(func.count()).select_from(UsageRecordRow)
        statement = statement.where(*_conditions(**_filters_from(filters)))
        async with _translate("usage.count"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def summary(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        module_slug: str | None = None,
        tenant_id: str | None = None,
    ) -> CostSummary:
        """Agrega custo e tokens do periodo, por modulo e por modelo, direto no banco.

        `runs` conta execucoes distintas; registros sem `run_id` contam como uma
        execucao propria, exatamente como faz `CostCalculator.summarize`.
        """
        clauses = _conditions(
            since=since, until=until, module_slug=module_slug, tenant_id=tenant_id
        )
        totals_statement = select(
            func.coalesce(func.sum(UsageRecordRow.cost_usd), 0.0),
            func.coalesce(func.sum(UsageRecordRow.total_tokens), 0),
            func.count(func.distinct(UsageRecordRow.run_id)),
            func.coalesce(
                func.sum(case((UsageRecordRow.run_id.is_(None), 1), else_=0)),
                0,
            ),
        ).where(*clauses)
        by_module_statement = (
            select(
                UsageRecordRow.module_slug,
                func.coalesce(func.sum(UsageRecordRow.cost_usd), 0.0),
            )
            .where(*clauses)
            .group_by(UsageRecordRow.module_slug)
        )
        by_model_statement = (
            select(UsageRecordRow.model, func.coalesce(func.sum(UsageRecordRow.cost_usd), 0.0))
            .where(*clauses)
            .group_by(UsageRecordRow.model)
        )

        async with _translate("usage.summary"):
            totals = (await self._session.execute(totals_statement)).one()
            by_module = (await self._session.execute(by_module_statement)).all()
            by_model = (await self._session.execute(by_model_statement)).all()

        total_usd, total_tokens, distinct_runs, orphan_runs = totals
        return CostSummary(
            total_usd=round(float(total_usd or 0.0), _COST_DIGITS),
            total_tokens=int(total_tokens or 0),
            runs=int(distinct_runs or 0) + int(orphan_runs or 0),
            by_module={
                str(slug): round(float(value or 0.0), _COST_DIGITS) for slug, value in by_module
            },
            by_model={
                str(name): round(float(value or 0.0), _COST_DIGITS) for name, value in by_model
            },
        )

    async def total_since(self, since: datetime, *, scope: str = _GLOBAL_SCOPE) -> float:
        """Custo total em USD desde o instante, no escopo informado."""
        statement = select(func.coalesce(func.sum(UsageRecordRow.cost_usd), 0.0)).where(
            UsageRecordRow.occurred_at >= since, *_scope_condition(scope)
        )
        async with _translate("usage.total_since"):
            result = await self._session.execute(statement)
        return round(float(result.scalar_one() or 0.0), _COST_DIGITS)
