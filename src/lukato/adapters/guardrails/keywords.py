"""Avaliador `keyword_block`: bloqueio por lista de termos, sem acento e sem caixa.

A comparacao acontece sobre uma versao "dobrada" do texto (minusculas, sem
diacriticos) acompanhada de um mapa de deslocamentos, o que permite devolver os
intervalos exatos no texto **original** — indispensavel para redigir sem destruir a
formatacao. O casamento tolera espacos multiplos e quebras de linha entre as
palavras de um termo composto.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    config_bool,
    config_str_list,
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
    "KeywordBlockEvaluator",
    "fold_with_offsets",
    "keyword_spans",
    "validate_rule",
]

_PATTERN_CACHE_SIZE: Final[int] = 512
_MAX_KEYWORDS: Final[int] = 2000
"""Teto defensivo: politicas com listas absurdas viram erro de validacao."""


def fold_with_offsets(text: str) -> tuple[str, list[int]]:
    """Devolve o texto sem diacriticos e em minusculas + o mapa para o texto original.

    `offsets[i]` e o indice, no texto original, do caractere que gerou o caractere
    `i` do texto dobrado. Caracteres puramente combinantes desaparecem do resultado.
    """
    folded: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        decomposed = unicodedata.normalize("NFKD", char)
        stripped = "".join(part for part in decomposed if not unicodedata.combining(part))
        lowered = stripped.casefold()
        if not lowered:
            continue
        folded.append(lowered)
        offsets.extend([index] * len(lowered))
    return "".join(folded), offsets


@lru_cache(maxsize=_PATTERN_CACHE_SIZE)
def _term_pattern(term: str, whole_word: bool) -> re.Pattern[str] | None:
    """Compila o termo tolerando espacos variaveis; `None` quando o termo e vazio."""
    parts = [re.escape(part) for part in term.split()]
    if not parts:
        return None
    core = r"\s+".join(parts)
    if whole_word:
        core = rf"(?<!\w){core}(?!\w)"
    return re.compile(core)


def keyword_spans(
    content: str, keyword: str, *, normalize: bool = True, whole_word: bool = True
) -> list[tuple[int, int]]:
    """Localiza todas as ocorrencias de `keyword` e devolve os intervalos no original.

    Com `normalize=True` a busca ignora acentos e caixa; com `False` a comparacao e
    literal (util para termos sensiveis a caixa, como siglas).
    """
    if not content or not keyword.strip():
        return []
    if normalize:
        haystack, offsets = fold_with_offsets(content)
        needle = fold_with_offsets(keyword)[0]
    else:
        haystack, offsets = content, list(range(len(content)))
        needle = keyword
    pattern = _term_pattern(needle, whole_word)
    if pattern is None or not haystack:
        return []

    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(haystack):
        start, end = match.span()
        if end <= start or start >= len(offsets):
            continue
        original_start = offsets[start]
        original_end = offsets[min(end, len(offsets)) - 1] + 1
        spans.append((original_start, original_end))
    return spans


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `keyword_block`."""
    keywords = config_str_list(rule.config, "keywords", rule_id=rule.id)
    if not keywords:
        raise ValidationError(
            f"A regra keyword_block '{rule.id}' exige 'keywords' com ao menos um termo.",
            details={"rule_id": rule.id},
        )
    if len(keywords) > _MAX_KEYWORDS:
        raise ValidationError(
            f"A regra keyword_block '{rule.id}' tem {len(keywords)} termos; o limite e "
            f"{_MAX_KEYWORDS}.",
            details={"rule_id": rule.id, "count": len(keywords), "max": _MAX_KEYWORDS},
        )
    if all(not keyword.strip() for keyword in keywords):
        raise ValidationError(
            f"A regra keyword_block '{rule.id}' so tem termos em branco.",
            details={"rule_id": rule.id},
        )


class KeywordBlockEvaluator:
    """`keyword_block`: dispara quando algum termo proibido aparece no conteudo."""

    kind: GuardrailRuleKind = GuardrailRuleKind.KEYWORD_BLOCK

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Procura os termos da regra e devolve o achado com o primeiro intervalo."""
        validate_rule(rule)
        if not content:
            return None
        keywords = config_str_list(rule.config, "keywords", rule_id=rule.id)
        normalize = config_bool(rule.config, "normalize", default=True)
        whole_word = config_bool(rule.config, "whole_word", default=True)

        spans: list[tuple[int, int]] = []
        matched: list[str] = []
        for keyword in keywords:
            found = keyword_spans(content, keyword, normalize=normalize, whole_word=whole_word)
            if found:
                matched.append(keyword)
                spans.extend(found)
        if not spans:
            return None

        merged = merge_spans(spans)
        first = merged[0]
        if rule.action in CONTENT_ACTIONS:
            evidence = redact_spans(content, merged, redaction_token(context))
        else:
            evidence = snippet(content[first[0] : first[1]])
        message = f"Conteudo contem {len(matched)} termo(s) bloqueado(s): {', '.join(matched[:5])}."
        return build_finding(rule, message, evidence=evidence, span=first)
