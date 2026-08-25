"""Rotas de `/api/v1/finops` — custo, consumo, tabela de precos e orcamentos.

Toda chamada de LLM da plataforma vira um `UsageRecord` com tokens e custo
(SPEC-0005 secao 3). Este recurso e a leitura desse registro e o lugar onde o
freio e configurado: o resumo alimenta a barra de status do console, a serie
alimenta os graficos e o orcamento decide se a proxima invocacao acontece.

Duas escolhas de contrato desta borda merecem explicacao:

* **`since` e `until` aceitam janela relativa.** A barra de status pede
  `?since=24h` a cada atualizacao (SPEC-0005 secao 5); obrigar o console a
  calcular e formatar um instante ISO-8601 a cada segundo so cria oportunidade de
  divergencia de relogio. :func:`parse_moment` resolve `24h`, `7d`, `30d` — e as
  formas por extenso — contra o relogio **do servidor**, e continua aceitando
  ISO-8601 para quem quer um recorte exato;
* **a serie nunca tem buraco no eixo do tempo.** `GET /series` devolve todo balde
  do intervalo, inclusive os de custo zero. Um ponto ausente seria lido pelo
  grafico como "nao sei", quando o fato e "nao gastou".

Nenhuma rota toca repositorio: toda operacao passa por um caso de uso de
:mod:`lukato.application.use_cases.finops`, construido com o `Container` injetado
por :func:`lukato.interfaces.http.deps.get_container`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query, Response, status

from lukato.application.use_cases.finops import (
    BudgetFilter,
    CostFilter,
    CreateBudget,
    DeleteBudget,
    GetBudget,
    GetBudgetStatus,
    GetCostSeries,
    GetCostSummary,
    GetPrices,
    ListBudgets,
    ListUsage,
    SeriesRequest,
    UpdateBudget,
    UpdatePrices,
    UsageFilter,
)
from lukato.domain.errors import ValidationError
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.types import utcnow
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.common import Page, error_responses
from lukato.interfaces.http.schemas.finops import (
    BudgetCreate,
    BudgetOut,
    BudgetStatusOut,
    BudgetUpdate,
    CostSeriesResponse,
    CostSummaryOut,
    PriceTableOut,
    PriceTableUpdate,
    UsageRecordOut,
)

__all__ = ["parse_moment", "router"]

router = APIRouter(prefix="/finops", tags=["finops"])
"""Roteador do recurso de FinOps (SPEC-0000 secao 11)."""

_Reader = Annotated[Principal, Depends(require(Permission.FINOPS_READ))]
"""Principal que ja provou ter `finops:read`."""

_Writer = Annotated[Principal, Depends(require(Permission.FINOPS_WRITE))]
"""Principal que ja provou ter `finops:write`."""

_BudgetId = Annotated[
    str,
    Path(min_length=1, description="Identificador do orcamento (`Budget.id`)."),
]
"""Referencia do orcamento recebida no caminho da rota."""

_BUDGET_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404)
"""Erros das rotas que resolvem um orcamento existente."""

_SECONDS_PER_MINUTE: Final[float] = 60.0
_SECONDS_PER_HOUR: Final[float] = 3_600.0
_SECONDS_PER_DAY: Final[float] = 86_400.0
_SECONDS_PER_WEEK: Final[float] = 7.0 * _SECONDS_PER_DAY

_RELATIVE_WINDOW: Final[re.Pattern[str]] = re.compile(
    r"^-?\s*(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-zç]+)$",
    re.IGNORECASE,
)
"""Forma relativa aceita em `since`/`until`: quantidade seguida de unidade."""

_UNIT_SECONDS: Final[dict[str, float]] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "seg": 1.0,
    "segs": 1.0,
    "segundo": 1.0,
    "segundos": 1.0,
    "m": _SECONDS_PER_MINUTE,
    "min": _SECONDS_PER_MINUTE,
    "mins": _SECONDS_PER_MINUTE,
    "minute": _SECONDS_PER_MINUTE,
    "minutes": _SECONDS_PER_MINUTE,
    "minuto": _SECONDS_PER_MINUTE,
    "minutos": _SECONDS_PER_MINUTE,
    "h": _SECONDS_PER_HOUR,
    "hr": _SECONDS_PER_HOUR,
    "hrs": _SECONDS_PER_HOUR,
    "hour": _SECONDS_PER_HOUR,
    "hours": _SECONDS_PER_HOUR,
    "hora": _SECONDS_PER_HOUR,
    "horas": _SECONDS_PER_HOUR,
    "d": _SECONDS_PER_DAY,
    "day": _SECONDS_PER_DAY,
    "days": _SECONDS_PER_DAY,
    "dia": _SECONDS_PER_DAY,
    "dias": _SECONDS_PER_DAY,
    "w": _SECONDS_PER_WEEK,
    "week": _SECONDS_PER_WEEK,
    "weeks": _SECONDS_PER_WEEK,
    "sem": _SECONDS_PER_WEEK,
    "semana": _SECONDS_PER_WEEK,
    "semanas": _SECONDS_PER_WEEK,
}
"""Unidades aceitas na janela relativa, em segundos."""

_NOW_WORDS: Final[frozenset[str]] = frozenset({"now", "agora"})
"""Palavras que designam o instante corrente do servidor."""

_MOMENT_EXAMPLES: Final[str] = "24h, 7d, 30d, 12 horas, 2026-08-25T00:00:00Z"
"""Exemplos exibidos na documentacao e na mensagem de erro."""


def parse_moment(raw: str | None, *, field: str, now: datetime | None = None) -> datetime | None:
    """Le um instante de query string: ISO-8601, janela relativa ou `now`.

    Aceita `24h`, `7d`, `30d` (e as formas por extenso, como `12 horas`), com ou
    sem o sinal negativo que algumas interfaces enviam, resolvendo sempre para
    tras a partir do relogio do servidor. Aceita tambem ISO-8601 completo, com ou
    sem fuso — instante ingenuo e assumido em UTC, a mesma leitura que a camada de
    aplicacao faz. Valor ausente ou em branco devolve `None`, o que significa "sem
    recorte" e deixa o caso de uso aplicar a janela padrao.

    Formato irreconhecivel levanta :class:`~lukato.domain.errors.ValidationError`
    (HTTP 422) dizendo o que era esperado — silenciar o erro devolveria um recorte
    que o chamador nao pediu.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    reference = now or utcnow()
    if candidate.casefold() in _NOW_WORDS:
        return reference

    relative = _RELATIVE_WINDOW.fullmatch(candidate)
    if relative is not None:
        unit = _UNIT_SECONDS.get(relative["unit"].casefold())
        if unit is not None:
            amount = float(relative["amount"].replace(",", "."))
            return reference - timedelta(seconds=amount * unit)

    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"Instante invalido em '{field}': {candidate!r}. Use ISO-8601 ou uma "
            f"janela relativa ({_MOMENT_EXAMPLES}).",
            details={
                "field": field,
                "value": candidate,
                "examples": _MOMENT_EXAMPLES.split(", "),
                "units": sorted(_UNIT_SECONDS),
            },
        ) from exc
    return parsed


