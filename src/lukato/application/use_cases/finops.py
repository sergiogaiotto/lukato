"""Casos de uso de FinOps: custo, consumo, tabela de precos e orcamentos (SPEC-0005).

Tres decisoes deste modulo merecem contexto:

* **A serie temporal e agregada em Python.** O repositorio devolve os
  `UsageRecord` do periodo e :func:`build_series` os distribui em baldes de hora
  ou de dia. Os baldes vazios sao preenchidos com zero porque o grafico do
  console precisa de uma serie continua: um buraco no eixo do tempo seria lido
  como "sem dado", nao como "sem custo".
* **O periodo do orcamento e recalculado a cada consulta.** `GetBudgetStatus`
  encontra o inicio da janela corrente (`daily`, `weekly`, `monthly`, `total`) e
  compara o gasto acumulado com o limite via
  :meth:`CostCalculator.check_budget`.
* **A tabela de precos vive no processo.** Ela nasce de `Settings`
  (`LUKATO_FINOPS__PRICES`, 12-factor) e `UpdatePrices` a ajusta na instancia em
  execucao — nao existe tabela de precos no banco (SPEC-0011). Mudanca
  permanente e mudanca de ambiente.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from lukato.application.container import Container
from lukato.application.dto import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, UNSET, Maybe, Page, is_set
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import NotFoundError, ValidationError
from lukato.domain.models.finops import (
    Budget,
    BudgetPeriod,
    CostSummary,
    ModelPrice,
    UsageRecord,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.run import TokenUsage
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.services.cost_calculator import BudgetCheck
from lukato.domain.types import DEFAULT_TENANT, Id, Json, utcnow

__all__ = [
    "BUCKET_DAY",
    "BUCKET_HOUR",
    "COST_DIGITS",
    "MAX_SERIES_BUCKETS",
    "SUPPORTED_BUCKETS",
    "BudgetFilter",
    "BudgetInput",
    "BudgetStatus",
    "BudgetUpdateInput",
    "CostFilter",
    "CreateBudget",
    "DeleteBudget",
    "GetBudget",
    "GetBudgetStatus",
    "GetCostSeries",
    "GetCostSummary",
    "GetPrices",
    "ListBudgets",
    "ListUsage",
    "PriceTable",
    "RecordUsage",
    "SeriesRequest",
    "UpdateBudget",
    "UpdatePrices",
    "UsageFilter",
    "UsageInput",
    "build_series",
    "period_start",
]

_logger = get_logger(__name__)

COST_DIGITS: Final[int] = 8
"""Casas decimais do custo em USD; a UI formata com 5 (SPEC-0005 secao 2)."""

BUCKET_HOUR: Final[str] = "hour"
BUCKET_DAY: Final[str] = "day"

SUPPORTED_BUCKETS: Final[tuple[str, ...]] = (BUCKET_HOUR, BUCKET_DAY)
"""Granularidades aceitas por `GET /finops/series`."""

_BUCKET_STEP: Final[dict[str, timedelta]] = {
    BUCKET_HOUR: timedelta(hours=1),
    BUCKET_DAY: timedelta(days=1),
}

_DEFAULT_WINDOW: Final[dict[str, timedelta]] = {
    BUCKET_HOUR: timedelta(hours=24),
    BUCKET_DAY: timedelta(days=30),
}
"""Janela usada quando o chamador nao informa `since`."""

MAX_SERIES_BUCKETS: Final[int] = 1000
"""Teto de pontos de uma serie: janela maior deve usar `bucket=day`."""

SERIES_PAGE_SIZE: Final[int] = 500
"""Tamanho da pagina usada para varrer os registros de consumo do periodo."""

MAX_SERIES_RECORDS: Final[int] = 100_000
"""Teto de registros lidos por serie; alem disso a serie e reportada truncada."""

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
"""Inicio de periodo dos orcamentos de escopo `TOTAL`."""

_GLOBAL_SCOPE: Final[str] = "global"
_MODULE_PREFIX: Final[str] = "module:"
_TENANT_PREFIX: Final[str] = "tenant:"


# ---------------------------------------------------------------------------
# Funcoes puras de tempo e agregacao
# ---------------------------------------------------------------------------
def _as_utc(moment: datetime) -> datetime:
    """Converte para UTC; instante ingenuo e assumido como ja estando em UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def period_start(period: BudgetPeriod, *, now: datetime) -> datetime:
    """Instante inicial da janela de apuracao corrente do orcamento.

    `daily` comeca a meia-noite, `weekly` na segunda-feira, `monthly` no dia 1 e
    `total` na epoca — um orcamento total nunca reinicia.
    """
    reference = _as_utc(now)
    midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is BudgetPeriod.DAILY:
        return midnight
    if period is BudgetPeriod.WEEKLY:
        return midnight - timedelta(days=midnight.weekday())
    if period is BudgetPeriod.MONTHLY:
        return midnight.replace(day=1)
    return _EPOCH


