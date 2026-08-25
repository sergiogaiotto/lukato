"""Building block de FinOps: custo, consumo, orcamentos e projecao de gasto (SPEC-0005).

Este modulo nao reimplementa nenhuma regra de FinOps: ele traduz o `payload` de um
:class:`~lukato.modules.base.ModuleRequest` nos casos de uso de
`lukato.application.use_cases.finops` e devolve o resultado ja serializavel. As
dependencias chegam pelo `Container` publicado em `ctx.services["container"]`;
nenhuma conexao de banco e aberta aqui (SPEC-0001 secao 2).

A unica conta feita neste arquivo e a **projecao**, e ela e deliberadamente
ingenua: `projecao = gasto_ate_agora / fracao_do_periodo_ja_decorrida`. E uma
extrapolacao **linear**, que assume ritmo constante ate o fim da janela. Nao ha
sazonalidade, tendencia nem intervalo de confianca — por isso toda resposta de
`forecast` carrega `method="linear"` e a frase que explica o metodo, e uma janela
recem-iniciada devolve `projected_total_usd = null` com o motivo em vez de um
numero inventado a partir de poucos segundos de amostra.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final

from lukato.application.container import Container
from lukato.application.use_cases.finops import (
    BUCKET_HOUR,
    COST_DIGITS,
    SUPPORTED_BUCKETS,
    BudgetFilter,
    BudgetStatus,
    CostFilter,
    GetBudgetStatus,
    GetCostSeries,
    GetCostSummary,
    ListBudgets,
    ListUsage,
    SeriesRequest,
    UsageFilter,
    period_start,
)
from lukato.config import get_logger
from lukato.domain.errors import ValidationError
from lukato.domain.models.finops import Budget, BudgetPeriod, UsageRecord
from lukato.domain.models.module import ModuleBinding, ModuleKind
from lukato.domain.types import Json, utcnow
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import register_module

__all__ = [
    "CONTAINER_SERVICE",
    "DEFAULT_ACTION",
    "FINOPS_ACTIONS",
    "FORECAST_EXPLANATION",
    "FORECAST_METHOD",
    "MAX_FORECAST_BUDGETS",
    "MAX_FORECAST_SECONDS",
    "MIN_ELAPSED_SECONDS",
    "FinOpsModule",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
CONTAINER_SERVICE: Final[str] = "container"
"""Chave de `ctx.services` onde a plataforma publica o `Container` da aplicacao."""

FINOPS_ACTIONS: Final[tuple[str, ...]] = (
    "summary",
    "series",
    "usage",
    "budgets",
    "budget_status",
    "forecast",
)
"""Acoes aceitas em `payload["action"]`, na ordem em que aparecem na SPEC-0005."""

DEFAULT_ACTION: Final[str] = "summary"
"""Acao assumida quando o chamador nao informa nenhuma."""

GLOBAL_SCOPE: Final[str] = "global"
_MODULE_PREFIX: Final[str] = "module:"
_TENANT_PREFIX: Final[str] = "tenant:"

FORECAST_METHOD: Final[str] = "linear"
"""Identificador do metodo de projecao, sempre presente na resposta."""

FORECAST_EXPLANATION: Final[str] = (
    "Projecao LINEAR: o gasto acumulado no periodo e dividido pela fracao do periodo "
    "ja decorrida e extrapolado ate o fim da janela, assumindo ritmo de consumo "
    "constante. Nao considera sazonalidade, tendencia nem picos de uso."
)
"""Frase que acompanha toda projecao: o metodo nunca fica implicito."""

MIN_ELAPSED_SECONDS: Final[float] = 60.0
"""Amostra minima do periodo para projetar; abaixo disso a projecao seria ruido."""

MAX_FORECAST_SECONDS: Final[float] = 315_360_000.0
"""Horizonte maximo de uma data projetada (10 anos); alem disso e reportado `null`."""

MAX_FORECAST_BUDGETS: Final[int] = 20
"""Teto de orcamentos avaliados numa projecao sem `budget_id` explicito."""

SECONDS_PER_DAY: Final[float] = 86_400.0

_RELATIVE_WINDOW = re.compile(r"^-?(?P<amount>\d+(?:\.\d+)?)(?P<unit>[smhdw])$", re.IGNORECASE)
_UNIT_SECONDS: Final[dict[str, float]] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3_600.0,
    "d": SECONDS_PER_DAY,
    "w": 7.0 * SECONDS_PER_DAY,
}

_TRUE_WORDS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on", "sim"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off", "nao", "não"})


# ---------------------------------------------------------------------------
# Leitura defensiva do payload
# ---------------------------------------------------------------------------
def _as_utc(moment: datetime) -> datetime:
    """Converte para UTC; instante ingenuo e assumido como ja estando em UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _text(payload: Mapping[str, Any], key: str, *, default: str = "") -> str:
    """Le um campo textual do payload, com `default` quando ausente ou em branco."""
    raw = payload.get(key)
    if raw is None:
        return default
    candidate = str(raw).strip()
    return candidate or default


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    """Le um campo textual opcional; ausente ou em branco vira `None`."""
    return _text(payload, key) or None


