"""Schemas do recurso `/api/v1/finops`: custo, consumo, precos e orcamentos.

O custo nasce do consumo de tokens convertido pela tabela de precos por modelo. O
orcamento e o freio: `alert_threshold` avisa e `hard_stop` impede a proxima
invocacao quando o limite ja foi ultrapassado.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from lukato.application.dto import UNSET, Maybe
from lukato.application.use_cases.finops import (
    BudgetInput,
    BudgetStatus,
    BudgetUpdateInput,
    PriceTable,
    UsageInput,
)
from lukato.domain.models.finops import Budget, BudgetPeriod, CostSummary, ModelPrice, UsageRecord
from lukato.domain.models.run import TokenUsage
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import InSchema, OutSchema, TokenUsageOut

__all__ = [
    "BudgetCreate",
    "BudgetOut",
    "BudgetStatusOut",
    "BudgetUpdate",
    "CostPointOut",
    "CostSeriesResponse",
    "CostSummaryOut",
    "ModelPriceIn",
    "ModelPriceOut",
    "PriceTableOut",
    "PriceTableUpdate",
    "TokenUsageIn",
    "UsageCreate",
    "UsageRecordOut",
]


# ---------------------------------------------------------------------------
# Custo
# ---------------------------------------------------------------------------
class CostSummaryOut(OutSchema):
    """Agregado de custo do periodo, por modulo e por modelo."""

    total_usd: float = Field(default=0.0, ge=0.0, description="Custo total em USD.")
    total_tokens: int = Field(default=0, ge=0, description="Tokens consumidos no periodo.")
    runs: int = Field(default=0, ge=0, description="Execucoes distintas no periodo.")
    by_module: dict[str, float] = Field(default_factory=dict, description="Custo por modulo.")
    by_model: dict[str, float] = Field(default_factory=dict, description="Custo por modelo.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_usd": 1.4382,
                "total_tokens": 512340,
                "runs": 128,
                "by_module": {"atendimento": 1.2},
                "by_model": {"qwen-latest": 1.2},
            }
        }
    )

    @classmethod
    def from_domain(cls, summary: CostSummary) -> CostSummaryOut:
        """Converte o agregado de dominio."""
        return cls(
            total_usd=summary.total_usd,
            total_tokens=summary.total_tokens,
            runs=summary.runs,
            by_module=dict(summary.by_module),
            by_model=dict(summary.by_model),
        )


class CostPointOut(OutSchema):
    """Ponto da serie temporal de custo (um balde de hora ou de dia)."""

    bucket: str = Field(description="Inicio do balde em ISO-8601 UTC.")
    cost_usd: float = Field(default=0.0, ge=0.0, description="Custo do balde em USD.")
    tokens: int = Field(default=0, ge=0, description="Tokens consumidos no balde.")
    runs: int = Field(default=0, ge=0, description="Execucoes distintas no balde.")

    @classmethod
    def from_result(cls, point: Json) -> CostPointOut:
        """Converte um ponto devolvido por `GetCostSeries`."""
        return cls(
            bucket=str(point.get("bucket", "")),
            cost_usd=float(point.get("cost_usd", 0.0) or 0.0),
            tokens=int(point.get("tokens", 0) or 0),
            runs=int(point.get("runs", 0) or 0),
        )


class CostSeriesResponse(OutSchema):
    """Serie temporal completa, sem buracos no eixo do tempo."""

    bucket: str = Field(default="hour", description="Granularidade pedida: `hour` ou `day`.")
    points: list[CostPointOut] = Field(default_factory=list, description="Pontos em ordem.")
    total_usd: float = Field(default=0.0, ge=0.0, description="Soma do custo dos pontos.")
    total_tokens: int = Field(default=0, ge=0, description="Soma dos tokens dos pontos.")

    @classmethod
    def from_result(cls, bucket: str, points: list[Json]) -> CostSeriesResponse:
        """Converte a lista devolvida por `GetCostSeries` e soma os totais."""
        items = [CostPointOut.from_result(point) for point in points]
        return cls(
            bucket=bucket,
            points=items,
            total_usd=round(sum(item.cost_usd for item in items), 6),
            total_tokens=sum(item.tokens for item in items),
        )


# ---------------------------------------------------------------------------
# Consumo
# ---------------------------------------------------------------------------
class TokenUsageIn(InSchema):
    """Consumo de tokens informado por um registro manual."""

    prompt_tokens: int = Field(default=0, ge=0, description="Tokens enviados ao modelo.")
    completion_tokens: int = Field(default=0, ge=0, description="Tokens gerados pelo modelo.")
    total_tokens: int = Field(default=0, ge=0, description="Total; ausente soma os dois campos.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"prompt_tokens": 312, "completion_tokens": 88, "total_tokens": 400}
        },
    )

    def to_domain(self) -> TokenUsage:
        """Converte para o value object de dominio."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
        )


