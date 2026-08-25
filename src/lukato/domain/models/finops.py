"""Modelos de FinOps: precos, registros de consumo, orcamentos e resumos de custo."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.models.run import TokenUsage
from lukato.domain.types import DEFAULT_TENANT, Id, new_id, utcnow

__all__ = ["Budget", "BudgetPeriod", "CostSummary", "ModelPrice", "UsageRecord"]


class ModelPrice(DomainModel):
    """Tabela de preco de um modelo, em USD por 1k tokens."""

    model: str
    input_usd_per_1k: float = 0.0
    output_usd_per_1k: float = 0.0
    currency: str = "USD"


class UsageRecord(DomainModel):
    """Registro de consumo faturavel gerado por uma execucao."""

    id: Id = Field(default_factory=new_id)
    run_id: Id | None = None
    module_slug: str
    model: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    tenant_id: str = DEFAULT_TENANT
    occurred_at: datetime = Field(default_factory=utcnow)


class BudgetPeriod(StrEnum):
    """Janela de apuracao do orcamento."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    TOTAL = "total"


class Budget(Entity):
    """Orcamento aplicado a um escopo (`global`, `module:<slug>` ou `tenant:<id>`)."""

    name: str
    scope: str = "global"
    limit_usd: float
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    alert_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    hard_stop: bool = False
    is_active: bool = True


class CostSummary(DomainModel):
    """Agregado de custo por periodo, modulo e modelo."""

    total_usd: float = 0.0
    total_tokens: int = 0
    runs: int = 0
    by_module: dict[str, float] = Field(default_factory=dict)
    by_model: dict[str, float] = Field(default_factory=dict)
