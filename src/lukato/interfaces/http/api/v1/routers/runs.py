"""Rotas do historico de execucoes (`AgentRun` e `RunStep`).

Toda invocacao de building block deixa um `AgentRun` persistido, com os passos na
ordem em que aconteceram — guardrail de entrada, prompt, LLM, ferramentas,
guardrail de saida — e o consumo, o custo e a latencia de cada um (SPEC-0001
secao 4). Estas rotas sao a leitura dessa trilha: a auditoria de negocio da
plataforma, e a resposta para "o que exatamente aconteceu naquela chamada?".

Duas escolhas de contrato merecem explicacao:

* a **listagem** devolve o resumo (`RunSummaryOut`), sem entrada, saida nem
  passos. Uma pagina de cinquenta execucoes com a trilha inteira de cada uma
  seria megabytes de JSON para preencher uma tabela de sete colunas;
* o **detalhe** devolve tudo, e `/{run_id}/steps` devolve so a trilha, para a
  UI que ja tem o cabecalho na tela e quer apenas expandir os passos.

`POST /{run_id}/cancel` e o unico ponto que interrompe uma execucao em andamento.
Ele exige `module:invoke`, nao `run:read`: cancelar e um ato de operacao sobre a
execucao, nao uma leitura do historico.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, status

from lukato.application.dto import RunFilter
from lukato.application.use_cases.runs import CancelRun, GetRun, GetRunSteps, ListRuns
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.run import RunStatus
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.common import Page, error_responses
from lukato.interfaces.http.schemas.runs import RunOut, RunStepOut, RunSummaryOut

__all__ = ["router"]

router = APIRouter(prefix="/runs", tags=["execucoes"])
"""Rotas da trilha de execucoes, sob `/api/v1/runs`."""

_ReaderDep = Annotated[Principal, Depends(require(Permission.RUN_READ))]
"""Ler o historico exige `run:read`."""

_OperatorDep = Annotated[Principal, Depends(require(Permission.MODULE_INVOKE))]
"""Cancelar uma execucao e operacao sobre ela: exige `module:invoke`."""

_RunPath = Annotated[str, Path(description="Identificador da execucao (`AgentRun.id`).")]
"""Referencia da execucao na URL."""

_READ_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404)
"""Erros das rotas que resolvem uma execucao existente."""


@router.get(
    "",
    response_model=Page[RunSummaryOut],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Lista execucoes",
    description=(
        "Pagina o historico da mais recente para a mais antiga, filtrando por "
        "modulo, desfecho, janela temporal e tenant. O item da listagem e o "
        "**resumo**: consumo, custo, latencia e a contagem de passos, sem a trilha "
        "completa. Use `GET /api/v1/runs/{run_id}` para o detalhe."
    ),
)
async def list_runs(
    container: ContainerDep,
    principal: _ReaderDep,
    pagination: PaginationDep,
    module_slug: Annotated[
        str | None, Query(description="Filtra pelas execucoes de um modulo.")
    ] = None,
    run_status: Annotated[
        RunStatus | None,
        Query(alias="status", description="Filtra pelo desfecho da execucao."),
    ] = None,
    since: Annotated[
        datetime | None, Query(description="Inicio da janela, em ISO-8601 (inclusivo).")
    ] = None,
    until: Annotated[
        datetime | None, Query(description="Fim da janela, em ISO-8601 (exclusivo).")
    ] = None,
    tenant_id: Annotated[str | None, Query(description="Filtra por tenant.")] = None,
) -> Page[RunSummaryOut]:
    """Devolve a pagina de execucoes que satisfazem os filtros."""
    filters = RunFilter(
        module_slug=module_slug,
        status=run_status,
        since=since,
        until=until,
        tenant_id=tenant_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListRuns(container).execute(filters, principal)
    return Page.from_result(result, RunSummaryOut.from_domain)


@router.get(
    "/{run_id}",
    response_model=RunOut,
    status_code=status.HTTP_200_OK,
    responses=_READ_ERRORS,
    summary="Busca uma execucao",
    description=(
        "Devolve a execucao completa: entrada, saida final, consumo, custo, "
        "`trace_id` e a trilha de passos na ordem em que aconteceram. Execucao "
        "inexistente responde `404`."
    ),
)
async def get_run(container: ContainerDep, principal: _ReaderDep, run_id: _RunPath) -> RunOut:
    """Devolve a execucao pedida, com a trilha inteira."""
    run = await GetRun(container).execute(run_id, principal)
    return RunOut.from_domain(run)


@router.get(
    "/{run_id}/steps",
    response_model=Page[RunStepOut],
    status_code=status.HTTP_200_OK,
    responses=_READ_ERRORS,
    summary="Lista os passos de uma execucao",
    description=(
        "Trilha da execucao em ordem de indice: guardrail de entrada, renderizacao "
        "do prompt, chamadas de LLM e de ferramentas, guardrail de saida. Cada "
        "passo traz entrada, saida, consumo, custo, latencia e o erro, quando "
        "houve. A trilha e devolvida inteira, no envelope de lista da API."
    ),
)
async def list_run_steps(
    container: ContainerDep, principal: _ReaderDep, run_id: _RunPath
) -> Page[RunStepOut]:
    """Devolve os passos da execucao, do primeiro ao ultimo.

    A trilha pertence a uma unica execucao e volta inteira: `limit` reflete o
    tamanho do que foi devolvido, nao uma janela pedida pelo cliente.
    """
    steps = await GetRunSteps(container).execute(run_id, principal)
    return Page.of((RunStepOut.from_domain(step) for step in steps), limit=max(len(steps), 1))


@router.post(
    "/{run_id}/cancel",
    response_model=RunOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 404, 409),
    summary="Cancela uma execucao",
    description=(
        "Marca como `cancelled` uma execucao ainda em `pending` ou `running`, "
        "registrando quem cancelou. Execucao em estado terminal (`succeeded`, "
        "`failed`, `blocked` ou ja `cancelled`) responde `409`: o historico e "
        "auditoria e nao pode ser reescrito depois do fato."
    ),
)
async def cancel_run(container: ContainerDep, principal: _OperatorDep, run_id: _RunPath) -> RunOut:
    """Cancela a execucao e devolve o registro atualizado."""
    cancelled = await CancelRun(container).execute(run_id, principal)
    return RunOut.from_domain(cancelled)