_Since = Annotated[
    str | None,
    Query(
        description=(
            "Inicio da janela (inclusivo). Aceita ISO-8601 ou janela relativa "
            f"({_MOMENT_EXAMPLES}); ausente usa a janela padrao da rota."
        ),
        examples=["24h", "7d", "2026-08-25T00:00:00Z"],
    ),
]
"""Inicio da janela, em ISO-8601 ou na forma relativa."""

_Until = Annotated[
    str | None,
    Query(
        description=(
            "Fim da janela (exclusivo). Mesmo formato de `since`; ausente usa o "
            "instante corrente do servidor."
        ),
        examples=["now", "2026-08-26T00:00:00Z"],
    ),
]
"""Fim da janela, em ISO-8601 ou na forma relativa."""

_ModuleSlug = Annotated[str | None, Query(description="Restringe ao consumo de um modulo.")]
"""Filtro por modulo consumidor."""

_TenantId = Annotated[str | None, Query(description="Restringe ao consumo de um inquilino.")]
"""Filtro por inquilino."""


# ---------------------------------------------------------------------------
# Custo e consumo
# ---------------------------------------------------------------------------
@router.get(
    "/summary",
    response_model=CostSummaryOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 422),
    summary="Resumo de custo do periodo",
    description=(
        "Agrega o custo da janela por modulo e por modelo, com o total em USD, os "
        "tokens consumidos e a quantidade de execucoes. E a fonte da barra de status do "
        "console, que pede `?since=24h` — a forma relativa e resolvida contra o relogio "
        "do servidor, entao nao ha divergencia entre o que a barra mostra e o que o "
        "banco registrou."
    ),
)
async def get_cost_summary(
    container: ContainerDep,
    principal: _Reader,
    since: _Since = None,
    until: _Until = None,
    module_slug: _ModuleSlug = None,
    tenant_id: _TenantId = None,
) -> CostSummaryOut:
    """Devolve o `CostSummary` do recorte pedido."""
    now = utcnow()
    filters = CostFilter(
        since=parse_moment(since, field="since", now=now),
        until=parse_moment(until, field="until", now=now),
        module_slug=module_slug,
        tenant_id=tenant_id,
    )
    summary = await GetCostSummary(container).execute(filters, principal)
    return CostSummaryOut.from_domain(summary)


