"""Casos de uso do historico de execucoes (`AgentRun` e `RunStep`).

Toda invocacao de building block vira um `AgentRun` persistido (SPEC-0001 secao
4): estes casos de uso sao a leitura dessa trilha e o unico ponto que interrompe
uma execucao em andamento.
"""

from __future__ import annotations

from lukato.application.container import Container
from lukato.application.dto import Page, RunFilter
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import ConflictError, NotFoundError
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.run import AgentRun, RunStatus, RunStep
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.types import Id, Json, utcnow

__all__ = ["CancelRun", "GetRun", "GetRunSteps", "ListRuns"]

_logger = get_logger(__name__)

CANCELLABLE_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.PENDING, RunStatus.RUNNING})
"""Estados que ainda admitem cancelamento; os demais sao terminais."""


class _RunUseCase:
    """Base dos casos de uso de execucao: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container

    @staticmethod
    async def _require_run(uow: UnitOfWork, run_id: Id) -> AgentRun:
        """Carrega a execucao ou levanta :class:`NotFoundError`."""
        found = await uow.runs.get(run_id)
        if found is None:
            raise NotFoundError(
                f"Execucao '{run_id}' nao encontrada.",
                details={"run_id": run_id},
            )
        return found


class GetRun(_RunUseCase):
    """Busca uma execucao completa, com os passos ja carregados."""

    async def execute(self, run_id: Id, principal: Principal) -> AgentRun:
        """Devolve a execucao; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.RUN_READ, "ler execucoes")
        async with self._container.uow_factory() as uow:
            return await self._require_run(uow, run_id)


class ListRuns(_RunUseCase):
    """Lista execucoes paginadas, da mais recente para a mais antiga."""

    async def execute(self, filters: RunFilter, principal: Principal) -> Page[AgentRun]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, Permission.RUN_READ, "listar execucoes")
        criteria: Json = {}
        if filters.module_slug:
            criteria["module_slug"] = filters.module_slug
        if filters.status is not None:
            criteria["status"] = filters.status
        if filters.since is not None:
            criteria["since"] = filters.since
        if filters.until is not None:
            criteria["until"] = filters.until
        if filters.tenant_id:
            criteria["tenant_id"] = filters.tenant_id
        async with self._container.uow_factory() as uow:
            items = await uow.runs.list(**criteria, limit=filters.limit, offset=filters.offset)
            total = await uow.runs.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class GetRunSteps(_RunUseCase):
    """Lista os passos de uma execucao em ordem de indice."""

    async def execute(self, run_id: Id, principal: Principal) -> list[RunStep]:
        """Devolve a trilha da execucao; execucao ausente levanta `NotFoundError`."""
        authorize(principal, Permission.RUN_READ, "ler passos de execucoes")
        async with self._container.uow_factory() as uow:
            run = await self._require_run(uow, run_id)
            steps = await uow.runs.list_steps(run.id)
        return list(steps) if steps else list(run.steps)


class CancelRun(_RunUseCase):
    """Cancela uma execucao ainda em `PENDING` ou `RUNNING`."""

    async def execute(self, run_id: Id, principal: Principal) -> AgentRun:
        """Marca a execucao como `CANCELLED`; estado terminal levanta `ConflictError`."""
        authorize(principal, Permission.MODULE_INVOKE, "cancelar execucoes")
        async with self._container.uow_factory() as uow:
            run = await self._require_run(uow, run_id)
            if run.status not in CANCELLABLE_STATUSES:
                raise ConflictError(
                    f"A execucao '{run_id}' esta em '{run.status.value}' e nao pode "
                    f"mais ser cancelada.",
                    details={
                        "run_id": run_id,
                        "status": run.status.value,
                        "cancellable": sorted(item.value for item in CANCELLABLE_STATUSES),
                    },
                )
            now = utcnow()
            cancelled = run.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "error": f"Execucao cancelada por '{principal.subject}'.",
                    "finished_at": now,
                    "updated_at": now,
                }
            )
            stored = await uow.runs.update(cancelled)
            await uow.commit()
        _logger.info(
            "run_cancelled", run_id=run_id, module=stored.module_slug, actor=principal.subject
        )
        return stored
