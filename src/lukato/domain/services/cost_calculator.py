"""Calculo de custo, agregacao de consumo e verificacao de orcamento (SPEC-0005)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from lukato.domain.models.base import DomainModel
from lukato.domain.models.finops import Budget, CostSummary, ModelPrice, UsageRecord
from lukato.domain.models.run import TokenUsage
from lukato.domain.types import Id, Json

__all__ = ["BudgetCheck", "CostCalculator"]

_COST_DIGITS = 8
"""Casas decimais do custo em USD; a UI formata com 5."""

_RATIO_DIGITS = 6
_CHARS_PER_TOKEN = 4
"""Heuristica de estimativa quando o provedor nao reporta tokens."""


class BudgetCheck(DomainModel):
    """Situacao corrente de um orcamento diante do valor ja gasto."""

    ok: bool
    ratio: float
    alert: bool
    blocked: bool
    remaining: float
    spent: float
    limit_usd: float


class CostCalculator:
    """Converte consumo de tokens em USD e agrega o resultado por modulo e modelo."""

    def __init__(
        self,
        prices: Mapping[str, ModelPrice] | None = None,
        *,
        default_input: float = 0.0,
        default_output: float = 0.0,
    ) -> None:
        self._prices: dict[str, ModelPrice] = dict(prices) if prices else {}
        self._lowercase: dict[str, str] = {key.lower(): key for key in self._prices}
        self._default_input = float(default_input)
        self._default_output = float(default_output)

    @property
    def prices(self) -> Mapping[str, ModelPrice]:
        """Tabela de precos corrente (somente leitura)."""
        return MappingProxyType(self._prices)

    @property
    def default_price(self) -> ModelPrice:
        """Preco aplicado a modelos ausentes da tabela."""
        return ModelPrice(
            model="",
            input_usd_per_1k=self._default_input,
            output_usd_per_1k=self._default_output,
        )

    def upsert_price(self, price: ModelPrice) -> None:
        """Insere ou substitui o preco de um modelo."""
        self._prices[price.model] = price
        self._lowercase[price.model.lower()] = price.model

    def is_known(self, model: str) -> bool:
        """True quando o modelo (ou o seu prefixo de provedor) tem preco cadastrado."""
        return self._lookup(model) is not None

    def price_for(self, model: str) -> ModelPrice:
        """Resolve o preco: match exato, depois prefixo antes de `/`, depois default."""
        found = self._lookup(model)
        if found is not None:
            return found
        return ModelPrice(
            model=model,
            input_usd_per_1k=self._default_input,
            output_usd_per_1k=self._default_output,
        )

    def cost(self, model: str, usage: TokenUsage) -> float:
        """Custo em USD de um consumo: `(tokens/1000) * preco`, com 8 casas decimais."""
        price = self.price_for(model)
        prompt_tokens = max(0, usage.prompt_tokens)
        completion_tokens = max(0, usage.completion_tokens)
        total = (prompt_tokens / 1000.0) * price.input_usd_per_1k + (
            completion_tokens / 1000.0
        ) * price.output_usd_per_1k
        return round(total, _COST_DIGITS)

    def estimate_usage(self, prompt_text: str, completion_text: str) -> TokenUsage:
        """Estima tokens por `len(texto)/4` quando o provedor nao reporta consumo."""
        return TokenUsage.of(_estimate_tokens(prompt_text), _estimate_tokens(completion_text))

    def summarize(self, records: Iterable[UsageRecord]) -> CostSummary:
        """Agrega registros de consumo por modulo e por modelo.

        `runs` conta execucoes distintas: registros sem `run_id` contam como uma
        execucao propria, pois nao ha como agrupa-los.
        """
        total_usd = 0.0
        total_tokens = 0
        by_module: dict[str, float] = {}
        by_model: dict[str, float] = {}
        run_ids: set[Id] = set()
        orphan_runs = 0
        unknown: set[str] = set()

        for record in records:
            cost = float(record.cost_usd)
            total_usd += cost
            total_tokens += record.usage.total_tokens
            by_module[record.module_slug] = by_module.get(record.module_slug, 0.0) + cost
            by_model[record.model] = by_model.get(record.model, 0.0) + cost
            if record.run_id is None:
                orphan_runs += 1
            else:
                run_ids.add(record.run_id)
            if not self.is_known(record.model):
                unknown.add(record.model)

        payload: Json = {
            "total_usd": round(total_usd, _COST_DIGITS),
            "total_tokens": total_tokens,
            "runs": len(run_ids) + orphan_runs,
            "by_module": {key: round(value, _COST_DIGITS) for key, value in by_module.items()},
            "by_model": {key: round(value, _COST_DIGITS) for key, value in by_model.items()},
        }
        payload["unknown_models"] = sorted(unknown)
        return CostSummary(**payload)

    def unknown_models(self, records: Iterable[UsageRecord]) -> frozenset[str]:
        """Modelos dos registros que nao possuem preco cadastrado.

        `summarize` ja devolve a mesma informacao em `CostSummary.unknown_models`;
        este metodo continua util para inspecionar um lote de registros sem montar
        o agregado inteiro.
        """
        return frozenset(record.model for record in records if not self.is_known(record.model))

    def check_budget(self, budget: Budget, spent: float) -> BudgetCheck:
        """Compara o gasto com o limite e diz se alerta ou bloqueia.

        `blocked` so e verdadeiro com `hard_stop` ativo e consumo em 100% ou mais.
        Orcamento inativo nunca alerta nem bloqueia.
        """
        limit = float(budget.limit_usd)
        spent_value = float(spent)
        ratio = _consumption_ratio(spent_value, limit)
        active = budget.is_active
        return BudgetCheck(
            ok=(not active) or ratio < 1.0,
            ratio=round(ratio, _RATIO_DIGITS),
            alert=active and ratio >= budget.alert_threshold,
            blocked=active and budget.hard_stop and ratio >= 1.0,
            remaining=round(max(0.0, limit - spent_value), _COST_DIGITS),
            spent=round(spent_value, _COST_DIGITS),
            limit_usd=round(limit, _COST_DIGITS),
        )

    def _lookup(self, model: str) -> ModelPrice | None:
        """Busca o preco por match exato, sem caixa e, por fim, pelo prefixo do provedor."""
        direct = self._exact(model)
        if direct is not None:
            return direct
        prefix, separator, _ = model.partition("/")
        if separator and prefix:
            return self._exact(prefix)
        return None

    def _exact(self, key: str) -> ModelPrice | None:
        """Match exato e, como cortesia, match sem diferenciar maiusculas."""
        found = self._prices.get(key)
        if found is not None:
            return found
        canonical = self._lowercase.get(key.lower())
        return self._prices.get(canonical) if canonical is not None else None


def _consumption_ratio(spent: float, limit: float) -> float:
    """Fracao consumida do orcamento; limite nao positivo conta como ja esgotado."""
    if limit > 0.0:
        return spent / limit
    return 1.0 if spent > 0.0 else 0.0


def _estimate_tokens(text: str) -> int:
    """Estima o numero de tokens de um texto por comprimento (4 caracteres por token)."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)
