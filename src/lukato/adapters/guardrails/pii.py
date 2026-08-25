"""Avaliador `pii_redact`: deteccao e redacao de dados pessoais brasileiros.

CPF, CNPJ e cartao de credito passam pelo **digito verificador** (modulo 11 para os
dois primeiros, Luhn para o cartao) antes de virarem violacao — sem isso qualquer
sequencia numerica viraria falso positivo. Telefone, CEP, IPv4 e RG usam formato
mais validacao estrutural; e-mail usa o formato usual.

A redacao **substitui o valor inteiro** pelo marcador vindo de
`context["redaction_token"]` (fallback `"[REDIGIDO]"`): mascaras parciais como
`123.***.***-09` ainda vazam entropia suficiente para reidentificacao.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
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
    "PII_TYPES",
    "PiiMatch",
    "PiiRedactEvaluator",
    "detect_pii",
    "is_valid_cnpj",
    "is_valid_cpf",
    "is_valid_credit_card",
    "validate_rule",
]

PII_TYPES: Final[tuple[str, ...]] = (
    "cpf",
    "cnpj",
    "email",
    "phone",
    "credit_card",
    "cep",
    "ip",
    "rg",
)
"""Tipos de dado pessoal suportados pela regra `pii_redact`."""

_LABELS: Final[dict[str, str]] = {
    "cpf": "CPF",
    "cnpj": "CNPJ",
    "email": "e-mail",
    "phone": "telefone",
    "credit_card": "cartao de credito",
    "cep": "CEP",
    "ip": "endereco IP",
    "rg": "RG",
}

_ONLY_DIGITS = re.compile(r"\D+")
_CNPJ_WEIGHTS_1: Final[tuple[int, ...]] = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_CNPJ_WEIGHTS_2: Final[tuple[int, ...]] = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


# --------------------------------------------------------------------------- #
# Digitos verificadores
# --------------------------------------------------------------------------- #


def _digits(value: str) -> str:
    """Mantem apenas os algarismos de um texto."""
    return _ONLY_DIGITS.sub("", value)


def _check_digit_mod11(numbers: Sequence[int], weights: Sequence[int]) -> int:
    """Digito verificador modulo 11 usado por CPF e CNPJ."""
    total = sum(number * weight for number, weight in zip(numbers, weights, strict=True))
    remainder = total % 11
    return 0 if remainder < 2 else 11 - remainder


def is_valid_cpf(value: str) -> bool:
    """Valida um CPF pelos dois digitos verificadores (recusa digitos repetidos)."""
    digits = _digits(value)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    numbers = [int(char) for char in digits]
    first = _check_digit_mod11(numbers[:9], range(10, 1, -1))
    second = _check_digit_mod11(numbers[:10], range(11, 1, -1))
    return numbers[9] == first and numbers[10] == second


def is_valid_cnpj(value: str) -> bool:
    """Valida um CNPJ pelos dois digitos verificadores (recusa digitos repetidos)."""
    digits = _digits(value)
    if len(digits) != 14 or len(set(digits)) == 1:
        return False
    numbers = [int(char) for char in digits]
    first = _check_digit_mod11(numbers[:12], _CNPJ_WEIGHTS_1)
    second = _check_digit_mod11(numbers[:13], _CNPJ_WEIGHTS_2)
    return numbers[12] == first and numbers[13] == second


def is_valid_credit_card(value: str) -> bool:
    """Valida um numero de cartao (13 a 19 digitos) pelo algoritmo de Luhn."""
    digits = _digits(value)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _is_valid_ipv4(value: str) -> bool:
    """Valida um IPv4 conferindo os quatro octetos em 0..255."""
    parts = value.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and len(part) <= 3 and int(part) <= 255 for part in parts)


def _is_valid_phone_br(value: str) -> bool:
    """Valida a estrutura de um telefone brasileiro, com ou sem DDI +55."""
    digits = _digits(value)
    if digits.startswith("55") and len(digits) in {12, 13}:
        digits = digits[2:]
    if len(digits) not in {10, 11}:
        return False
    if int(digits[:2]) < 11:  # DDD valido comeca em 11
        return False
    if len(digits) == 11:
        return digits[2] == "9"  # celular no formato atual
    return digits[2] not in {"0", "1"}


# --------------------------------------------------------------------------- #
# Detectores
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PiiMatch:
    """Ocorrencia de dado pessoal localizada no texto original."""

    type: str
    value: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Detector:
    """Par padrao + validador semantico de um tipo de dado pessoal."""

    type: str
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None


# A ordem e a prioridade de resolucao de sobreposicao: o detector mais especifico
# reivindica o trecho antes dos mais genericos (telefone e o ultimo de proposito).
_DETECTORS: Final[tuple[_Detector, ...]] = (
    _Detector("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")),
    # O `(?!\.\d)` final evita casar um prefixo de sequencia mais longa (1.2.3.4.5)
    # sem recusar o IP que fecha uma frase ("... IP 192.168.0.15.").
    _Detector(
        "ip",
        re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\d)(?!\.\d)"),
        _is_valid_ipv4,
    ),
    _Detector(
        "cnpj",
        re.compile(r"(?<!\d)(?:\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})(?!\d)"),
        is_valid_cnpj,
    ),
    _Detector(
        "cpf",
        re.compile(r"(?<!\d)(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})(?!\d)"),
        is_valid_cpf,
    ),
    _Detector(
        "credit_card",
        re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?!\d)"),
        is_valid_credit_card,
    ),
    _Detector("rg", re.compile(r"(?<!\d)\d{1,2}\.\d{3}\.\d{3}-[0-9xX](?!\w)")),
    _Detector("cep", re.compile(r"(?<!\d)\d{5}-\d{3}(?!\d)")),
    _Detector(
        "phone",
        re.compile(r"(?<![\d+])(?:\+?55[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)"),
        _is_valid_phone_br,
    ),
)

_DETECTORS_BY_TYPE: Final[dict[str, _Detector]] = {
    detector.type: detector for detector in _DETECTORS
}


def detect_pii(content: str, types: Sequence[str] = PII_TYPES) -> list[PiiMatch]:
    """Localiza os dados pessoais dos tipos pedidos, sem sobreposicao de trechos.

    O resultado vem ordenado pela posicao no texto; trechos ja reivindicados por um
    detector mais especifico nao sao reofertados aos seguintes.
    """
    if not content:
        return []
    wanted = [name for name in types if name in _DETECTORS_BY_TYPE]
    claimed: list[tuple[int, int]] = []
    matches: list[PiiMatch] = []
    for detector in _DETECTORS:
        if detector.type not in wanted:
            continue
        for match in detector.pattern.finditer(content):
            start, end = match.span()
            value = match.group()
            if detector.validator is not None and not detector.validator(value):
                continue
            if any(start < other_end and other_start < end for other_start, other_end in claimed):
                continue
            claimed.append((start, end))
            matches.append(PiiMatch(type=detector.type, value=value, start=start, end=end))
    matches.sort(key=lambda item: (item.start, item.end))
    return matches


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `pii_redact`."""
    types = config_str_list(rule.config, "types", rule_id=rule.id, default=PII_TYPES)
    unknown = sorted({name for name in types if name not in _DETECTORS_BY_TYPE})
    if unknown:
        raise ValidationError(
            f"A regra pii_redact '{rule.id}' pede tipos desconhecidos: {', '.join(unknown)}.",
            details={"rule_id": rule.id, "unknown": unknown, "supported": list(PII_TYPES)},
        )
    if not types:
        raise ValidationError(
            f"A regra pii_redact '{rule.id}' precisa de ao menos um tipo em 'types'.",
            details={"rule_id": rule.id, "supported": list(PII_TYPES)},
        )


class PiiRedactEvaluator:
    """`pii_redact`: detecta dados pessoais e devolve o texto ja redigido."""

    kind: GuardrailRuleKind = GuardrailRuleKind.PII_REDACT

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Detecta os tipos configurados e monta o achado com o texto redigido."""
        validate_rule(rule)
        types = config_str_list(rule.config, "types", rule_id=rule.id, default=PII_TYPES)
        matches = detect_pii(content, types)
        if not matches:
            return None

        counts: dict[str, int] = {}
        for match in matches:
            counts[match.type] = counts.get(match.type, 0) + 1
        summary = ", ".join(
            f"{_LABELS.get(name, name)} ({counts[name]})" for name in sorted(counts)
        )
        spans = merge_spans((match.start, match.end) for match in matches)
        first = spans[0]

        if rule.action in CONTENT_ACTIONS:
            evidence = redact_spans(content, spans, redaction_token(context))
        else:
            evidence = snippet(summary)
        message = f"Dado pessoal detectado e tratado: {summary}."
        return build_finding(rule, message, evidence=evidence, span=first)