def _floor(moment: datetime, bucket: str) -> datetime:
    """Alinha o instante ao inicio do balde de hora ou de dia."""
    reference = _as_utc(moment)
    if bucket == BUCKET_DAY:
        return reference.replace(hour=0, minute=0, second=0, microsecond=0)
    return reference.replace(minute=0, second=0, microsecond=0)


def _validate_bucket(bucket: str) -> str:
    """Aceita apenas `hour` ou `day`."""
    candidate = (bucket or "").strip().lower()
    if candidate not in SUPPORTED_BUCKETS:
        raise ValidationError(
            f"Granularidade de serie invalida: {bucket!r}.",
            details={"bucket": bucket, "supported": list(SUPPORTED_BUCKETS)},
        )
    return candidate


def _validate_scope(scope: str) -> str:
    """Aceita `global`, `module:<slug>` ou `tenant:<id>` (SPEC-0005 secao 2)."""
    candidate = (scope or "").strip() or _GLOBAL_SCOPE
    if candidate == _GLOBAL_SCOPE:
        return candidate
    for prefix in (_MODULE_PREFIX, _TENANT_PREFIX):
        if candidate.startswith(prefix) and candidate[len(prefix) :].strip():
            return candidate
    raise ValidationError(
        f"Escopo de orcamento invalido: {scope!r}.",
        details={"scope": scope, "supported": ["global", "module:<slug>", "tenant:<id>"]},
    )


@dataclass(slots=True)
class _Accumulator:
    """Acumulador de um balde da serie temporal."""

    cost_usd: float = 0.0
    tokens: int = 0
    runs: set[Id] = field(default_factory=set)
    orphan_runs: int = 0

    def add(self, record: UsageRecord) -> None:
        """Soma um registro de consumo neste balde."""
        self.cost_usd += float(record.cost_usd)
        self.tokens += int(record.usage.total_tokens)
        if record.run_id is None:
            self.orphan_runs += 1
        else:
            self.runs.add(record.run_id)

    def to_dict(self, moment: datetime) -> Json:
        """Ponto da serie no formato consumido pelo grafico do console."""
        return {
            "bucket": moment.isoformat(),
            "cost_usd": round(self.cost_usd, COST_DIGITS),
            "tokens": self.tokens,
            "runs": len(self.runs) + self.orphan_runs,
        }