@router.get(
    "/series",
    response_model=CostSeriesResponse,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 422),
    summary="Serie temporal de custo",
    description=(
        "Distribui o custo da janela em baldes de hora ou de dia, para os graficos do "
        "console. **Todo** balde do intervalo aparece na resposta, inclusive os sem "
        "consumo: um zero explicito significa 'nao gastou', enquanto um ponto ausente "
        "seria lido como 'sem dado'. Sem `since`, a janela padrao e de 24 horas em "
        "`bucket=hour` e de 30 dias em `bucket=day`; uma janela que produza pontos "
        "demais responde `422` pedindo `bucket=day`."
    ),
)
async def get_cost_series(
    container: ContainerDep,
    principal: _Reader,
    bucket: Annotated[
        Literal["hour", "day"],
        Query(description="Granularidade dos baldes da serie."),
    ] = "hour",
    since: _Since = None,
    until: _Until = None,
    module_slug: _ModuleSlug = None,
    tenant_id: _TenantId = None,
) -> CostSeriesResponse:
    """Devolve a serie continua de custo do periodo."""
    now = utcnow()
    request = SeriesRequest(
        bucket=bucket,
        since=parse_moment(since, field="since", now=now),
        until=parse_moment(until, field="until", now=now),
        module_slug=module_slug,
        tenant_id=tenant_id,
    )
    points = await GetCostSeries(container).execute(request, principal)
    return CostSeriesResponse.from_result(bucket, points)


@router.get(
    "/usage",
    response_model=Page[UsageRecordOut],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 422),
    summary="Lista registros de consumo",
    description=(
        "Pagina os registros faturaveis, do mais recente para o mais antigo, filtrando "
        "por janela, modulo, modelo e inquilino. Cada registro traz os tokens, o custo "
        "ja apurado pela tabela de precos e a execucao de origem, quando houve — e a "
        "trilha que sustenta qualquer conferencia de fatura."
    ),
)
async def list_usage(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    since: _Since = None,
    until: _Until = None,
    module_slug: _ModuleSlug = None,
    model: Annotated[str | None, Query(description="Restringe ao consumo de um modelo.")] = None,
    tenant_id: _TenantId = None,
) -> Page[UsageRecordOut]:
    """Devolve a pagina de registros de consumo."""
    now = utcnow()
    filters = UsageFilter(
        since=parse_moment(since, field="since", now=now),
        until=parse_moment(until, field="until", now=now),
        module_slug=module_slug,
        model=model,
        tenant_id=tenant_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListUsage(container).execute(filters, principal)
    return Page[UsageRecordOut].from_result(result, UsageRecordOut.from_domain)


# ---------------------------------------------------------------------------
# Tabela de precos
# ---------------------------------------------------------------------------
@router.get(
    "/prices",
    response_model=PriceTableOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Tabela de precos por modelo",
    description=(
        "Devolve os precos em USD por 1k tokens usados para converter consumo em custo, "
        "mais os precos default aplicados a modelo sem tabela. Modelo desconhecido nunca "
        "custa zero em silencio: ele entra pelo default e a lacuna e sinalizada."
    ),
)
async def get_prices(container: ContainerDep, principal: _Reader) -> PriceTableOut:
    """Devolve a tabela de precos corrente."""
    table = await GetPrices(container).execute(principal)
    return PriceTableOut.from_result(table)


@router.put(
    "/prices",
    response_model=PriceTableOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 422),
    summary="Atualizar a tabela de precos",
    description=(
        "Insere ou substitui os precos informados e devolve a tabela resultante. A "
        "mudanca vale para o **processo em execucao**: a fonte da verdade permanente e "
        "`LUKATO_FINOPS__PRICES` (12-factor), entao corrija aqui para nao esperar um "
        "redeploy e leve a mesma correcao para o ambiente."
    ),
)
async def update_prices(
    payload: PriceTableUpdate,
    container: ContainerDep,
    principal: _Writer,
) -> PriceTableOut:
    """Aplica os precos informados e devolve a tabela resultante."""
    table = await UpdatePrices(container).execute(payload.to_domain(), principal)
    return PriceTableOut.from_result(table)