class UsageCreate(InSchema):
    """Corpo de `POST /api/v1/finops/usage`: consumo gerado fora de `InvokeModule`."""

    module_slug: str = Field(min_length=1, description="Modulo que consumiu.")
    model: str = Field(min_length=1, description="Modelo cobrado.")
    usage: TokenUsageIn = Field(default_factory=TokenUsageIn, description="Tokens consumidos.")
    run_id: Id | None = Field(default=None, description="Execucao associada, quando houver.")
    cost_usd: float | None = Field(
        default=None, ge=0.0, description="Custo ja apurado; ausente usa a tabela de precos."
    )
    tenant_id: str = Field(default="default", description="Inquilino dono do consumo.")
    occurred_at: datetime | None = Field(default=None, description="Instante do consumo.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "module_slug": "atendimento",
                "model": "qwen-latest",
                "usage": {"prompt_tokens": 312, "completion_tokens": 88},
                "tenant_id": "default",
            }
        },
    )

    def to_input(self) -> UsageInput:
        """Converte para o DTO do caso de uso `RecordUsage`."""
        return UsageInput(
            module_slug=self.module_slug,
            model=self.model,
            usage=self.usage.to_domain(),
            run_id=self.run_id,
            cost_usd=self.cost_usd,
            tenant_id=self.tenant_id,
            occurred_at=self.occurred_at,
        )


class UsageRecordOut(OutSchema):
    """Registro de consumo faturavel."""

    id: Id
    run_id: Id | None = None
    module_slug: str
    model: str
    usage: TokenUsageOut = Field(default_factory=TokenUsageOut)
    cost_usd: float = Field(default=0.0, ge=0.0)
    tenant_id: str = "default"
    occurred_at: datetime

    @classmethod
    def from_domain(cls, record: UsageRecord) -> UsageRecordOut:
        """Converte o registro de dominio."""
        return cls(
            id=record.id,
            run_id=record.run_id,
            module_slug=record.module_slug,
            model=record.model,
            usage=TokenUsageOut.from_domain(record.usage),
            cost_usd=record.cost_usd,
            tenant_id=record.tenant_id,
            occurred_at=record.occurred_at,
        )


# ---------------------------------------------------------------------------
# Precos
# ---------------------------------------------------------------------------
class ModelPriceIn(InSchema):
    """Preco de um modelo, em USD por 1k tokens."""

    model: str = Field(min_length=1, description="Nome do modelo cobrado.")
    input_usd_per_1k: float = Field(
        default=0.0, ge=0.0, description="USD por 1k tokens de entrada."
    )
    output_usd_per_1k: float = Field(default=0.0, ge=0.0, description="USD por 1k tokens de saida.")
    currency: str = Field(default="USD", description="Moeda da tabela.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "model": "qwen-latest",
                "input_usd_per_1k": 0.0006,
                "output_usd_per_1k": 0.0018,
                "currency": "USD",
            }
        },
    )

    def to_domain(self) -> ModelPrice:
        """Converte para o value object de dominio."""
        return ModelPrice(
            model=self.model,
            input_usd_per_1k=self.input_usd_per_1k,
            output_usd_per_1k=self.output_usd_per_1k,
            currency=self.currency,
        )


class PriceTableUpdate(InSchema):
    """Corpo de `PUT /api/v1/finops/prices`: substitui a tabela inteira."""

    prices: list[ModelPriceIn] = Field(
        default_factory=list, description="Tabela completa de precos por modelo."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "prices": [
                    {
                        "model": "qwen-latest",
                        "input_usd_per_1k": 0.0006,
                        "output_usd_per_1k": 0.0018,
                    }
                ]
            }
        },
    )

    def to_domain(self) -> list[ModelPrice]:
        """Converte a tabela para os value objects de dominio."""
        return [price.to_domain() for price in self.prices]


class ModelPriceOut(OutSchema):
    """Preco vigente de um modelo."""

    model: str
    input_usd_per_1k: float = 0.0
    output_usd_per_1k: float = 0.0
    currency: str = "USD"

    @classmethod
    def from_domain(cls, price: ModelPrice) -> ModelPriceOut:
        """Converte o value object de dominio."""
        return cls(
            model=price.model,
            input_usd_per_1k=price.input_usd_per_1k,
            output_usd_per_1k=price.output_usd_per_1k,
            currency=price.currency,
        )


class PriceTableOut(OutSchema):
    """Tabela de precos em uso nesta instalacao."""

    currency: str = Field(default="USD", description="Moeda da tabela.")
    default_input_usd_per_1k: float = Field(
        default=0.0, ge=0.0, description="Preco de entrada para modelo sem tabela."
    )
    default_output_usd_per_1k: float = Field(
        default=0.0, ge=0.0, description="Preco de saida para modelo sem tabela."
    )
    prices: list[ModelPriceOut] = Field(default_factory=list, description="Precos por modelo.")

    @classmethod
    def from_result(cls, table: PriceTable) -> PriceTableOut:
        """Converte o DTO do caso de uso `GetPrices`."""
        return cls(
            currency=table.currency,
            default_input_usd_per_1k=table.default_input_usd_per_1k,
            default_output_usd_per_1k=table.default_output_usd_per_1k,
            prices=[ModelPriceOut.from_domain(price) for price in table.prices],
        )


