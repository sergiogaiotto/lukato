"""Avaliadores de tamanho: `max_length` (teto) e `min_length` (piso).

O teto aceita limite em caracteres e/ou em tokens (aproximacao normativa da
SPEC-0003: 1 token ~ 4 caracteres). Quando a acao da regra e `TRANSFORM` (ou
`REDACT`), o texto truncado — sempre em fronteira de palavra, via
`lukato.domain.services.text_normalizer.truncate_words` — viaja em
`finding.evidence` e quem substitui o conteudo e o motor.
"""

from __future__ import annotations

from typing import Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    config_int,
    snippet,
)
from lukato.domain.errors import ValidationError
from lukato.domain.models.guardrail import (
    GuardrailFinding,
    GuardrailRule,
    GuardrailRuleKind,
)
from lukato.domain.services.text_normalizer import truncate_words
from lukato.domain.types import Json

__all__ = [
    "CHARS_PER_TOKEN",
    "MaxLengthEvaluator",
    "MinLengthEvaluator",
    "estimate_tokens",
    "validate_rule",
]

CHARS_PER_TOKEN: Final[int] = 4
"""Aproximacao normativa usada para converter caracteres em tokens."""


def estimate_tokens(text: str) -> int:
    """Estima o numero de tokens de um texto (`len // 4`, SPEC-0003 secao 3)."""
    return len(text) // CHARS_PER_TOKEN


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `max_length` ou `min_length`."""
    if rule.kind is GuardrailRuleKind.MIN_LENGTH:
        minimum = config_int(rule.config, "min_chars", rule_id=rule.id, minimum=1)
        if minimum is None:
            raise ValidationError(
                f"A regra min_length '{rule.id}' exige 'min_chars' >= 1.",
                details={"rule_id": rule.id},
            )
        return

    max_chars = config_int(rule.config, "max_chars", rule_id=rule.id, minimum=1)
    max_tokens = config_int(rule.config, "max_tokens", rule_id=rule.id, minimum=1)
    if max_chars is None and max_tokens is None:
        raise ValidationError(
            f"A regra max_length '{rule.id}' exige 'max_chars' e/ou 'max_tokens'.",
            details={"rule_id": rule.id},
        )


class MaxLengthEvaluator:
    """`max_length`: recusa (ou trunca) conteudo acima do teto configurado."""

    kind: GuardrailRuleKind = GuardrailRuleKind.MAX_LENGTH

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Compara tamanho e tokens estimados contra os limites da regra."""
        validate_rule(rule)
        max_chars = config_int(rule.config, "max_chars", rule_id=rule.id, minimum=1)
        max_tokens = config_int(rule.config, "max_tokens", rule_id=rule.id, minimum=1)

        limits: list[int] = []
        if max_chars is not None:
            limits.append(max_chars)
        if max_tokens is not None:
            limits.append(max_tokens * CHARS_PER_TOKEN)
        limit = min(limits)

        length = len(content)
        tokens = estimate_tokens(content)
        over_chars = max_chars is not None and length > max_chars
        over_tokens = max_tokens is not None and tokens > max_tokens
        if not (over_chars or over_tokens):
            return None

        span = (limit, length) if length > limit else None
        if rule.action in CONTENT_ACTIONS:
            truncated = truncate_words(content, limit)
            message = (
                f"Conteudo truncado de {length} para {len(truncated)} caracteres (limite {limit})."
            )
            return build_finding(rule, message, evidence=truncated, span=span)

        message = (
            f"Conteudo com {length} caracteres (~{tokens} tokens) excede o limite de "
            f"{limit} caracteres."
        )
        evidence = snippet(f"{length} caracteres / ~{tokens} tokens (limite {limit}).")
        return build_finding(rule, message, evidence=evidence, span=span)


class MinLengthEvaluator:
    """`min_length`: recusa conteudo curto demais (espacos nas pontas nao contam)."""

    kind: GuardrailRuleKind = GuardrailRuleKind.MIN_LENGTH

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Compara o tamanho util do conteudo com o piso configurado."""
        validate_rule(rule)
        min_chars = config_int(rule.config, "min_chars", rule_id=rule.id, minimum=1) or 1
        length = len(content.strip())
        if length >= min_chars:
            return None

        message = f"Conteudo com {length} caracteres uteis abaixo do minimo de {min_chars}."
        # Nao ha como "completar" um texto curto: em acao de conteudo nada muda.
        evidence = content if rule.action in CONTENT_ACTIONS else snippet(content)
        return build_finding(rule, message, evidence=evidence, span=(0, len(content)))
