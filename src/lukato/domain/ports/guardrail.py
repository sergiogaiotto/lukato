"""Portas de guardrails: avaliador de regra e aplicador de politica."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lukato.domain.models.guardrail import (
    GuardrailFinding,
    GuardrailPolicy,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailVerdict,
)
from lukato.domain.types import Json

__all__ = ["GuardrailPort", "GuardrailRuleEvaluator"]


class GuardrailRuleEvaluator(Protocol):
    """Avaliador de um tipo especifico de regra de guardrail."""

    kind: GuardrailRuleKind

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Avalia o conteudo contra a regra; devolve `None` quando nada e violado."""
        ...


@runtime_checkable
class GuardrailPort(Protocol):
    """Aplica uma politica de guardrail sobre um texto e devolve o veredito."""

    async def apply(
        self, content: str, policy: GuardrailPolicy | None, *, context: Json | None = None
    ) -> GuardrailVerdict:
        """Aplica a politica; politica `None` significa liberar o conteudo intacto."""
        ...
