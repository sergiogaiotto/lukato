"""Montagem do catalogo completo de avaliadores de guardrail.

`build_default_evaluators` devolve uma instancia de **cada** um dos onze `kind` da
SPEC-0003, pronta para ser injetada no `GuardrailEngine` do dominio. Nada aqui abre
conexao: o unico avaliador que fala com a rede (`llm_judge`) recebe a porta de LLM
por injecao e degrada para `WARN` quando ela e `None` — o pacote inteiro continua
importavel e funcional offline.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

from lukato.adapters.guardrails.keywords import KeywordBlockEvaluator
from lukato.adapters.guardrails.keywords import validate_rule as validate_keyword_rule
from lukato.adapters.guardrails.language import LanguageAllowEvaluator
from lukato.adapters.guardrails.language import validate_rule as validate_language_rule
from lukato.adapters.guardrails.length import MaxLengthEvaluator, MinLengthEvaluator
from lukato.adapters.guardrails.length import validate_rule as validate_length_rule
from lukato.adapters.guardrails.llm_judge import (
    DEFAULT_TIMEOUT_SECONDS,
    LlmJudgeEvaluator,
)
from lukato.adapters.guardrails.llm_judge import validate_rule as validate_judge_rule
from lukato.adapters.guardrails.pii import PiiRedactEvaluator
from lukato.adapters.guardrails.pii import validate_rule as validate_pii_rule
from lukato.adapters.guardrails.regex_rules import RegexBlockEvaluator, RegexRequireEvaluator
from lukato.adapters.guardrails.regex_rules import validate_rule as validate_regex_rule
from lukato.adapters.guardrails.schema_json import JsonSchemaEvaluator
from lukato.adapters.guardrails.schema_json import validate_rule as validate_schema_rule
from lukato.adapters.guardrails.secrets_scan import SecretScanEvaluator
from lukato.adapters.guardrails.secrets_scan import validate_rule as validate_secret_rule
from lukato.adapters.guardrails.topic import TopicBlockEvaluator
from lukato.adapters.guardrails.topic import validate_rule as validate_topic_rule
from lukato.config import Settings
from lukato.domain.errors import ValidationError
from lukato.domain.models.guardrail import GuardrailPolicy, GuardrailRule, GuardrailRuleKind
from lukato.domain.ports.guardrail import GuardrailRuleEvaluator
from lukato.domain.ports.llm import LLMPort

__all__ = [
    "RULE_VALIDATORS",
    "build_default_evaluators",
    "evaluator_kinds",
    "validate_policy",
    "validate_rule",
]

RULE_VALIDATORS: Final[dict[GuardrailRuleKind, Callable[[GuardrailRule], None]]] = {
    GuardrailRuleKind.REGEX_BLOCK: validate_regex_rule,
    GuardrailRuleKind.REGEX_REQUIRE: validate_regex_rule,
    GuardrailRuleKind.KEYWORD_BLOCK: validate_keyword_rule,
    GuardrailRuleKind.MAX_LENGTH: validate_length_rule,
    GuardrailRuleKind.MIN_LENGTH: validate_length_rule,
    GuardrailRuleKind.PII_REDACT: validate_pii_rule,
    GuardrailRuleKind.SECRET_SCAN: validate_secret_rule,
    GuardrailRuleKind.JSON_SCHEMA: validate_schema_rule,
    GuardrailRuleKind.LANGUAGE_ALLOW: validate_language_rule,
    GuardrailRuleKind.TOPIC_BLOCK: validate_topic_rule,
    GuardrailRuleKind.LLM_JUDGE: validate_judge_rule,
}
"""Validador de config por tipo de regra (usado antes de persistir uma politica)."""


def build_default_evaluators(
    llm: LLMPort | None = None, *, settings: Settings | None = None
) -> list[GuardrailRuleEvaluator]:
    """Cria uma instancia de cada avaliador do catalogo (SPEC-0003, secao 3).

    `llm` alimenta apenas o `llm_judge`; `settings` ajusta o teto de latencia e o
    modelo padrao do juiz. Ambos sao opcionais para que o catalogo possa ser montado
    em teste e em execucao offline, sem nenhuma dependencia externa.
    """
    timeout = DEFAULT_TIMEOUT_SECONDS
    model: str | None = None
    if settings is not None:
        model = settings.llm.model or None
        if settings.llm.timeout > 0:
            timeout = min(float(settings.llm.timeout), DEFAULT_TIMEOUT_SECONDS)

    evaluators: list[GuardrailRuleEvaluator] = [
        RegexBlockEvaluator(),
        RegexRequireEvaluator(),
        KeywordBlockEvaluator(),
        MaxLengthEvaluator(),
        MinLengthEvaluator(),
        PiiRedactEvaluator(),
        SecretScanEvaluator(),
        JsonSchemaEvaluator(),
        LanguageAllowEvaluator(),
        TopicBlockEvaluator(),
        LlmJudgeEvaluator(llm, timeout=timeout, model=model),
    ]
    return evaluators


def evaluator_kinds(evaluators: Sequence[GuardrailRuleEvaluator]) -> set[GuardrailRuleKind]:
    """Conjunto de tipos de regra cobertos por uma lista de avaliadores."""
    return {evaluator.kind for evaluator in evaluators}


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra qualquer, despachando pelo `kind`."""
    validator = RULE_VALIDATORS.get(rule.kind)
    if validator is None:
        raise ValidationError(
            f"Nenhum validador conhecido para regras do tipo '{rule.kind.value}'.",
            details={"rule_id": rule.id, "kind": rule.kind.value},
        )
    validator(rule)


def validate_policy(policy: GuardrailPolicy) -> None:
    """Valida a politica inteira: ids unicos e config de cada regra habilitada."""
    seen: set[str] = set()
    duplicated: set[str] = set()
    for rule in policy.rules:
        if rule.id in seen:
            duplicated.add(rule.id)
        seen.add(rule.id)
    if duplicated:
        raise ValidationError(
            f"A politica '{policy.slug}' repete os ids de regra: {', '.join(sorted(duplicated))}.",
            details={"policy_slug": policy.slug, "duplicated": sorted(duplicated)},
        )
    for rule in policy.rules:
        if rule.enabled:
            validate_rule(rule)