def _require_text(payload: Mapping[str, Any], key: str, *, action: str) -> str:
    """Le um campo textual obrigatorio da acao."""
    found = _optional_text(payload, key)
    if found is None:
        raise ValidationError(
            f"A acao '{action}' exige o campo '{key}' no payload.",
            details={"action": action, "field": key},
        )
    return found


def _optional_flag(payload: Mapping[str, Any], key: str) -> bool | None:
    """Le um booleano de tres estados: ausente vira `None`."""
    raw = payload.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    candidate = str(raw).strip().lower()
    if not candidate:
        return None
    if candidate in _TRUE_WORDS:
        return True
    if candidate in _FALSE_WORDS:
        return False
    raise ValidationError(
        f"Valor booleano invalido em '{key}': {raw!r}.",
        details={"field": key, "value": str(raw)},
    )


def _integer(
    payload: Mapping[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    """Le um inteiro do payload dentro de uma faixa fechada."""
    raw = payload.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Valor inteiro invalido em '{key}': {raw!r}.",
            details={"field": key, "value": str(raw)},
        ) from exc
    return max(minimum, min(value, maximum))


def _moment(payload: Mapping[str, Any], key: str, *, now: datetime) -> datetime | None:
    """Le um instante do payload: ISO-8601 ou janela relativa (`24h`, `7d`, `30m`).

    A forma relativa e a mesma citada na SPEC-0005 secao 5 (`since=24h`) e e
    resolvida contra `now`, nunca contra o relogio do chamador.
    """
    raw = payload.get(key)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _as_utc(raw)
    candidate = str(raw).strip()
    if not candidate:
        return None
    relative = _RELATIVE_WINDOW.fullmatch(candidate)
    if relative is not None:
        seconds = float(relative["amount"]) * _UNIT_SECONDS[relative["unit"].lower()]
        return now - timedelta(seconds=seconds)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError(
            f"Instante invalido em '{key}': {candidate!r}. Use ISO-8601 "
            f"(2026-08-25T00:00:00Z) ou uma janela relativa (24h, 7d, 30m).",
            details={"field": key, "value": candidate},
        ) from exc
    return _as_utc(parsed)


def _bucket(payload: Mapping[str, Any], *, default: str) -> str:
    """Le a granularidade da serie temporal (`hour` ou `day`)."""
    candidate = _text(payload, "bucket", default=default).lower()
    if candidate not in SUPPORTED_BUCKETS:
        raise ValidationError(
            f"Granularidade de serie invalida: {candidate!r}.",
            details={"bucket": candidate, "supported": list(SUPPORTED_BUCKETS)},
        )
    return candidate


def _period(payload: Mapping[str, Any], *, default: str) -> BudgetPeriod:
    """Le a janela de apuracao pedida na projecao."""
    candidate = _text(payload, "period", default=default).lower()
    try:
        return BudgetPeriod(candidate)
    except ValueError as exc:
        raise ValidationError(
            f"Periodo invalido: {candidate!r}.",
            details={"period": candidate, "supported": [item.value for item in BudgetPeriod]},
        ) from exc


def _scope(payload: Mapping[str, Any], *, default: str) -> str:
    """Le e valida o escopo (`global`, `module:<slug>` ou `tenant:<id>`)."""
    candidate = _text(payload, "scope", default=default) or GLOBAL_SCOPE
    if candidate == GLOBAL_SCOPE:
        return candidate
    for prefix in (_MODULE_PREFIX, _TENANT_PREFIX):
        if candidate.startswith(prefix) and candidate[len(prefix) :].strip():
            return candidate
    raise ValidationError(
        f"Escopo invalido: {candidate!r}.",
        details={"scope": candidate, "supported": ["global", "module:<slug>", "tenant:<id>"]},
    )


def _action_of(request: ModuleRequest, payload: Mapping[str, Any], *, default: str) -> str:
    """Resolve a acao pedida: `payload["action"]` e, como atalho, `request.input`."""
    candidate = _text(payload, "action")
    if not candidate:
        typed = request.input.strip().lower()
        candidate = typed if typed in FINOPS_ACTIONS else default
    candidate = candidate.strip().lower()
    if candidate not in FINOPS_ACTIONS:
        raise ValidationError(
            f"Acao de FinOps desconhecida: {candidate!r}.",
            details={"action": candidate, "supported": list(FINOPS_ACTIONS)},
        )
    return candidate


# ---------------------------------------------------------------------------
# Janela e projecao linear
# ---------------------------------------------------------------------------
def _period_end(period: BudgetPeriod, start: datetime) -> datetime | None:
    """Fim da janela de apuracao; `total` nao tem fim e devolve `None`."""
    if period is BudgetPeriod.DAILY:
        return start + timedelta(days=1)
    if period is BudgetPeriod.WEEKLY:
        return start + timedelta(days=7)
    if period is BudgetPeriod.MONTHLY:
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    return None


@dataclass(frozen=True, slots=True)
class _Window:
    """Janela de apuracao usada pela projecao.

    `rate_start` pode ser posterior a `start`: um orcamento `total` comeca na
    epoca, mas o ritmo so faz sentido a partir do momento em que ele passou a
    existir.
    """

    period: BudgetPeriod
    start: datetime
    end: datetime | None
    rate_start: datetime

    def to_dict(self) -> Json:
        """Forma serializavel da janela."""
        return {
            "period": self.period.value,
            "period_start": self.start.isoformat(),
            "period_end": self.end.isoformat() if self.end is not None else None,
            "rate_start": self.rate_start.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class _Projection:
    """Resultado da extrapolacao linear do gasto de uma janela."""

    spent_usd: float
    elapsed_seconds: float
    period_seconds: float | None
    elapsed_fraction: float | None
    daily_burn_usd: float | None
    rate_per_second: float
    projected_total_usd: float | None
    reliable: bool
    reason: str

    def to_dict(self) -> Json:
        """Forma serializavel da projecao, sempre com o metodo explicito."""
        return {
            "method": FORECAST_METHOD,
            "explanation": FORECAST_EXPLANATION,
            "spent_usd": self.spent_usd,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "period_seconds": (
                None if self.period_seconds is None else round(self.period_seconds, 3)
            ),
            "elapsed_fraction": (
                None if self.elapsed_fraction is None else round(self.elapsed_fraction, 6)
            ),
            "daily_burn_usd": self.daily_burn_usd,
            "projected_total_usd": self.projected_total_usd,
            "reliable": self.reliable,
            "reason": self.reason,
        }


def _project(spent: float, window: _Window, *, now: datetime) -> _Projection:
    """Extrapola linearmente o gasto da janela ate o fim do periodo.

    Sem periodo fechado (`total`) nao existe total a projetar: a funcao devolve
    apenas o ritmo, que ainda permite estimar a data de estouro de um limite.
    """
    spent_usd = round(max(0.0, float(spent)), COST_DIGITS)
    elapsed = max(0.0, (now - window.rate_start).total_seconds())
    period_seconds = (
        None if window.end is None else max(0.0, (window.end - window.start).total_seconds())
    )
    fraction: float | None = None
    if period_seconds:
        elapsed_in_period = max(0.0, (now - window.start).total_seconds())
        fraction = min(1.0, elapsed_in_period / period_seconds)

    if elapsed < MIN_ELAPSED_SECONDS:
        return _Projection(
            spent_usd=spent_usd,
            elapsed_seconds=elapsed,
            period_seconds=period_seconds,
            elapsed_fraction=fraction,
            daily_burn_usd=None,
            rate_per_second=0.0,
            projected_total_usd=None,
            reliable=False,
            reason=(
                f"periodo decorrido ha apenas {elapsed:.0f}s, abaixo do minimo de "
                f"{MIN_ELAPSED_SECONDS:.0f}s: extrapolar essa amostra produziria ruido"
            ),
        )

    rate = spent_usd / elapsed
    projected: float | None = None
    if period_seconds and fraction:
        projected = round(spent_usd / fraction, COST_DIGITS)
    return _Projection(
        spent_usd=spent_usd,
        elapsed_seconds=elapsed,
        period_seconds=period_seconds,
        elapsed_fraction=fraction,
        daily_burn_usd=round(rate * SECONDS_PER_DAY, COST_DIGITS),
        rate_per_second=rate,
        projected_total_usd=projected,
        reliable=True,
        reason=(
            "projecao linear sobre o ritmo observado no periodo"
            if projected is not None
            else "periodo sem fim definido (total): so o ritmo de consumo foi projetado"
        ),
    )


def _exhaustion(
    *, limit_usd: float, spent_usd: float, projection: _Projection, window: _Window, now: datetime
) -> tuple[str | None, bool, str]:
    """Data projetada de estouro do limite: `(instante, ja_estourou, motivo)`.

    Devolve `None` quando o ritmo atual nao leva ao limite dentro da janela — um
    orcamento que nao estoura nao ganha uma data inventada.
    """
    if limit_usd <= 0.0:
        return (now.isoformat(), True, "orcamento sem limite positivo: ja considerado esgotado")
    remaining = limit_usd - spent_usd
    if remaining <= 0.0:
        return (now.isoformat(), True, "o limite ja foi ultrapassado pelo gasto acumulado")
    if not projection.reliable or projection.rate_per_second <= 0.0:
        return (None, False, projection.reason if not projection.reliable else "sem consumo")
    seconds = remaining / projection.rate_per_second
    # O horizonte e checado em segundos, antes de virar `timedelta`: um ritmo
    # quase nulo produz um numero grande o bastante para estourar `datetime`.
    if window.end is not None:
        if seconds > (window.end - now).total_seconds():
            return (
                None,
                False,
                "no ritmo atual o limite nao e atingido antes do fim do periodo",
            )
    elif seconds > MAX_FORECAST_SECONDS:
        return (
            None,
            False,
            "no ritmo atual o limite levaria mais de 10 anos para ser atingido",
        )
    moment = now + timedelta(seconds=seconds)
    return (moment.isoformat(), False, "data em que o ritmo atual atinge o limite")


def _budget_window(budget: Budget, start: datetime) -> _Window:
    """Janela de um orcamento; em `total` o ritmo conta a partir da criacao dele."""
    end = _period_end(budget.period, start)
    rate_start = start
    if budget.period is BudgetPeriod.TOTAL:
        rate_start = max(start, _as_utc(budget.created_at))
    return _Window(period=budget.period, start=start, end=end, rate_start=rate_start)


def _project_budget(status: BudgetStatus, *, now: datetime) -> Json:
    """Projeta um orcamento a partir da sua situacao corrente."""
    budget = status.budget
    window = _budget_window(budget, _as_utc(status.period_start))
    projection = _project(status.check.spent, window, now=now)
    limit = float(budget.limit_usd)
    exhaustion, already, reason = _exhaustion(
        limit_usd=limit,
        spent_usd=projection.spent_usd,
        projection=projection,
        window=window,
        now=now,
    )
    projected = projection.projected_total_usd
    return {
        "budget_id": budget.id,
        "name": budget.name,
        "scope": budget.scope,
        "hard_stop": budget.hard_stop,
        "is_active": budget.is_active,
        "limit_usd": round(limit, COST_DIGITS),
        "spent_usd": projection.spent_usd,
        "ratio": status.check.ratio,
        "alert": status.check.alert,
        "blocked": status.check.blocked,
        "projected_total_usd": projected,
        "projected_ratio": (
            None if projected is None or limit <= 0.0 else round(projected / limit, 6)
        ),
        "projected_overrun_usd": (
            None if projected is None else round(max(0.0, projected - limit), COST_DIGITS)
        ),
        "projected_exhaustion_at": exhaustion,
        "already_exceeded": already,
        "will_exceed": exhaustion is not None,
        "exhaustion_reason": reason,
        "window": window.to_dict(),
        "projection": projection.to_dict(),
    }


def _money(value: float) -> str:
    """Formata um custo em USD com as 5 casas usadas pela UI (SPEC-0005 secao 2)."""
    return f"{value:.5f}"


# ---------------------------------------------------------------------------
# Building block
# ---------------------------------------------------------------------------
@register_module
class FinOpsModule(BaseModule):
    """Custo por modulo, modelo e tenant, orcamentos e projecao linear de gasto.

    Despacha por `payload["action"]`: `summary`, `series`, `usage`, `budgets`,
    `budget_status` e `forecast`. Toda autorizacao (`finops:read` / `finops:write`)
    e feita pelos casos de uso, nunca aqui.
    """

    kind: ClassVar[ModuleKind] = ModuleKind.FINOPS
    slug: ClassVar[str] = "finops"
    name: ClassVar[str] = "FinOps"
    description: ClassVar[str] = (
        "Custo por modulo, modelo e tenant, orcamentos e projecao linear de gasto."
    )
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[tuple[str, ...]] = ("cost_summary", "budgets", "forecast")
    config_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "default_action": {
                "type": "string",
                "enum": list(FINOPS_ACTIONS),
                "default": DEFAULT_ACTION,
            },
            "default_bucket": {
                "type": "string",
                "enum": list(SUPPORTED_BUCKETS),
                "default": BUCKET_HOUR,
            },
            "default_period": {
                "type": "string",
                "enum": [item.value for item in BudgetPeriod],
                "default": BudgetPeriod.MONTHLY.value,
            },
            "default_scope": {"type": "string", "default": GLOBAL_SCOPE},
            "max_budgets": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": MAX_FORECAST_BUDGETS,
            },
        },
    }
    default_binding: ClassVar[ModuleBinding] = ModuleBinding(timeout_seconds=30.0)

    # -- execucao ----------------------------------------------------------
    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Despacha a acao pedida sobre os casos de uso de FinOps."""
        container = ctx.service(CONTAINER_SERVICE, Container)
        config = self.validate_config(dict(ctx.definition.config or {}))
        payload: Json = dict(request.payload or {})
        action = _action_of(
            request, payload, default=str(config.get("default_action", DEFAULT_ACTION))
        )
        now = utcnow()

        if action == "summary":
            return await self._summary(container, ctx, payload, now=now)
        if action == "series":
            return await self._series(container, ctx, payload, config, now=now)
        if action == "usage":
            return await self._usage(container, ctx, payload, now=now)
        if action == "budgets":
            return await self._budgets(container, ctx, payload)
        if action == "budget_status":
            return await self._budget_status(container, ctx, payload)
        return await self._forecast(container, ctx, payload, config, now=now)

    # -- acoes de leitura --------------------------------------------------
    async def _summary(
        self, container: Container, ctx: ModuleContext, payload: Json, *, now: datetime
    ) -> ModuleResponse:
        """`summary`: resumo de custo do periodo, por modulo e por modelo."""
        since = _moment(payload, "since", now=now)
        until = _moment(payload, "until", now=now)
        filters = CostFilter(
            since=since,
            until=until,
            module_slug=_optional_text(payload, "module_slug"),
            tenant_id=_optional_text(payload, "tenant_id"),
        )
        summary = await GetCostSummary(container).execute(filters, ctx.principal)
        data: Json = {
            "action": "summary",
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "module_slug": filters.module_slug,
            "tenant_id": filters.tenant_id,
            "summary": summary.model_dump(mode="json"),
        }
        output = (
            f"Custo total de {_money(summary.total_usd)} USD em {summary.runs} execucao(oes) "
            f"e {summary.total_tokens} token(s)."
        )
        return ModuleResponse(output=output, data=data, metadata={"action": "summary"})

    async def _series(
        self,
        container: Container,
        ctx: ModuleContext,
        payload: Json,
        config: Json,
        *,
        now: datetime,
    ) -> ModuleResponse:
        """`series`: serie temporal de custo por hora ou por dia, sem buracos."""
        bucket = _bucket(payload, default=str(config.get("default_bucket", BUCKET_HOUR)))
        request = SeriesRequest(
            bucket=bucket,
            since=_moment(payload, "since", now=now),
            until=_moment(payload, "until", now=now),
            module_slug=_optional_text(payload, "module_slug"),
            tenant_id=_optional_text(payload, "tenant_id"),
        )
        points = await GetCostSeries(container).execute(request, ctx.principal)
        total = round(sum(float(point["cost_usd"]) for point in points), COST_DIGITS)
        data: Json = {
            "action": "series",
            "bucket": bucket,
            "points": points,
            "buckets": len(points),
            "total_usd": total,
            "module_slug": request.module_slug,
            "tenant_id": request.tenant_id,
        }
        output = (
            f"Serie de {len(points)} balde(s) de {bucket}, somando {_money(total)} USD no periodo."
        )
        return ModuleResponse(output=output, data=data, metadata={"action": "series"})

    async def _usage(
        self, container: Container, ctx: ModuleContext, payload: Json, *, now: datetime
    ) -> ModuleResponse:
        """`usage`: registros de consumo paginados."""
        filters = UsageFilter(
            since=_moment(payload, "since", now=now),
            until=_moment(payload, "until", now=now),
            module_slug=_optional_text(payload, "module_slug"),
            model=_optional_text(payload, "model"),
            tenant_id=_optional_text(payload, "tenant_id"),
            limit=_integer(payload, "limit", default=50, minimum=1, maximum=200),
            offset=_integer(payload, "offset", default=0, minimum=0, maximum=1_000_000),
        )
        page = await ListUsage(container).execute(filters, ctx.principal)
        data: Json = {"action": "usage"}
        data.update(page.to_dict(_dump_usage))
        output = f"{page.count} de {page.total} registro(s) de consumo."
        return ModuleResponse(output=output, data=data, metadata={"action": "usage"})

    async def _budgets(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`budgets`: lista os orcamentos cadastrados."""
        filters = BudgetFilter(
            scope=_optional_text(payload, "scope"),
            is_active=_optional_flag(payload, "is_active"),
        )
        budgets = await ListBudgets(container).execute(filters, ctx.principal)
        data: Json = {
            "action": "budgets",
            "items": [budget.model_dump(mode="json") for budget in budgets],
            "total": len(budgets),
            "scope": filters.scope,
            "is_active": filters.is_active,
        }
        output = f"{len(budgets)} orcamento(s) encontrado(s)."
        return ModuleResponse(output=output, data=data, metadata={"action": "budgets"})

    async def _budget_status(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`budget_status`: situacao corrente de um orcamento."""
        budget_id = _require_text(payload, "budget_id", action="budget_status")
        status = await GetBudgetStatus(container).execute(budget_id, ctx.principal)
        data: Json = {"action": "budget_status", "status": status.to_dict()}
        output = (
            f"Orcamento '{status.budget.name}': {_money(status.check.spent)} de "
            f"{_money(status.check.limit_usd)} USD ({status.check.ratio:.1%} do limite)."
        )
        return ModuleResponse(output=output, data=data, metadata={"action": "budget_status"})

    # -- projecao ----------------------------------------------------------
    async def _forecast(
        self,
        container: Container,
        ctx: ModuleContext,
        payload: Json,
        config: Json,
        *,
        now: datetime,
    ) -> ModuleResponse:
        """`forecast`: extrapolacao LINEAR do gasto do periodo e do estouro de orcamentos.

        Com `budget_id`, a janela e a do proprio orcamento. Sem ele, a janela vem
        de `period` (`daily`/`weekly`/`monthly`) e todos os orcamentos ativos do
        escopo sao projetados, cada um na sua propria janela.
        """
        budget_id = _optional_text(payload, "budget_id")
        if budget_id is not None:
            status = await GetBudgetStatus(container).execute(budget_id, ctx.principal)
            projected = _project_budget(status, now=now)
            data: Json = {
                "action": "forecast",
                "method": FORECAST_METHOD,
                "explanation": FORECAST_EXPLANATION,
                "generated_at": now.isoformat(),
                "scope": status.budget.scope,
                "window": projected["window"],
                "projection": projected["projection"],
                "budgets": [projected],
            }
            _log_forecast(status.budget.scope, projected["projection"], 1)
            return ModuleResponse(
                output=_forecast_output(projected["projection"], [projected]),
                data=data,
                metadata={"action": "forecast", "method": FORECAST_METHOD},
            )

        period = _period(payload, default=str(config.get("default_period", "monthly")))
        if period is BudgetPeriod.TOTAL:
            raise ValidationError(
                "O periodo 'total' nao tem fim definido e por isso nao admite projecao "
                "agregada. Informe 'budget_id' para projetar um orcamento total, ou use "
                "'daily', 'weekly' ou 'monthly'.",
                details={
                    "period": period.value,
                    "supported": ["daily", "weekly", "monthly"],
                    "hint": "budget_id",
                },
            )
        scope = _scope(payload, default=str(config.get("default_scope", GLOBAL_SCOPE)))
        module_slug = _optional_text(payload, "module_slug") or _prefixed(scope, _MODULE_PREFIX)
        tenant_id = _optional_text(payload, "tenant_id") or _prefixed(scope, _TENANT_PREFIX)

        start = period_start(period, now=now)
        window = _Window(
            period=period, start=start, end=_period_end(period, start), rate_start=start
        )
        summary = await GetCostSummary(container).execute(
            CostFilter(since=start, until=now, module_slug=module_slug, tenant_id=tenant_id),
            ctx.principal,
        )
        projection = _project(summary.total_usd, window, now=now)
        budgets = await self._project_scope_budgets(
            container,
            ctx,
            scope=scope,
            limit=_integer(
                payload,
                "max_budgets",
                default=int(config.get("max_budgets", MAX_FORECAST_BUDGETS)),
                minimum=1,
                maximum=100,
            ),
            now=now,
        )
        data = {
            "action": "forecast",
            "method": FORECAST_METHOD,
            "explanation": FORECAST_EXPLANATION,
            "generated_at": now.isoformat(),
            "scope": scope,
            "module_slug": module_slug,
            "tenant_id": tenant_id,
            "window": window.to_dict(),
            "projection": projection.to_dict(),
            "summary": summary.model_dump(mode="json"),
            "budgets": budgets,
        }
        _log_forecast(scope, projection.to_dict(), len(budgets))
        return ModuleResponse(
            output=_forecast_output(projection.to_dict(), budgets),
            data=data,
            metadata={"action": "forecast", "method": FORECAST_METHOD},
        )

    async def _project_scope_budgets(
        self,
        container: Container,
        ctx: ModuleContext,
        *,
        scope: str,
        limit: int,
        now: datetime,
    ) -> list[Json]:
        """Projeta os orcamentos ativos do escopo, cada um na sua propria janela."""
        candidates = await ListBudgets(container).execute(
            BudgetFilter(scope=None if scope == GLOBAL_SCOPE else scope, is_active=True),
            ctx.principal,
        )
        status_use_case = GetBudgetStatus(container)
        projected: list[Json] = []
        for budget in candidates[:limit]:
            status = await status_use_case.execute(budget.id, ctx.principal)
            projected.append(_project_budget(status, now=now))
        if len(candidates) > limit:
            _logger.info(
                "forecast_budgets_truncated",
                scope=scope,
                budgets=len(candidates),
                projected=limit,
            )
        return projected

    # -- presenca na plataforma -------------------------------------------
    def ui(self) -> UIDescriptor:
        """Publica o item FinOps na secao MONITORAMENTO do console (SPEC-0009 secao 4)."""
        return UIDescriptor(
            nav=[
                UINavItem(
                    label="FinOps",
                    icon="coins",
                    endpoint="/finops",
                    section="MONITORAMENTO",
                    order=10,
                )
            ],
            center_template="pages/finops.html",
            context_template="context/default.html",
        )

    def health(self) -> Json:
        """Resumo de saude com as acoes efetivamente atendidas."""
        report = super().health()
        report["actions"] = list(FINOPS_ACTIONS)
        report["forecast_method"] = FORECAST_METHOD
        return report


# ---------------------------------------------------------------------------
# Auxiliares de modulo
# ---------------------------------------------------------------------------
def _prefixed(scope: str, prefix: str) -> str | None:
    """Extrai o sufixo de um escopo `module:`/`tenant:`; `None` quando nao casa."""
    if scope.startswith(prefix):
        return scope[len(prefix) :].strip() or None
    return None


def _dump_usage(record: UsageRecord) -> Json:
    """Serializa um registro de consumo para a resposta do modulo."""
    return record.model_dump(mode="json")


def _forecast_output(projection: Json, budgets: list[Json]) -> str:
    """Frase de resumo da projecao, sempre nomeando o metodo linear."""
    spent = _money(float(projection["spent_usd"]))
    total = projection["projected_total_usd"]
    head = (
        f"Projecao linear: {spent} USD gastos ate agora projetam "
        f"{_money(float(total))} USD no periodo."
        if total is not None
        else f"Projecao linear indisponivel ({projection['reason']}); {spent} USD gastos ate agora."
    )
    breaching = [item for item in budgets if item["projected_exhaustion_at"]]
    if not breaching:
        return f"{head} Nenhum orcamento projeta estouro nesta janela."
    first = min(breaching, key=lambda item: str(item["projected_exhaustion_at"]))
    return (
        f"{head} {len(breaching)} orcamento(s) projetam estouro; o mais proximo e "
        f"'{first['name']}' em {first['projected_exhaustion_at']}."
    )


def _log_forecast(scope: str, projection: Json, budgets: int) -> None:
    """Registra a projecao emitida, com o metodo explicito na trilha de log."""
    _logger.info(
        "finops_forecast",
        scope=scope,
        method=FORECAST_METHOD,
        spent_usd=projection["spent_usd"],
        projected_total_usd=projection["projected_total_usd"],
        reliable=projection["reliable"],
        budgets=budgets,
    )
