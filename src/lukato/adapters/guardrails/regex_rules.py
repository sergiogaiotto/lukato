"""Avaliadores por expressao regular e fundacao comum do pacote de guardrails.

Alem de implementar as regras `regex_block` e `regex_require` (SPEC-0003, secao 3),
este modulo concentra os utilitarios compartilhados por todos os avaliadores do
pacote — compilacao de padroes com cache e limite de tamanho, construcao de
`GuardrailFinding` a partir da regra, redacao por intervalos e leitura tipada da
`config` — de modo que o pacote nao precise de modulos fora do contrato da SPEC.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Final

from lukato.domain.errors import ValidationError
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailFinding,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailSeverity,
)
from lukato.domain.types import Json

__all__ = [
    "CONTENT_ACTIONS",
    "DEFAULT_REDACTION_TOKEN",
    "EVIDENCE_LIMIT",
    "MAX_PATTERN_LENGTH",
    "RegexBlockEvaluator",
    "RegexRequireEvaluator",
    "build_finding",
    "clear_pattern_cache",
    "compile_pattern",
    "config_bool",
    "config_float",
    "config_int",
    "config_str",
    "config_str_list",
    "merge_spans",
    "parse_flags",
    "redact_spans",
    "redaction_token",
    "snippet",
    "validate_pattern",
    "validate_rule",
]

MAX_PATTERN_LENGTH: Final[int] = 500
"""Tamanho maximo de um padrao regex aceito numa politica (guarda contra ReDoS)."""

DEFAULT_REDACTION_TOKEN: Final[str] = "[REDIGIDO]"
"""Marcador usado quando o contexto nao informa `redaction_token`."""

EVIDENCE_LIMIT: Final[int] = 240
"""Tamanho maximo de um trecho copiado para `finding.evidence` descritivo."""

CONTENT_ACTIONS: Final[frozenset[GuardrailAction]] = frozenset(
    {GuardrailAction.REDACT, GuardrailAction.TRANSFORM}
)
"""Acoes em que o motor substitui o conteudo pelo texto contido em `evidence`."""

_PATTERN_CACHE_SIZE: Final[int] = 512
_FLAG_BITS: Final[dict[str, int]] = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
_FLAG_SEPARATORS: Final[frozenset[str]] = frozenset({" ", ",", "|", "-", "+"})


# --------------------------------------------------------------------------- #
# Compilacao de padroes
# --------------------------------------------------------------------------- #


def parse_flags(flags: str | Iterable[str] | None) -> int:
    """Converte flags textuais (`"i"`, `"im"`, `["i","s"]`) em bits do modulo `re`.

    Suporta apenas `i` (ignore case), `m` (multiline) e `s` (dotall); qualquer
    outra letra e recusada com `ValidationError` para nao mascarar erro de config.
    """
    if flags is None:
        return 0
    letters = flags if isinstance(flags, str) else "".join(str(item) for item in flags)
    bits = 0
    for letter in letters:
        if letter in _FLAG_SEPARATORS:
            continue
        bit = _FLAG_BITS.get(letter.lower())
        if bit is None:
            raise ValidationError(
                f"Flag de regex nao suportada: '{letter}'. Use apenas 'i', 'm' ou 's'.",
                details={"flags": letters, "supported": sorted(_FLAG_BITS)},
            )
        bits |= bit
    return bits


@lru_cache(maxsize=_PATTERN_CACHE_SIZE)
def _compile_cached(pattern: str, flag_bits: int) -> re.Pattern[str]:
    """Compilacao memorizada — o mesmo padrao nunca e recompilado."""
    return re.compile(pattern, flag_bits)


def compile_pattern(pattern: str, flags: str | Iterable[str] | None = "") -> re.Pattern[str]:
    """Compila um padrao de politica com cache, tamanho limitado e erro de dominio."""
    if not isinstance(pattern, str) or not pattern:
        raise ValidationError(
            "Padrao de regex vazio ou nao textual na politica de guardrail.",
            details={"pattern": repr(pattern)},
        )
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValidationError(
            f"Padrao de regex com {len(pattern)} caracteres excede o limite de "
            f"{MAX_PATTERN_LENGTH}.",
            details={"length": len(pattern), "max_length": MAX_PATTERN_LENGTH},
        )
    bits = parse_flags(flags)
    try:
        return _compile_cached(pattern, bits)
    except re.error as exc:
        raise ValidationError(
            f"Padrao de regex invalido: {exc}",
            details={"pattern": pattern, "position": exc.pos},
        ) from exc


def validate_pattern(pattern: str, flags: str | Iterable[str] | None = "") -> None:
    """Valida um padrao (tamanho, flags e sintaxe) sem devolver o objeto compilado."""
    compile_pattern(pattern, flags)


def clear_pattern_cache() -> None:
    """Esvazia o cache de padroes compilados (util em testes longos)."""
    _compile_cached.cache_clear()


# --------------------------------------------------------------------------- #
# Leitura tipada da config da regra
# --------------------------------------------------------------------------- #


def config_str_list(
    config: Json, key: str, *, rule_id: str = "", default: Sequence[str] = ()
) -> list[str]:
    """Le uma lista de textos da config, aceitando tambem um unico texto."""
    raw = config.get(key)
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        items: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValidationError(
                    f"'{key}' da regra '{rule_id}' deve conter apenas textos.",
                    details={"key": key, "rule_id": rule_id, "invalid": repr(item)},
                )
            if item:
                items.append(item)
        return items
    raise ValidationError(
        f"'{key}' da regra '{rule_id}' deve ser uma lista de textos.",
        details={"key": key, "rule_id": rule_id, "type": type(raw).__name__},
    )


def config_str(config: Json, key: str, *, rule_id: str = "", default: str = "") -> str:
    """Le um texto da config da regra."""
    raw = config.get(key, default)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser texto.",
            details={"key": key, "rule_id": rule_id, "type": type(raw).__name__},
        )
    return raw


def config_int(
    config: Json,
    key: str,
    *,
    rule_id: str = "",
    default: int | None = None,
    minimum: int | None = None,
) -> int | None:
    """Le um inteiro opcional da config da regra, com piso opcional."""
    raw = config.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser um numero inteiro.",
            details={"key": key, "rule_id": rule_id, "type": type(raw).__name__},
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser um numero inteiro.",
            details={"key": key, "rule_id": rule_id, "value": repr(raw)},
        ) from exc
    if minimum is not None and value < minimum:
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser >= {minimum} (recebido {value}).",
            details={"key": key, "rule_id": rule_id, "value": value, "minimum": minimum},
        )
    return value


def config_float(
    config: Json,
    key: str,
    *,
    rule_id: str = "",
    default: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Le um numero real da config da regra, com limites opcionais."""
    raw = config.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser um numero.",
            details={"key": key, "rule_id": rule_id, "type": type(raw).__name__},
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser um numero.",
            details={"key": key, "rule_id": rule_id, "value": repr(raw)},
        ) from exc
    if minimum is not None and value < minimum:
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser >= {minimum} (recebido {value}).",
            details={"key": key, "rule_id": rule_id, "value": value, "minimum": minimum},
        )
    if maximum is not None and value > maximum:
        raise ValidationError(
            f"'{key}' da regra '{rule_id}' deve ser <= {maximum} (recebido {value}).",
            details={"key": key, "rule_id": rule_id, "value": value, "maximum": maximum},
        )
    return value


