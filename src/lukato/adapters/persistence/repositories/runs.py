"""Repositorio SQLAlchemy do historico de execucoes (tabelas `agent_runs`/`run_steps`).

Implementa a porta :class:`~lukato.domain.ports.repositories.RunRepository`. Regras
proprias deste agregado:

* `add_step` atribui `position` como o **proximo indice** do run (0 quando ainda nao
  ha passos), ignorando o `index` que venha preenchido no modelo;
* `list` devolve as execucoes da mais recente para a mais antiga, **sem** os passos —
  quem precisa da trilha completa usa `get` ou `list_steps`;
* `update` reescreve os contadores de token e o custo a partir do modelo de dominio e
  concilia os passos que o chamador acumulou em `AgentRun.steps` (trilha append-only:
  nada e removido do banco).
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from lukato.adapters.persistence.mappers import (
    run_apply,
    run_step_apply,
    run_step_to_domain,
    run_to_domain,
)
from lukato.adapters.persistence.orm import AgentRunRow, RunStepRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.run import AgentRun, RunStatus, RunStep
from lukato.domain.types import Id

__all__ = ["SqlAlchemyRunRepository"]

_RowT = TypeVar("_RowT")

_FILTER_KEYS: Final[frozenset[str]] = frozenset(
    {"module_slug", "status", "since", "until", "tenant_id", "limit", "offset"}
)


async def _execute(session: AsyncSession, statement: Any) -> Any:
    """Executa a instrucao traduzindo qualquer falha do driver em `ProviderError`."""
    try:
        return await session.execute(statement)
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha ao consultar o historico de execucoes: {exc}",
            details={"error": type(exc).__name__},
        ) from exc


async def _rows(session: AsyncSession, statement: Select[tuple[_RowT]]) -> builtins.list[_RowT]:
    """Devolve todas as linhas escalares da consulta."""
    result = await _execute(session, statement)
    return list(result.scalars().all())


async def _flush(session: AsyncSession, *, conflict: str, details: dict[str, Any]) -> None:
    """Descarrega a sessao convertendo os erros do driver na hierarquia do dominio."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(conflict, details={**details, "error": str(exc.orig)}) from exc
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha ao gravar a execucao: {exc}",
            details={**details, "error": type(exc).__name__},
        ) from exc


def _as_status(value: Any) -> RunStatus | None:
    """Normaliza o filtro `status` para o enum do dominio."""
    if value is None or isinstance(value, RunStatus):
        return value
    try:
        return RunStatus(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"status invalido: {value!r}",
            details={"field": "status", "allowed": [item.value for item in RunStatus]},
        ) from exc


def _as_datetime(value: Any, *, field: str) -> datetime | None:
    """Normaliza um filtro temporal vindo de `**filters`."""
    if value is None or isinstance(value, datetime):
        return value
    raise ValidationError(
        f"{field} deve ser um datetime, recebido {type(value).__name__}",
        details={"field": field},
    )


def _conditions(
    *,
    module_slug: str | None,
    status: RunStatus | None,
    since: datetime | None,
    until: datetime | None,
    tenant_id: str | None,
) -> builtins.list[ColumnElement[bool]]:
    """Traduz os filtros publicos em predicados SQL."""
    conditions: builtins.list[ColumnElement[bool]] = []
    if module_slug:
        conditions.append(AgentRunRow.module_slug == module_slug)
    if status is not None:
        conditions.append(AgentRunRow.status == status.value)
    if since is not None:
        conditions.append(AgentRunRow.created_at >= since)
    if until is not None:
        conditions.append(AgentRunRow.created_at <= until)
    if tenant_id:
        conditions.append(AgentRunRow.tenant_id == tenant_id)
    return conditions