def build_series(
    records: Iterable[UsageRecord],
    *,
    bucket: str,
    since: datetime,
    until: datetime,
) -> list[Json]:
    """Distribui os registros em baldes de hora ou de dia, sem buracos no eixo.

    Todo balde entre `since` e `until` aparece na saida, mesmo sem consumo: a UI
    desenha uma serie continua e um zero significa "nao gastou", nunca "nao sei".
    """
    granularity = _validate_bucket(bucket)
    step = _BUCKET_STEP[granularity]
    first = _floor(since, granularity)
    last = _floor(until, granularity)
    if last < first:
        raise ValidationError(
            "O inicio da serie precisa ser anterior ao fim.",
            details={"since": _as_utc(since).isoformat(), "until": _as_utc(until).isoformat()},
        )

    total_buckets = int((last - first) / step) + 1
    if total_buckets > MAX_SERIES_BUCKETS:
        raise ValidationError(
            f"A janela pedida produz {total_buckets} pontos, acima do teto de "
            f"{MAX_SERIES_BUCKETS}. Reduza o intervalo ou use 'bucket=day'.",
            details={
                "buckets": total_buckets,
                "max_buckets": MAX_SERIES_BUCKETS,
                "bucket": granularity,
            },
        )

    slots: dict[datetime, _Accumulator] = {}
    moment = first
    while moment <= last:
        slots[moment] = _Accumulator()
        moment += step

    for record in records:
        key = _floor(record.occurred_at, granularity)
        target = slots.get(key)
        if target is not None:
            target.add(record)

    return [slots[key].to_dict(key) for key in sorted(slots)]