def config_bool(config: Json, key: str, *, default: bool = False) -> bool:
    """Le um booleano da config da regra, aceitando as grafias textuais usuais."""
    raw = config.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}
    return bool(raw)


# --------------------------------------------------------------------------- #
# Achados, redacao e evidencias
# --------------------------------------------------------------------------- #


def build_finding(
    rule: GuardrailRule,
    message: str,
    *,
    evidence: str = "",
    span: tuple[int, int] | None = None,
    severity: GuardrailSeverity | None = None,
    action: GuardrailAction | None = None,
    use_rule_message: bool = True,
) -> GuardrailFinding:
    """Monta o achado de uma regra que disparou.

    A mensagem da propria regra tem precedencia quando existe (`use_rule_message`),
    o que permite a cada politica falar com o usuario final na linguagem dela.
    """
    text = rule.message if (use_rule_message and rule.message) else message
    return GuardrailFinding(
        rule_id=rule.id,
        kind=rule.kind,
        action=action if action is not None else rule.action,
        severity=severity if severity is not None else rule.severity,
        message=text,
        evidence=evidence,
        span=span,
    )


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Ordena e funde intervalos sobrepostos ou adjacentes, descartando os vazios."""
    ordered = sorted((start, end) for start, end in spans if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def redact_spans(text: str, spans: Iterable[tuple[int, int]], token: str) -> str:
    """Substitui cada intervalo informado pelo marcador de redacao."""
    parts: list[str] = []
    cursor = 0
    for start, end in merge_spans(spans):
        start = max(start, cursor)
        if start >= end:
            continue
        parts.append(text[cursor:start])
        parts.append(token)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def redaction_token(context: Json) -> str:
    """Marcador de redacao vindo do contexto do motor, com fallback padrao."""
    raw = context.get("redaction_token") if context else None
    return raw if isinstance(raw, str) and raw else DEFAULT_REDACTION_TOKEN


def snippet(text: str, limit: int = EVIDENCE_LIMIT) -> str:
    """Recorta um trecho curto para `evidence` descritiva (nunca o texto inteiro)."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Avaliadores
