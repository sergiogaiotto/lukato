"""Repositorio de orcamentos de custo por escopo (SPEC-0011 secao 4, SPEC-0005).

Implementa a porta `BudgetRepository`. `list` nao recebe paginacao no contrato, mas
nenhum `select` do lukato pode ficar sem `LIMIT` (SPEC-0011 secao 3.10): aplica-se o
teto de seguranca :data:`MAX_BUDGETS`, folgado para um recurso administrativo.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import ColumnElement, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import budget_apply, budget_to_domain
from lukato.adapters.persistence.orm import BudgetRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError
from lukato.domain.models.finops import Budget
from lukato.domain.types import Id

__all__ = ["MAX_BUDGETS", "SqlAlchemyBudgetRepository"]

MAX_BUDGETS: Final[int] = 500
"""Teto de seguranca da listagem de orcamentos (o contrato nao expoe paginacao)."""


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


class SqlAlchemyBudgetRepository:
    """Orcamentos em SQL; implementa a porta `BudgetRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, budget: Budget) -> Budget:
        """Insere o orcamento e devolve o modelo de dominio gravado."""
        row = BudgetRow()
        budget_apply(row, budget)
        async with _translate("budgets.add"):
            self._session.add(row)
            await self._session.flush()
        return budget_to_domain(row)

    async def get(self, budget_id: Id) -> Budget | None:
        """Busca o orcamento por identificador; `None` quando nao existe."""
        row = await self._row(budget_id, operation="budgets.get")
        return None if row is None else budget_to_domain(row)

    async def list(
        self, *, scope: str | None = None, is_active: bool | None = None
    ) -> builtins.list[Budget]:
        """Lista orcamentos por escopo e situacao, do mais recente para o mais antigo."""
        clauses: list[ColumnElement[bool]] = []
        if scope:
            clauses.append(BudgetRow.scope == scope)
        if is_active is not None:
            clauses.append(BudgetRow.is_active.is_(is_active))
        statement = (
            select(BudgetRow)
            .where(*clauses)
            .order_by(BudgetRow.created_at.desc(), BudgetRow.id.desc())
            .limit(MAX_BUDGETS)
        )
        async with _translate("budgets.list"):
            result = await self._session.execute(statement)
            rows: Sequence[BudgetRow] = result.scalars().all()
        return [budget_to_domain(row) for row in rows]

    async def update(self, budget: Budget) -> Budget:
        """Grava o orcamento existente; ausente gera `NotFoundError`."""
        row = await self._require(budget.id, operation="budgets.update")
        budget_apply(row, budget)
        async with _translate("budgets.update"):
            await self._session.flush()
        return budget_to_domain(row)

    async def delete(self, budget_id: Id) -> None:
        """Remove o orcamento; ausente gera `NotFoundError`."""
        row = await self._require(budget_id, operation="budgets.delete")
        async with _translate("budgets.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def _row(self, budget_id: Id, *, operation: str) -> BudgetRow | None:
        """Carrega a linha bruta do orcamento, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(BudgetRow, budget_id)

    async def _require(self, budget_id: Id, *, operation: str) -> BudgetRow:
        """Carrega a linha do orcamento ou levanta `NotFoundError`."""
        row = await self._row(budget_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"orcamento nao encontrado: {budget_id}",
                details={"budget_id": budget_id},
            )
        return row