class SqlAlchemyRunRepository:
    """Historico persistido de `AgentRun` e `RunStep`; implementa `RunRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit pertence ao `UnitOfWork`)."""
        self._session = session

    async def add(self, run: AgentRun) -> AgentRun:
        """Insere a execucao e os passos que ja acompanham o modelo."""
        details = {"id": run.id, "module_slug": run.module_slug}
        conflict = f"ja existe uma execucao com o id '{run.id}'"
        if await self._load(run.id) is not None:
            raise ConflictError(conflict, details=details)
        row = AgentRunRow()
        run_apply(row, run)
        self._session.add(row)
        await _flush(self._session, conflict=conflict, details=details)
        step_rows = self._attach_steps(run.id, run.steps)
        if step_rows:
            await _flush(
                self._session,
                conflict="falha de integridade ao gravar os passos da execucao",
                details=details,
            )
        return run_to_domain(row, steps=step_rows)

    async def get(self, run_id: Id) -> AgentRun | None:
        """Busca a execucao com os passos ja carregados em ordem de indice."""
        row = await self._load(run_id)
        if row is None:
            return None
        return run_to_domain(row, steps=await self._step_rows(run_id))

    async def list(
        self,
        *,
        module_slug: str | None = None,
        status: RunStatus | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[AgentRun]:
        """Lista execucoes da mais recente para a mais antiga, sem carregar os passos."""
        statement = (
            select(AgentRunRow)
            .where(
                *_conditions(
                    module_slug=module_slug,
                    status=status,
                    since=since,
                    until=until,
                    tenant_id=tenant_id,
                )
            )
            .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
            .limit(max(limit, 0))
            .offset(max(offset, 0))
        )
        return [run_to_domain(row) for row in await _rows(self._session, statement)]

    async def count(self, **filters: Any) -> int:
        """Conta execucoes com os mesmos filtros aceitos por `list`."""
        unknown = sorted(set(filters) - _FILTER_KEYS)
        if unknown:
            raise ValidationError(
                f"filtros nao suportados para execucoes: {', '.join(unknown)}",
                details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
            )
        conditions = _conditions(
            module_slug=filters.get("module_slug"),
            status=_as_status(filters.get("status")),
            since=_as_datetime(filters.get("since"), field="since"),
            until=_as_datetime(filters.get("until"), field="until"),
            tenant_id=filters.get("tenant_id"),
        )
        statement = select(func.count()).select_from(AgentRunRow).where(*conditions)
        result = await _execute(self._session, statement)
        return int(result.scalar_one())

    async def update(self, run: AgentRun) -> AgentRun:
        """Grava o estado final da execucao, recalculando usage e custo pelo dominio."""
        row = await self._require(run.id)
        run_apply(row, run)
        details = {"id": run.id, "module_slug": run.module_slug}
        if run.steps:
            await self._reconcile_steps(run)
        await _flush(
            self._session,
            conflict="falha de integridade ao gravar a execucao",
            details=details,
        )
        return run_to_domain(row, steps=await self._step_rows(run.id))

    async def add_step(self, step: RunStep) -> RunStep:
        """Anexa um passo a execucao, atribuindo o proximo indice disponivel."""
        await self._require(step.run_id)
        row = RunStepRow()
        run_step_apply(row, step)
        row.run_id = step.run_id
        row.position = await self._next_position(step.run_id)
        self._session.add(row)
        await _flush(
            self._session,
            conflict="falha de integridade ao gravar o passo da execucao",
            details={"run_id": step.run_id, "step_id": step.id},
        )
        return run_step_to_domain(row)

    async def list_steps(self, run_id: Id) -> builtins.list[RunStep]:
        """Lista os passos da execucao em ordem de indice."""
        return [run_step_to_domain(row) for row in await self._step_rows(run_id)]

    # ----------------------------------------------------------------- #
    # Internos
    # ----------------------------------------------------------------- #

    def _attach_steps(self, run_id: Id, steps: Iterable[RunStep]) -> builtins.list[RunStepRow]:
        """Cria as linhas dos passos informados, ancorando-as no run indicado."""
        rows: builtins.list[RunStepRow] = []
        for position, step in enumerate(steps):
            row = RunStepRow()
            run_step_apply(row, step)
            row.run_id = run_id
            row.position = step.index if step.index > 0 else position
            self._session.add(row)
            rows.append(row)
        return rows

    async def _reconcile_steps(self, run: AgentRun) -> None:
        """Atualiza os passos ja gravados e insere os novos, preservando a trilha."""
        stored = {row.id: row for row in await self._step_rows(run.id)}
        position = max((row.position for row in stored.values()), default=-1)
        for step in run.steps:
            row = stored.get(step.id)
            if row is not None:
                kept = row.position
                run_step_apply(row, step)
                row.position = kept
            else:
                row = RunStepRow()
                run_step_apply(row, step)
                row.position = step.index if step.index > position else position + 1
                self._session.add(row)
            row.run_id = run.id
            position = max(position, row.position)

    async def _next_position(self, run_id: Id) -> int:
        """Calcula o proximo indice livre da trilha do run."""
        statement = select(func.max(RunStepRow.position)).where(RunStepRow.run_id == run_id)
        result = await _execute(self._session, statement)
        current = result.scalar()
        return 0 if current is None else int(current) + 1

    async def _step_rows(self, run_id: Id) -> builtins.list[RunStepRow]:
        """Carrega as linhas de passo do run em ordem de indice."""
        statement = (
            select(RunStepRow)
            .where(RunStepRow.run_id == run_id)
            .order_by(RunStepRow.position.asc())
        )
        return await _rows(self._session, statement)

    async def _load(self, run_id: Id) -> AgentRunRow | None:
        """Carrega a linha da execucao pela chave primaria."""
        try:
            return await self._session.get(AgentRunRow, run_id)
        except SQLAlchemyError as exc:
            raise ProviderError(
                f"falha ao carregar a execucao: {exc}",
                details={"id": run_id, "error": type(exc).__name__},
            ) from exc

    async def _require(self, run_id: Id) -> AgentRunRow:
        """Carrega a linha da execucao exigindo que ela exista."""
        row = await self._load(run_id)
        if row is None:
            raise NotFoundError(f"execucao '{run_id}' nao encontrada", details={"id": run_id})
        return row