# ---------------------------------------------------------------------------
# Orcamentos
# ---------------------------------------------------------------------------
@router.get(
    "/budgets",
    response_model=Page[BudgetOut],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Lista orcamentos",
    description=(
        "Devolve os orcamentos cadastrados, filtrando por escopo (`global`, "
        "`module:<slug>` ou `tenant:<id>`) e por estado. O orcamento inativo continua "
        "listado, mas nao e avaliado em nenhuma invocacao."
    ),
)
async def list_budgets(
    container: ContainerDep,
    principal: _Reader,
    scope: Annotated[
        str | None,
        Query(description="Escopo exato: `global`, `module:<slug>` ou `tenant:<id>`."),
    ] = None,
    is_active: Annotated[
        bool | None, Query(description="Filtra por orcamentos ativos ou inativos.")
    ] = None,
) -> Page[BudgetOut]:
    """Devolve os orcamentos no envelope de lista da API."""
    budgets = await ListBudgets(container).execute(
        BudgetFilter(scope=scope, is_active=is_active), principal
    )
    return Page[BudgetOut].of(
        [BudgetOut.from_domain(budget) for budget in budgets],
        total=len(budgets),
        limit=max(1, len(budgets)),
    )


@router.post(
    "/budgets",
    response_model=BudgetOut,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 422),
    summary="Criar orcamento",
    description=(
        "Cria um teto de gasto para um escopo e um periodo. `alert_threshold` acende o "
        "alerta sem bloquear nada; `hard_stop=true` faz a proxima invocacao do escopo "
        "responder `402` depois que o teto for ultrapassado (SPEC-0005 criterio 2). "
        "Escopo invalido ou limite nao positivo respondem `422`."
    ),
)
async def create_budget(
    payload: BudgetCreate,
    container: ContainerDep,
    principal: _Writer,
) -> BudgetOut:
    """Grava o orcamento e devolve o registro criado."""
    budget = await CreateBudget(container).execute(payload.to_input(), principal)
    return BudgetOut.from_domain(budget)


@router.get(
    "/budgets/{budget_id}",
    response_model=BudgetOut,
    status_code=status.HTTP_200_OK,
    responses=_BUDGET_ERRORS,
    summary="Busca um orcamento",
    description="Devolve o orcamento pelo identificador; inexistente responde `404`.",
)
async def get_budget(
    container: ContainerDep, principal: _Reader, budget_id: _BudgetId
) -> BudgetOut:
    """Devolve o orcamento pedido."""
    budget = await GetBudget(container).execute(budget_id, principal)
    return BudgetOut.from_domain(budget)


@router.put(
    "/budgets/{budget_id}",
    response_model=BudgetOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 404, 422),
    summary="Atualizar orcamento",
    description=(
        "Atualizacao **parcial**: apenas os campos enviados mudam, e omitir um campo "
        "preserva o valor atual. Corpo vazio e legitimo e devolve o orcamento intacto."
    ),
)
async def update_budget(
    budget_id: _BudgetId,
    payload: BudgetUpdate,
    container: ContainerDep,
    principal: _Writer,
) -> BudgetOut:
    """Aplica as mudancas informadas e devolve o orcamento resultante."""
    budget = await UpdateBudget(container).execute(budget_id, payload.to_input(), principal)
    return BudgetOut.from_domain(budget)


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_BUDGET_ERRORS,
    summary="Remover orcamento",
    description=(
        "Apaga o orcamento. O consumo ja registrado permanece intacto: some o teto, "
        "nao o historico de custo."
    ),
)
async def delete_budget(
    container: ContainerDep, principal: _Writer, budget_id: _BudgetId
) -> Response:
    """Remove o orcamento e responde 204 sem corpo."""
    await DeleteBudget(container).execute(budget_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/budgets/{budget_id}/status",
    response_model=BudgetStatusOut,
    status_code=status.HTTP_200_OK,
    responses=_BUDGET_ERRORS,
    summary="Situacao corrente de um orcamento",
    description=(
        "Compara o gasto acumulado da janela vigente com o teto e devolve o "
        "`BudgetCheck` inteiro: `ratio`, `alert`, `blocked`, gasto e saldo, com o "
        "inicio e o fim da janela explicitos. A janela e recalculada a cada consulta — "
        "`daily` reinicia a meia-noite, `weekly` na segunda-feira, `monthly` no dia 1 e "
        "`total` nunca reinicia. `blocked` so e verdadeiro com `hard_stop` ativo e "
        "consumo em 100% ou mais."
    ),
)
async def get_budget_status(
    container: ContainerDep, principal: _Reader, budget_id: _BudgetId
) -> BudgetStatusOut:
    """Devolve a situacao corrente do orcamento."""
    result = await GetBudgetStatus(container).execute(budget_id, principal)
    return BudgetStatusOut.from_result(result)