# --------------------------------------------------------------------------- #


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `regex_block` ou `regex_require`."""
    patterns = config_str_list(rule.config, "patterns", rule_id=rule.id)
    if not patterns:
        raise ValidationError(
            f"A regra regex '{rule.id}' exige 'patterns' com ao menos um padrao.",
            details={"rule_id": rule.id, "kind": rule.kind.value},
        )
    flags = config_str(rule.config, "flags", rule_id=rule.id)
    for pattern in patterns:
        validate_pattern(pattern, flags)


class RegexBlockEvaluator:
    """`regex_block`: dispara quando **qualquer** padrao casa com o conteudo."""

    kind: GuardrailRuleKind = GuardrailRuleKind.REGEX_BLOCK

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Procura todos os padroes bloqueados e devolve o achado do primeiro casamento."""
        validate_rule(rule)
        if not content:
            return None
        patterns = config_str_list(rule.config, "patterns", rule_id=rule.id)
        flags = config_str(rule.config, "flags", rule_id=rule.id)

        spans: list[tuple[int, int]] = []
        matched: list[str] = []
        for pattern in patterns:
            regex = compile_pattern(pattern, flags)
            found = [
                match.span() for match in regex.finditer(content) if match.end() > match.start()
            ]
            if found:
                matched.append(pattern)
                spans.extend(found)
        if not spans:
            return None

        merged = merge_spans(spans)
        first = merged[0]
        if rule.action in CONTENT_ACTIONS:
            evidence = redact_spans(content, merged, redaction_token(context))
        else:
            evidence = snippet(content[first[0] : first[1]])
        message = (
            f"Conteudo casou com {len(matched)} padrao(oes) bloqueado(s) "
            f"em {len(merged)} trecho(s): {', '.join(matched[:3])}."
        )
        return build_finding(rule, message, evidence=evidence, span=first)


class RegexRequireEvaluator:
    """`regex_require`: dispara quando **algum** padrao obrigatorio nao casa."""

    kind: GuardrailRuleKind = GuardrailRuleKind.REGEX_REQUIRE

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Confere que todos os padroes exigidos aparecem no conteudo."""
        validate_rule(rule)
        patterns = config_str_list(rule.config, "patterns", rule_id=rule.id)
        flags = config_str(rule.config, "flags", rule_id=rule.id)

        missing = [
            pattern for pattern in patterns if not compile_pattern(pattern, flags).search(content)
        ]
        if not missing:
            return None

        message = f"Conteudo nao contem {len(missing)} padrao(oes) obrigatorio(s): " + ", ".join(
            missing[:3]
        )
        # Regra de ausencia nao tem como reescrever o texto: em acao de conteudo o
        # conteudo segue intacto e a informacao util fica na mensagem.
        evidence = content if rule.action in CONTENT_ACTIONS else snippet("; ".join(missing))
        return build_finding(rule, message, evidence=evidence)