# ---------------------------------------------------------------------------
# Orcamentos
# ---------------------------------------------------------------------------
class BudgetCreate(InSchema):
    """Corpo de `POST /api/v1/finops/budgets`."""

    name: str = Field(min_length=1, description="Nome do orcamento.")
    limit_usd: float = Field(gt=0.0, description="Teto de gasto do periodo, em USD.")
    scope: str = Field(
        default="global", description="Escopo: `global`, `module:<slug>` ou `tenant:<id>`."
    )
    period: BudgetPeriod = Field(
        default=BudgetPeriod.MONTHLY, description="Janela de apuracao do teto."
    )
    alert_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0, description="Fracao do teto que dispara o alerta."
    )
    hard_stop: bool = Field(
        default=False, description="True impede a proxima invocacao apos estourar."
    )
    is_active: bool = Field(default=True, description="Orcamento inativo nao e avaliado.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "name": "Atendimento — mensal",
                "limit_usd": 50.0,
                "scope": "module:atendimento",
                "period": "monthly",
                "alert_threshold": 0.8,
                "hard_stop": True,
                "is_active": True,
            }
        },
    )

    def to_input(self) -> BudgetInput:
        """Converte para o DTO do caso de uso `CreateBudget`."""
        return BudgetInput(
            name=self.name,
            limit_usd=self.limit_usd,
            scope=self.scope,
            period=self.period,
            alert_threshold=self.alert_threshold,
            hard_stop=self.hard_stop,
            is_active=self.is_active,
        )


class BudgetUpdate(InSchema):
    """Corpo de `PUT /api/v1/finops/budgets/{id}`: so muda o que foi enviado."""

    name: str | None = None
    limit_usd: float | None = Field(default=None, gt=0.0)
    scope: str | None = None
    period: BudgetPeriod | None = None
    alert_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    hard_stop: bool | None = None
    is_active: bool | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"limit_usd": 80.0, "hard_stop": False}},
    )

    def to_input(self) -> BudgetUpdateInput:
        """Converte para o DTO parcial, preservando o que nao foi enviado."""
        sent = self.model_fields_set

        def maybe(field: str, value: Any) -> Maybe[Any]:
            return value if field in sent else UNSET

        return BudgetUpdateInput(
            name=maybe("name", self.name),
            limit_usd=maybe("limit_usd", self.limit_usd),
            scope=maybe("scope", self.scope),
            period=maybe("period", self.period),
            alert_threshold=maybe("alert_threshold", self.alert_threshold),
            hard_stop=maybe("hard_stop", self.hard_stop),
            is_active=maybe("is_active", self.is_active),
        )


class BudgetOut(OutSchema):
    """Orcamento devolvido pela API."""

    id: Id
    name: str
    scope: str = "global"
    limit_usd: float
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    alert_threshold: float = 0.8
    hard_stop: bool = False
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, budget: Budget) -> BudgetOut:
        """Converte a entidade de dominio."""
        return cls(
            id=budget.id,
            name=budget.name,
            scope=budget.scope,
            limit_usd=budget.limit_usd,
            period=budget.period,
            alert_threshold=budget.alert_threshold,
            hard_stop=budget.hard_stop,
            is_active=budget.is_active,
            created_at=budget.created_at,
            updated_at=budget.updated_at,
        )


class BudgetStatusOut(OutSchema):
    """Situacao corrente de um orcamento, com a janela de apuracao explicita."""

    budget_id: Id
    budget_name: str = ""
    scope: str = "global"
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    ok: bool = Field(default=True, description="False quando o teto ja foi ultrapassado.")
    ratio: float = Field(default=0.0, ge=0.0, description="Fracao do teto ja consumida.")
    alert: bool = Field(default=False, description="True a partir de `alert_threshold`.")
    blocked: bool = Field(default=False, description="True quando `hard_stop` ja impede a chamada.")
    spent: float = Field(default=0.0, ge=0.0, description="Gasto apurado na janela, em USD.")
    remaining: float = Field(default=0.0, description="Saldo restante na janela, em USD.")
    limit_usd: float = Field(default=0.0, ge=0.0, description="Teto configurado.")
    hard_stop: bool = False
    is_active: bool = True
    alert_threshold: float = 0.8
    period_start: datetime
    period_end: datetime

    @classmethod
    def from_result(cls, status: BudgetStatus) -> BudgetStatusOut:
        """Converte o DTO do caso de uso `GetBudgetStatus`."""
        check = status.check
        return cls(
            budget_id=status.budget.id,
            budget_name=status.budget.name,
            scope=status.budget.scope,
            period=status.budget.period,
            ok=check.ok,
            ratio=check.ratio,
            alert=check.alert,
            blocked=check.blocked,
            spent=check.spent,
            remaining=check.remaining,
            limit_usd=check.limit_usd,
            hard_stop=status.budget.hard_stop,
            is_active=status.budget.is_active,
            alert_threshold=status.budget.alert_threshold,
            period_start=status.period_start,
            period_end=status.period_end,
        )