# ---------------------------------------------------------------------------
# DTOs de entrada e de saida
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CostFilter:
    """Recorte de um resumo de custo."""

    since: datetime | None = None
    until: datetime | None = None
    module_slug: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class SeriesRequest:
    """Pedido de serie temporal de custo para os graficos do console."""

    bucket: str = BUCKET_HOUR
    since: datetime | None = None
    until: datetime | None = None
    module_slug: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class UsageFilter:
    """Filtros de listagem paginada dos registros de consumo."""

    since: datetime | None = None
    until: datetime | None = None
    module_slug: str | None = None
    model: str | None = None
    tenant_id: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao vinda da borda HTTP."""
        object.__setattr__(self, "limit", max(1, min(int(self.limit), MAX_PAGE_LIMIT)))
        object.__setattr__(self, "offset", max(0, int(self.offset)))


@dataclass(frozen=True, slots=True)
class UsageInput:
    """Registro de consumo a ser gravado fora do caminho de `InvokeModule`."""

    module_slug: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    run_id: Id | None = None
    cost_usd: float | None = None
    tenant_id: str = DEFAULT_TENANT
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BudgetInput:
    """Dados de criacao de um orcamento."""

    name: str
    limit_usd: float
    scope: str = _GLOBAL_SCOPE
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    alert_threshold: float = 0.8
    hard_stop: bool = False
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class BudgetUpdateInput:
    """Atualizacao parcial de um orcamento; campos ausentes ficam :data:`UNSET`."""

    name: Maybe[str] = UNSET
    limit_usd: Maybe[float] = UNSET
    scope: Maybe[str] = UNSET
    period: Maybe[BudgetPeriod] = UNSET
    alert_threshold: Maybe[float] = UNSET
    hard_stop: Maybe[bool] = UNSET
    is_active: Maybe[bool] = UNSET

    def changes(self) -> Json:
        """Mapa `campo -> valor` apenas com o que foi efetivamente informado."""
        candidates: dict[str, Maybe[Any]] = {
            "name": self.name,
            "limit_usd": self.limit_usd,
            "scope": self.scope,
            "period": self.period,
            "alert_threshold": self.alert_threshold,
            "hard_stop": self.hard_stop,
            "is_active": self.is_active,
        }
        changed: Json = {}
        for name, value in candidates.items():
            if not is_set(value):
                continue
            changed[name] = _validate_scope(str(value)) if name == "scope" else value
        return changed


@dataclass(frozen=True, slots=True)
class BudgetFilter:
    """Filtros de listagem de orcamentos."""

    scope: str | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Situacao corrente de um orcamento, com a janela de apuracao explicita."""

    budget: Budget
    check: BudgetCheck
    period_start: datetime
    period_end: datetime

    def to_dict(self) -> Json:
        """Serializa `BudgetCheck` com o contexto do orcamento no mesmo nivel."""
        payload: Json = self.check.model_dump(mode="json")
        payload.update(
            {
                "budget_id": self.budget.id,
                "budget_name": self.budget.name,
                "scope": self.budget.scope,
                "period": self.budget.period.value,
                "hard_stop": self.budget.hard_stop,
                "is_active": self.budget.is_active,
                "alert_threshold": self.budget.alert_threshold,
                "period_start": self.period_start.isoformat(),
                "period_end": self.period_end.isoformat(),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class PriceTable:
    """Tabela de precos por modelo em uso nesta instancia."""

    prices: list[ModelPrice]
    default_input_usd_per_1k: float
    default_output_usd_per_1k: float
    currency: str = "USD"

    def to_dict(self) -> Json:
        """Forma serializavel para `GET /finops/prices`."""
        return {
            "currency": self.currency,
            "default_input_usd_per_1k": self.default_input_usd_per_1k,
            "default_output_usd_per_1k": self.default_output_usd_per_1k,
            "prices": [price.model_dump(mode="json") for price in self.prices],
        }


# ---------------------------------------------------------------------------
# Base dos casos de uso
# ---------------------------------------------------------------------------
class _FinOpsUseCase:
    """Base dos casos de uso de FinOps: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container

    def price_table(self) -> PriceTable:
        """Fotografia da tabela de precos corrente."""
        calculator = self._container.cost_calculator
        default = calculator.default_price
        return PriceTable(
            prices=sorted(calculator.prices.values(), key=lambda price: price.model),
            default_input_usd_per_1k=default.input_usd_per_1k,
            default_output_usd_per_1k=default.output_usd_per_1k,
            currency=self._container.settings.finops.currency,
        )

    @staticmethod
    async def _require_budget(uow: UnitOfWork, budget_id: Id) -> Budget:
        """Carrega o orcamento ou levanta :class:`NotFoundError`."""
        found = await uow.budgets.get(budget_id)
        if found is None:
            raise NotFoundError(
                f"Orcamento '{budget_id}' nao encontrado.",
                details={"budget_id": budget_id},
            )
        return found


# ---------------------------------------------------------------------------
# Custo e consumo
# ---------------------------------------------------------------------------
class GetCostSummary(_FinOpsUseCase):
    """Resumo de custo do periodo, agregado por modulo e por modelo."""

    async def execute(self, filters: CostFilter, principal: Principal) -> CostSummary:
        """Devolve o `CostSummary` do recorte pedido (SPEC-0005 secao 4)."""
        authorize(principal, Permission.FINOPS_READ, "ler o resumo de custo")
        async with self._container.uow_factory() as uow:
            summary = await uow.usage.summary(
                since=filters.since,
                until=filters.until,
                module_slug=filters.module_slug,
                tenant_id=filters.tenant_id,
            )
        return self._flag_unknown_models(summary)

    def _flag_unknown_models(self, summary: CostSummary) -> CostSummary:
        """Nomeia em `unknown_models` os modelos do resumo sem preco cadastrado.

        A agregacao vem em SQL do repositorio, que nao tem como decidir isto: a
        tabela de precos vive no processo (`Settings` e `UpdatePrices`), nunca no
        banco (SPEC-0011). A lacuna e resolvida aqui, na aplicacao, onde o
        `CostCalculator` esta ao alcance e nenhum adaptador precisa conhecer o
        outro. Sem esta marcacao, um modelo novo custaria `0.00` pelo preco
        default e passaria por gratuito (SPEC-0005 secao 2).
        """
        calculator = self._container.cost_calculator
        unknown = sorted(model for model in summary.by_model if not calculator.is_known(model))
        if unknown:
            # `debug`, nao `warning`: esta e a leitura, nao o evento. A barra de
            # status do console chama este caso de uso a cada render de pagina, e
            # um warning aqui repetiria a mesma lista para sempre. Quem avisa e
            # `module_usage_unknown_model_price`, no momento da invocacao. Para
            # quem consome a resposta, o sinal e o proprio campo `unknown_models`.
            _logger.debug(
                "cost_summary_unknown_models",
                models=unknown,
                reason="modelos sem preco cadastrado: custo apurado com o preco default",
            )
        return summary.model_copy(update={"unknown_models": unknown})


class GetCostSeries(_FinOpsUseCase):
    """Serie temporal de custo por hora ou por dia, sem buracos no eixo do tempo."""

    async def execute(self, request: SeriesRequest, principal: Principal) -> list[Json]:
        """Devolve `[{"bucket", "cost_usd", "tokens", "runs"}]` do periodo pedido."""
        authorize(principal, Permission.FINOPS_READ, "ler a serie de custo")
        bucket = _validate_bucket(request.bucket)
        until = _as_utc(request.until) if request.until else utcnow()
        since = _as_utc(request.since) if request.since else until - _DEFAULT_WINDOW[bucket]
        if since > until:
            raise ValidationError(
                "O inicio da serie precisa ser anterior ao fim.",
                details={"since": since.isoformat(), "until": until.isoformat()},
            )
        records = await self._collect(
            since=since,
            until=until,
            module_slug=request.module_slug,
            tenant_id=request.tenant_id,
        )
        return build_series(records, bucket=bucket, since=since, until=until)

    async def _collect(
        self,
        *,
        since: datetime,
        until: datetime,
        module_slug: str | None,
        tenant_id: str | None,
    ) -> list[UsageRecord]:
        """Varre os registros do periodo em paginas, com teto de seguranca."""
        collected: list[UsageRecord] = []
        async with self._container.uow_factory() as uow:
            offset = 0
            while len(collected) < MAX_SERIES_RECORDS:
                page = await uow.usage.list(
                    since=since,
                    until=until,
                    module_slug=module_slug,
                    tenant_id=tenant_id,
                    limit=SERIES_PAGE_SIZE,
                    offset=offset,
                )
                if not page:
                    break
                collected.extend(page)
                if len(page) < SERIES_PAGE_SIZE:
                    break
                offset += SERIES_PAGE_SIZE
        if len(collected) >= MAX_SERIES_RECORDS:
            _logger.warning(
                "cost_series_truncated",
                records=len(collected),
                max_records=MAX_SERIES_RECORDS,
                since=since.isoformat(),
                until=until.isoformat(),
            )
        return collected


class ListUsage(_FinOpsUseCase):
    """Lista os registros de consumo paginados, do mais recente para o mais antigo."""

    async def execute(self, filters: UsageFilter, principal: Principal) -> Page[UsageRecord]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, Permission.FINOPS_READ, "listar registros de consumo")
        criteria: Json = {}
        if filters.since is not None:
            criteria["since"] = filters.since
        if filters.until is not None:
            criteria["until"] = filters.until
        if filters.module_slug:
            criteria["module_slug"] = filters.module_slug
        if filters.model:
            criteria["model"] = filters.model
        if filters.tenant_id:
            criteria["tenant_id"] = filters.tenant_id
        async with self._container.uow_factory() as uow:
            items = await uow.usage.list(**criteria, limit=filters.limit, offset=filters.offset)
            total = await uow.usage.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class RecordUsage(_FinOpsUseCase):
    """Grava um registro de consumo faturavel.

    `InvokeModule` ja registra o consumo das suas proprias chamadas (etapa 10 da
    SPEC-0001); este caso de uso atende quem consome LLM fora daquele caminho,
    como importacoes e reprocessamentos.
    """

    async def execute(self, data: UsageInput, principal: Principal) -> UsageRecord:
        """Calcula o custo quando nao informado e persiste o registro."""
        authorize(principal, Permission.FINOPS_WRITE, "registrar consumo")
        module_slug = data.module_slug.strip()
        model = data.model.strip()
        if not module_slug or not model:
            raise ValidationError(
                "O registro de consumo exige `module_slug` e `model`.",
                details={"module_slug": data.module_slug, "model": data.model},
            )
        calculator = self._container.cost_calculator
        cost = (
            round(float(data.cost_usd), COST_DIGITS)
            if data.cost_usd is not None
            else calculator.cost(model, data.usage)
        )
        if not calculator.is_known(model):
            _logger.warning(
                "usage_unknown_model_price",
                model=model,
                module=module_slug,
                cost_usd=cost,
                reason="modelo sem preco cadastrado: aplicado o preco default",
            )
        record = UsageRecord(
            run_id=data.run_id,
            module_slug=module_slug,
            model=model,
            usage=data.usage,
            cost_usd=cost,
            tenant_id=data.tenant_id or principal.tenant_id,
            occurred_at=_as_utc(data.occurred_at) if data.occurred_at else utcnow(),
        )
        async with self._container.uow_factory() as uow:
            stored = await uow.usage.add(record)
            await uow.commit()
        _logger.info(
            "usage_recorded",
            module=stored.module_slug,
            model=stored.model,
            cost_usd=stored.cost_usd,
            tokens=stored.usage.total_tokens,
        )
        return stored


# ---------------------------------------------------------------------------
# Tabela de precos
# ---------------------------------------------------------------------------
class GetPrices(_FinOpsUseCase):
    """Devolve a tabela de precos por modelo em uso nesta instancia."""

    async def execute(self, principal: Principal) -> PriceTable:
        """Fotografia da tabela corrente, incluindo os precos default."""
        authorize(principal, Permission.FINOPS_READ, "ler a tabela de precos")
        return self.price_table()


class UpdatePrices(_FinOpsUseCase):
    """Ajusta a tabela de precos da instancia em execucao.

    A fonte da verdade permanente e `LUKATO_FINOPS__PRICES` (12-factor): este
    caso de uso corrige a tabela do processo em andamento, sem persistir.
    """

    async def execute(self, prices: Sequence[ModelPrice], principal: Principal) -> PriceTable:
        """Insere ou substitui os precos informados e devolve a tabela resultante."""
        authorize(principal, Permission.FINOPS_WRITE, "alterar a tabela de precos")
        if not prices:
            raise ValidationError(
                "Informe ao menos um preco de modelo.",
                details={"prices": []},
            )
        calculator = self._container.cost_calculator
        applied: list[str] = []
        for price in prices:
            model = price.model.strip()
            if not model:
                raise ValidationError(
                    "Preco sem nome de modelo.",
                    details={"price": price.model_dump(mode="json")},
                )
            if price.input_usd_per_1k < 0.0 or price.output_usd_per_1k < 0.0:
                raise ValidationError(
                    f"Preco negativo para o modelo '{model}'.",
                    details={"price": price.model_dump(mode="json")},
                )
            calculator.upsert_price(price.model_copy(update={"model": model}))
            applied.append(model)
        _logger.info("prices_updated", models=sorted(applied), actor=principal.subject)
        return self.price_table()


# ---------------------------------------------------------------------------
# Orcamentos
# ---------------------------------------------------------------------------
class CreateBudget(_FinOpsUseCase):
    """Cria um orcamento de custo em um escopo."""

    async def execute(self, data: BudgetInput, principal: Principal) -> Budget:
        """Grava o orcamento validando escopo e limite."""
        authorize(principal, Permission.FINOPS_WRITE, "criar orcamentos")
        name = data.name.strip()
        if not name:
            raise ValidationError(
                "O orcamento precisa de um nome.",
                details={"field": "name"},
            )
        if data.limit_usd <= 0.0:
            raise ValidationError(
                "O limite do orcamento deve ser positivo.",
                details={"limit_usd": data.limit_usd},
            )
        budget = Budget(
            name=name,
            scope=_validate_scope(data.scope),
            limit_usd=round(float(data.limit_usd), COST_DIGITS),
            period=data.period,
            alert_threshold=data.alert_threshold,
            hard_stop=data.hard_stop,
            is_active=data.is_active,
        )
        async with self._container.uow_factory() as uow:
            stored = await uow.budgets.add(budget)
            await uow.commit()
        _logger.info(
            "budget_created",
            budget_id=stored.id,
            scope=stored.scope,
            limit_usd=stored.limit_usd,
            hard_stop=stored.hard_stop,
        )
        return stored


class ListBudgets(_FinOpsUseCase):
    """Lista os orcamentos cadastrados."""

    async def execute(self, filters: BudgetFilter, principal: Principal) -> list[Budget]:
        """Devolve os orcamentos que atendem aos filtros informados."""
        authorize(principal, Permission.FINOPS_READ, "listar orcamentos")
        async with self._container.uow_factory() as uow:
            items = await uow.budgets.list(scope=filters.scope, is_active=filters.is_active)
        return list(items)


class GetBudget(_FinOpsUseCase):
    """Busca um orcamento pelo identificador."""

    async def execute(self, budget_id: Id, principal: Principal) -> Budget:
        """Devolve o orcamento; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.FINOPS_READ, "ler orcamentos")
        async with self._container.uow_factory() as uow:
            return await self._require_budget(uow, budget_id)


class UpdateBudget(_FinOpsUseCase):
    """Atualiza parcialmente um orcamento existente."""

    async def execute(self, budget_id: Id, data: BudgetUpdateInput, principal: Principal) -> Budget:
        """Aplica somente os campos informados e grava o orcamento."""
        authorize(principal, Permission.FINOPS_WRITE, "alterar orcamentos")
        changes = data.changes()
        limit = changes.get("limit_usd")
        if limit is not None and float(limit) <= 0.0:
            raise ValidationError(
                "O limite do orcamento deve ser positivo.",
                details={"limit_usd": limit},
            )
        async with self._container.uow_factory() as uow:
            budget = await self._require_budget(uow, budget_id)
            if not changes:
                return budget
            updated = budget.model_copy(update={**changes, "updated_at": utcnow()})
            stored = await uow.budgets.update(updated)
            await uow.commit()
        _logger.info("budget_updated", budget_id=stored.id, fields=sorted(changes))
        return stored


class DeleteBudget(_FinOpsUseCase):
    """Remove um orcamento do catalogo."""

    async def execute(self, budget_id: Id, principal: Principal) -> None:
        """Apaga o orcamento; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.FINOPS_WRITE, "remover orcamentos")
        async with self._container.uow_factory() as uow:
            budget = await self._require_budget(uow, budget_id)
            await uow.budgets.delete(budget.id)
            await uow.commit()
        _logger.info("budget_deleted", budget_id=budget.id, scope=budget.scope)


class GetBudgetStatus(_FinOpsUseCase):
    """Situacao corrente de um orcamento: gasto do periodo contra o limite."""

    async def execute(self, budget_id: Id, principal: Principal) -> BudgetStatus:
        """Compara o gasto acumulado da janela corrente com o limite do orcamento.

        `alert` acende em `alert_threshold` sem bloquear; `blocked` so e verdadeiro
        com `hard_stop` ativo e consumo em 100% ou mais (SPEC-0005 secao 2).
        """
        authorize(principal, Permission.FINOPS_READ, "consultar orcamentos")
        now = utcnow()
        async with self._container.uow_factory() as uow:
            budget = await self._require_budget(uow, budget_id)
            start = period_start(budget.period, now=now)
            spent = await uow.usage.total_since(start, scope=budget.scope)
        check = self._container.cost_calculator.check_budget(budget, spent)
        if check.blocked:
            _logger.warning(
                "budget_blocked",
                budget_id=budget.id,
                scope=budget.scope,
                ratio=check.ratio,
                spent=check.spent,
                limit_usd=check.limit_usd,
            )
        elif check.alert:
            _logger.info(
                "budget_alert",
                budget_id=budget.id,
                scope=budget.scope,
                ratio=check.ratio,
                spent=check.spent,
            )
        return BudgetStatus(budget=budget, check=check, period_start=start, period_end=now)
