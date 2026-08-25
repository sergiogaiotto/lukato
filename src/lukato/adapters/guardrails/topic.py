"""Avaliador `topic_block`: bloqueio por densidade de termos de um assunto.

Cada topico declara um nome e uma lista de termos (simples ou compostos). O
avaliador conta as ocorrencias desses termos no conteudo — comparacao sem acento e
sem caixa, reaproveitando o casamento de `keywords.py` — e dispara quando a
densidade de um topico alcanca o `threshold` (global ou proprio do topico). Contar
ocorrencias, e nao apenas presenca, evita bloquear uma mencao isolada e casual.
"""

from __future__ import annotations

from typing import Any, Final

from lukato.adapters.guardrails.keywords import keyword_spans
from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    config_bool,
    config_int,
    merge_spans,
    redact_spans,
    redaction_token,
    snippet,
)
from lukato.domain.errors import ValidationError
from lukato.domain.models.guardrail import (
    GuardrailFinding,
    GuardrailRule,
    GuardrailRuleKind,
)
from lukato.domain.types import Json

__all__ = [
    "DEFAULT_THRESHOLD",
    "TopicBlockEvaluator",
    "TopicHit",
    "score_topics",
    "validate_rule",
]

DEFAULT_THRESHOLD: Final[int] = 2
"""Densidade minima padrao para considerar o topico presente."""


class TopicHit:
    """Resultado da contagem de um topico no conteudo."""

    __slots__ = ("density", "name", "spans", "terms")

    def __init__(
        self, name: str, density: int, terms: list[str], spans: list[tuple[int, int]]
    ) -> None:
        self.name = name
        self.density = density
        self.terms = terms
        self.spans = spans

    def __repr__(self) -> str:
        return f"TopicHit(name={self.name!r}, density={self.density}, terms={self.terms!r})"


def _topics_of(rule: GuardrailRule) -> list[dict[str, Any]]:
    """Extrai e valida a lista de topicos declarada na regra."""
    raw = rule.config.get("topics")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError(
            f"'topics' da regra '{rule.id}' deve ser uma lista de objetos.",
            details={"rule_id": rule.id, "type": type(raw).__name__},
        )
    topics: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(
                f"O topico #{index + 1} da regra '{rule.id}' deve ser um objeto "
                "com 'name' e 'terms'.",
                details={"rule_id": rule.id, "index": index},
            )
        name = item.get("name")
        terms = item.get("terms")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(
                f"O topico #{index + 1} da regra '{rule.id}' precisa de 'name' textual.",
                details={"rule_id": rule.id, "index": index},
            )
        if not isinstance(terms, list) or not terms:
            raise ValidationError(
                f"O topico '{name}' da regra '{rule.id}' precisa de 'terms' nao vazio.",
                details={"rule_id": rule.id, "topic": name},
            )
        if any(not isinstance(term, str) for term in terms):
            raise ValidationError(
                f"O topico '{name}' da regra '{rule.id}' so aceita termos textuais.",
                details={"rule_id": rule.id, "topic": name},
            )
        topics.append(item)
    return topics


def score_topics(
    content: str,
    topics: list[dict[str, Any]],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    whole_word: bool = True,
) -> list[TopicHit]:
    """Conta as ocorrencias de cada topico e devolve os que alcancaram o limiar."""
    hits: list[TopicHit] = []
    for topic in topics:
        name = str(topic.get("name", "")).strip()
        terms = [str(term) for term in topic.get("terms", []) if str(term).strip()]
        local_threshold = topic.get("threshold", threshold)
        try:
            limit = max(1, int(local_threshold))
        except (TypeError, ValueError):
            limit = threshold

        matched_terms: list[str] = []
        spans: list[tuple[int, int]] = []
        for term in terms:
            found = keyword_spans(content, term, normalize=True, whole_word=whole_word)
            if found:
                matched_terms.append(term)
                spans.extend(found)
        if spans and len(spans) >= limit:
            hits.append(TopicHit(name, len(spans), matched_terms, merge_spans(spans)))
    hits.sort(key=lambda hit: (-hit.density, hit.name))
    return hits


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `topic_block`."""
    topics = _topics_of(rule)
    if not topics:
        raise ValidationError(
            f"A regra topic_block '{rule.id}' exige 'topics' com ao menos um topico.",
            details={"rule_id": rule.id},
        )
    config_int(rule.config, "threshold", rule_id=rule.id, default=DEFAULT_THRESHOLD, minimum=1)


class TopicBlockEvaluator:
    """`topic_block`: dispara quando a densidade de um topico atinge o limiar."""

    kind: GuardrailRuleKind = GuardrailRuleKind.TOPIC_BLOCK

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Pontua todos os topicos e devolve o achado do mais denso."""
        validate_rule(rule)
        if not content:
            return None
        topics = _topics_of(rule)
        threshold = (
            config_int(rule.config, "threshold", rule_id=rule.id, minimum=1) or DEFAULT_THRESHOLD
        )
        whole_word = config_bool(rule.config, "whole_word", default=True)

        hits = score_topics(content, topics, threshold=threshold, whole_word=whole_word)
        if not hits:
            return None

        top = hits[0]
        first = top.spans[0]
        if rule.action in CONTENT_ACTIONS:
            spans = [span for hit in hits for span in hit.spans]
            evidence = redact_spans(content, spans, redaction_token(context))
        else:
            evidence = snippet(f"{top.name}: {', '.join(top.terms[:5])}")
        message = (
            f"Topico bloqueado '{top.name}' com densidade {top.density} "
            f"(limiar {threshold}); termos: {', '.join(top.terms[:5])}."
        )
        return build_finding(rule, message, evidence=evidence, span=first)
