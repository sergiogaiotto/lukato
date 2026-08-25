"""Casos de uso das politicas de guardrail (SPEC-0003) — CRUD, testador e catalogo.

Uma politica so entra no banco depois de ser **provada coerente**: id de regra
unico, tipo conhecido, acao que o tipo sabe executar e config que o avaliador
correspondente vai aceitar (regex que compila, schema que e objeto, palavra-chave
que existe, limite que e inteiro positivo). Validar aqui, e nao no adaptador,
significa que o erro aparece no editor do console — e nao no meio de uma execucao
com o usuario esperando.

A validacao vive na camada de aplicacao porque a regra hexagonal proibe importar
`lukato.adapters`: o catalogo abaixo e a copia normativa da SPEC-0003 secao 3, e o
avaliador continua sendo a autoridade final em tempo de execucao.

`TestPolicy` alimenta o testador da UI (SPEC-0009, rota `/guardrails`): aplica uma
politica salva **ou** um rascunho ainda nao gravado sobre um texto e devolve o
`GuardrailVerdict` inteiro — conteudo final, achados, latencia e bloqueio.
`ListRuleKinds` devolve o descritor de cada tipo de regra para a UI montar o
editor sem hard-code.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, fields
from typing import Any, Final, TypeVar, cast

from pydantic import ValidationError as PydanticValidationError

from lukato.application.container import Container
from lukato.application.dto import (
    DEFAULT_PAGE_LIMIT,
    UNSET,
    Maybe,
    Page,
    PageRequest,
    is_set,
)
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import ConflictError, NotFoundError, ValidationError
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailPolicy,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailStage,
    GuardrailVerdict,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.types import Json, new_id, slugify, utcnow

__all__ = [
    "DEFAULT_JUDGE_THRESHOLD",
    "DEFAULT_TOPIC_THRESHOLD",
    "MAX_KEYWORDS",
    "MAX_PATTERN_LENGTH",
    "PII_TYPES",
    "REGEX_FLAGS",
    "RULE_KIND_DESCRIPTIONS",
    "RULE_SUPPORTED_ACTIONS",
    "SUPPORTED_LANGUAGES",
    "CreatePolicy",
    "DeletePolicy",
    "GetPolicy",
    "GetPolicyBySlug",
    "ListPolicies",
    "ListRuleKinds",
    "PolicyCreateInput",
    "PolicyFilter",
    "PolicyUpdateInput",
    "TestPolicy",
    "UpdatePolicy",
    "coerce_rule",
    "coerce_rules",
    "describe_rule_kind",
    "rule_config_schema",
    "rule_kind_catalog",
    "validate_rule",
    "validate_rules",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

MAX_PATTERN_LENGTH: Final[int] = 500
"""Teto de caracteres de um padrao de regex (SPEC-0003 secao 3: timeout logico)."""

MAX_KEYWORDS: Final[int] = 2000
"""Teto de termos de uma regra `keyword_block`, para manter a varredura barata."""

REGEX_FLAGS: Final[dict[str, int]] = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
"""Flags textuais aceitas em regras de regex e seus bits do modulo `re`."""

_FLAG_SEPARATORS: Final[frozenset[str]] = frozenset({" ", ",", "|", "-", "+"})
"""Separadores tolerados ao escrever varias flags (`"i, m"`, `"i|s"`)."""

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
"""Tipos de dado pessoal reconhecidos pela regra `pii_redact`."""

SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("en", "es", "pt")
"""Idiomas cobertos pela heuristica de `language_allow` (sem dependencia externa)."""

DEFAULT_TOPIC_THRESHOLD: Final[int] = 2
"""Densidade minima de termos que faz um topico disparar."""

DEFAULT_JUDGE_THRESHOLD: Final[float] = 0.5
"""Nota a partir da qual o juiz LLM considera o conteudo em violacao."""

_ALL_ACTIONS: Final[tuple[GuardrailAction, ...]] = (
    GuardrailAction.ALLOW,
    GuardrailAction.WARN,
    GuardrailAction.REDACT,
    GuardrailAction.TRANSFORM,
    GuardrailAction.BLOCK,
)
"""Acoes de uma regra capaz de reescrever o conteudo."""

_DETECTION_ACTIONS: Final[tuple[GuardrailAction, ...]] = (
    GuardrailAction.ALLOW,
    GuardrailAction.WARN,
    GuardrailAction.BLOCK,
)
"""Acoes de uma regra que so sabe apontar o problema, nunca conserta-lo."""

_TRUNCATION_ACTIONS: Final[tuple[GuardrailAction, ...]] = (
    GuardrailAction.ALLOW,
    GuardrailAction.WARN,
    GuardrailAction.TRANSFORM,
    GuardrailAction.BLOCK,
)
"""Acoes de `max_length`: `TRANSFORM` trunca, `BLOCK` recusa (SPEC-0003 secao 3)."""

RULE_SUPPORTED_ACTIONS: Final[dict[GuardrailRuleKind, tuple[GuardrailAction, ...]]] = {
    GuardrailRuleKind.REGEX_BLOCK: _ALL_ACTIONS,
    GuardrailRuleKind.REGEX_REQUIRE: _DETECTION_ACTIONS,
    GuardrailRuleKind.KEYWORD_BLOCK: _ALL_ACTIONS,
    GuardrailRuleKind.PII_REDACT: _ALL_ACTIONS,
    GuardrailRuleKind.SECRET_SCAN: _ALL_ACTIONS,
    GuardrailRuleKind.TOPIC_BLOCK: _ALL_ACTIONS,
    GuardrailRuleKind.MAX_LENGTH: _TRUNCATION_ACTIONS,
    GuardrailRuleKind.MIN_LENGTH: _DETECTION_ACTIONS,
    GuardrailRuleKind.JSON_SCHEMA: _DETECTION_ACTIONS,
    GuardrailRuleKind.LANGUAGE_ALLOW: _DETECTION_ACTIONS,
    GuardrailRuleKind.LLM_JUDGE: _DETECTION_ACTIONS,
}
"""Acoes coerentes com cada tipo: pedir `REDACT` a quem nao reescreve seria no-op."""

RULE_KIND_DESCRIPTIONS: Final[dict[GuardrailRuleKind, str]] = {
    GuardrailRuleKind.REGEX_BLOCK: (
        "Dispara quando qualquer expressao regular da lista casa com o conteudo."
    ),
    GuardrailRuleKind.REGEX_REQUIRE: (
        "Dispara quando alguma expressao regular obrigatoria NAO aparece no conteudo."
    ),
    GuardrailRuleKind.KEYWORD_BLOCK: (
        "Dispara em termos proibidos, comparando sem acento e sem diferenca de caixa."
    ),
    GuardrailRuleKind.PII_REDACT: (
        "Localiza dados pessoais (CPF, CNPJ e cartao com digito verificador conferido) "
        "e devolve o texto ja redigido."
    ),
    GuardrailRuleKind.SECRET_SCAN: (
        "Procura credenciais: chaves 'sk-', 'AKIA', 'ghp_', tokens JWT, 'Bearer' e "
        "chaves privadas PEM."
    ),
    GuardrailRuleKind.MAX_LENGTH: (
        "Impoe um teto de caracteres e/ou de tokens estimados; TRANSFORM trunca, BLOCK recusa."
    ),
    GuardrailRuleKind.MIN_LENGTH: "Exige um minimo de caracteres no conteudo.",
    GuardrailRuleKind.JSON_SCHEMA: (
        "Exige que o conteudo seja um documento JSON valido conforme o JSON Schema informado."
    ),
    GuardrailRuleKind.LANGUAGE_ALLOW: (
        "Detecta o idioma por heuristica de stopwords e barra o que estiver fora da lista."
    ),
    GuardrailRuleKind.TOPIC_BLOCK: (
        "Bloqueia por densidade de termos: o topico dispara ao atingir o limiar de ocorrencias."
    ),
    GuardrailRuleKind.LLM_JUDGE: (
        "Pede a um LLM um veredito estruturado sobre o criterio informado; falha do "
        "provedor vira apenas aviso. Use sempre como ultima regra da politica."
    ),
}
"""Texto curto por tipo de regra, exibido no editor de politicas do console."""


# ---------------------------------------------------------------------------
# Esquemas de config (o que a UI usa para montar o formulario de cada regra)
# ---------------------------------------------------------------------------
def _schema(properties: Json, *, required: Sequence[str] = (), **extra: Any) -> Json:
    """Monta o JSON Schema da config de um tipo de regra."""
    schema: Json = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    schema.update(extra)
    return schema


def _regex_schema() -> Json:
    """Config de `regex_block` e `regex_require`."""
    return _schema(
        {
            "patterns": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "maxLength": MAX_PATTERN_LENGTH},
                "title": "Padroes",
                "description": (
                    f"Expressoes regulares de ate {MAX_PATTERN_LENGTH} caracteres, "
                    "compiladas com cache."
                ),
            },
            "flags": {
                "type": "string",
                "default": "",
                "title": "Flags",
                "description": "Combine 'i' (ignora caixa), 'm' (multilinha) e 's' (ponto casa \\n).",
            },
        },
        required=("patterns",),
    )


def _length_schema(kind: GuardrailRuleKind) -> Json:
    """Config de `max_length` (teto) ou de `min_length` (piso)."""
    if kind is GuardrailRuleKind.MIN_LENGTH:
        return _schema(
            {
                "min_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "title": "Minimo de caracteres",
                    "description": "Conteudo menor que isso dispara a regra.",
                }
            },
            required=("min_chars",),
        )
    return _schema(
        {
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "title": "Maximo de caracteres",
                "description": "Teto absoluto de caracteres do conteudo.",
            },
            "max_tokens": {
                "type": "integer",
                "minimum": 1,
                "title": "Maximo de tokens",
                "description": "Teto de tokens estimados (cerca de 4 caracteres por token).",
            },
        },
        anyOf=[{"required": ["max_chars"]}, {"required": ["max_tokens"]}],
    )


_CONFIG_SCHEMA_FACTORIES: Final[dict[GuardrailRuleKind, Callable[[], Json]]] = {
    GuardrailRuleKind.REGEX_BLOCK: _regex_schema,
    GuardrailRuleKind.REGEX_REQUIRE: _regex_schema,
    GuardrailRuleKind.MAX_LENGTH: lambda: _length_schema(GuardrailRuleKind.MAX_LENGTH),
    GuardrailRuleKind.MIN_LENGTH: lambda: _length_schema(GuardrailRuleKind.MIN_LENGTH),
    GuardrailRuleKind.KEYWORD_BLOCK: lambda: _schema(
        {
            "keywords": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_KEYWORDS,
                "items": {"type": "string"},
                "title": "Termos",
                "description": "Termos proibidos; a comparacao ignora acento e caixa.",
            },
            "normalize": {"type": "boolean", "default": True, "title": "Normalizar acentos"},
            "whole_word": {"type": "boolean", "default": True, "title": "Somente palavra inteira"},
        },
        required=("keywords",),
    ),
    GuardrailRuleKind.PII_REDACT: lambda: _schema(
        {
            "types": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": list(PII_TYPES)},
                "default": list(PII_TYPES),
                "title": "Tipos de dado pessoal",
                "description": "CPF, CNPJ e cartao passam por conferencia de digito verificador.",
            }
        }
    ),
    GuardrailRuleKind.SECRET_SCAN: lambda: _schema(
        {
            "extra_patterns": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_PATTERN_LENGTH},
                "default": [],
                "title": "Padroes adicionais",
                "description": "Formatos proprios de credencial, alem do catalogo embutido.",
            },
            "flags": {"type": "string", "default": "", "title": "Flags"},
        }
    ),
    GuardrailRuleKind.JSON_SCHEMA: lambda: _schema(
        {
            "schema": {
                "type": "object",
                "title": "JSON Schema",
                "description": "Contrato que a saida precisa satisfazer.",
            },
            "coerce": {
                "type": "boolean",
                "default": False,
                "title": "Extrair JSON do texto",
                "description": "Recorta o primeiro objeto/array antes de validar.",
            },
        },
        required=("schema",),
    ),
    GuardrailRuleKind.LANGUAGE_ALLOW: lambda: _schema(
        {
            "languages": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": list(SUPPORTED_LANGUAGES)},
                "title": "Idiomas permitidos",
            },
            "min_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": 0.5,
                "title": "Confianca minima",
                "description": "Abaixo disso a deteccao e considerada inconclusiva.",
            },
        },
        required=("languages",),
    ),
    GuardrailRuleKind.TOPIC_BLOCK: lambda: _schema(
        {
            "topics": {
                "type": "array",
                "minItems": 1,
                "title": "Topicos",
                "items": {
                    "type": "object",
                    "required": ["name", "terms"],
                    "properties": {
                        "name": {"type": "string", "title": "Nome"},
                        "terms": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "title": "Termos",
                        },
                        "threshold": {
                            "type": "integer",
                            "minimum": 1,
                            "title": "Limiar proprio",
                        },
                    },
                },
            },
            "threshold": {
                "type": "integer",
                "minimum": 1,
                "default": DEFAULT_TOPIC_THRESHOLD,
                "title": "Limiar padrao",
                "description": "Ocorrencias necessarias para o topico disparar.",
            },
            "whole_word": {"type": "boolean", "default": True, "title": "Somente palavra inteira"},
        },
        required=("topics",),
    ),
    GuardrailRuleKind.LLM_JUDGE: lambda: _schema(
        {
            "criteria": {
                "type": "string",
                "minLength": 1,
                "title": "Criterio",
                "description": "O que o juiz deve procurar, em linguagem natural.",
            },
            "threshold": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": DEFAULT_JUDGE_THRESHOLD,
                "title": "Nota de corte",
            },
            "model": {
                "type": ["string", "null"],
                "default": None,
                "title": "Modelo",
                "description": "Vazio usa o modelo padrao da instalacao.",
            },
        },
        required=("criteria",),
    ),
}
"""Fabrica do JSON Schema de config de cada tipo (uma copia nova por chamada)."""


def rule_config_schema(kind: GuardrailRuleKind) -> Json:
    """JSON Schema da config de um tipo de regra, pronto para o editor da UI."""
    factory = _CONFIG_SCHEMA_FACTORIES.get(kind)
    if factory is None:  # pragma: no cover - catalogo cobre os onze tipos da SPEC
        return _schema({})
    return factory()


def describe_rule_kind(kind: GuardrailRuleKind) -> Json:
    """Descritor completo de um tipo de regra para a UI montar o editor."""
    return {
        "kind": kind.value,
        "descricao": RULE_KIND_DESCRIPTIONS.get(kind, ""),
        "config_schema": rule_config_schema(kind),
        "acoes_suportadas": [
            action.value for action in RULE_SUPPORTED_ACTIONS.get(kind, _DETECTION_ACTIONS)
        ],
    }


def rule_kind_catalog() -> list[Json]:
    """Catalogo dos onze tipos de regra da SPEC-0003, na ordem do enum."""
    return [describe_rule_kind(kind) for kind in GuardrailRuleKind]


# ---------------------------------------------------------------------------
# Validacao de regras
# ---------------------------------------------------------------------------
def _invalid(rule: GuardrailRule, problema: str, **extra: Any) -> ValidationError:
    """Monta o erro padrao de regra invalida, com `rule_id` e `problema` nos detalhes."""
    return ValidationError(
        f"Regra '{rule.id}' ({rule.kind.value}): {problema}",
        details={"rule_id": rule.id, "problema": problema, "kind": rule.kind.value, **extra},
    )


def _text(rule: GuardrailRule, key: str, *, default: str = "") -> str:
    """Le um texto da config da regra."""
    raw = rule.config.get(key, default)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise _invalid(rule, f"'{key}' deve ser texto", recebido=type(raw).__name__)
    return raw


def _text_list(rule: GuardrailRule, key: str, *, default: Sequence[str] = ()) -> list[str]:
    """Le uma lista de textos da config, aceitando tambem um unico texto."""
    raw = rule.config.get(key)
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        return [raw] if raw else []
    if not isinstance(raw, (list, tuple)):
        raise _invalid(rule, f"'{key}' deve ser uma lista de textos", recebido=type(raw).__name__)
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise _invalid(rule, f"'{key}' so aceita textos", recebido=repr(item))
        if item:
            values.append(item)
    return values


def _integer(rule: GuardrailRule, key: str, *, minimum: int = 1) -> int | None:
    """Le um inteiro opcional da config da regra, com piso obrigatorio."""
    raw = rule.config.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise _invalid(rule, f"'{key}' deve ser um numero inteiro", recebido=type(raw).__name__)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid(rule, f"'{key}' deve ser um numero inteiro", recebido=repr(raw)) from exc
    if value < minimum:
        raise _invalid(rule, f"'{key}' deve ser >= {minimum}", recebido=value)
    return value


def _number(rule: GuardrailRule, key: str, *, minimum: float, maximum: float) -> float | None:
    """Le um numero real opcional da config, dentro da faixa exigida pelo tipo."""
    raw = rule.config.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise _invalid(rule, f"'{key}' deve ser um numero", recebido=type(raw).__name__)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid(rule, f"'{key}' deve ser um numero", recebido=repr(raw)) from exc
    if not minimum <= value <= maximum:
        raise _invalid(rule, f"'{key}' deve estar entre {minimum} e {maximum}", recebido=value)
    return value


def _flag_bits(rule: GuardrailRule, flags: str) -> int:
    """Converte as flags textuais em bits do modulo `re`, recusando letras estranhas."""
    bits = 0
    for letter in flags:
        if letter in _FLAG_SEPARATORS:
            continue
        bit = REGEX_FLAGS.get(letter.lower())
        if bit is None:
            raise _invalid(
                rule,
                f"flag de regex nao suportada: '{letter}'",
                suportadas=sorted(REGEX_FLAGS),
            )
        bits |= bit
    return bits


def _check_patterns(rule: GuardrailRule, patterns: Sequence[str], bits: int) -> None:
    """Exige que cada padrao caiba no limite de tamanho e compile."""
    for pattern in patterns:
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise _invalid(
                rule,
                f"padrao com {len(pattern)} caracteres excede o limite de {MAX_PATTERN_LENGTH}",
                pattern=pattern[:80],
            )
        try:
            re.compile(pattern, bits)
        except re.error as exc:
            raise _invalid(rule, f"padrao de regex invalido: {exc}", pattern=pattern) from exc


def _validate_regex(rule: GuardrailRule) -> None:
    """`regex_block` / `regex_require`: padroes obrigatorios que compilam."""
    patterns = _text_list(rule, "patterns")
    if not patterns:
        raise _invalid(rule, "exige 'patterns' com ao menos um padrao")
    _check_patterns(rule, patterns, _flag_bits(rule, _text(rule, "flags")))


def _validate_keywords(rule: GuardrailRule) -> None:
    """`keyword_block`: lista de termos nao vazia e dentro do teto."""
    keywords = _text_list(rule, "keywords")
    if not keywords or all(not keyword.strip() for keyword in keywords):
        raise _invalid(rule, "exige 'keywords' com ao menos um termo")
    if len(keywords) > MAX_KEYWORDS:
        raise _invalid(
            rule,
            f"tem {len(keywords)} termos e o limite e {MAX_KEYWORDS}",
            recebido=len(keywords),
        )


def _validate_pii(rule: GuardrailRule) -> None:
    """`pii_redact`: tipos conhecidos (lista vazia significa todos)."""
    types = _text_list(rule, "types", default=PII_TYPES)
    if not types:
        raise _invalid(rule, "precisa de ao menos um tipo em 'types'", suportados=list(PII_TYPES))
    unknown = sorted({name for name in types if name not in PII_TYPES})
    if unknown:
        raise _invalid(
            rule,
            f"tipos de dado pessoal desconhecidos: {', '.join(unknown)}",
            suportados=list(PII_TYPES),
        )


def _validate_length(rule: GuardrailRule) -> None:
    """`max_length` / `min_length`: pelo menos um limite inteiro positivo."""
    if rule.kind is GuardrailRuleKind.MIN_LENGTH:
        if _integer(rule, "min_chars") is None:
            raise _invalid(rule, "exige 'min_chars' inteiro >= 1")
        return
    max_chars = _integer(rule, "max_chars")
    max_tokens = _integer(rule, "max_tokens")
    if max_chars is None and max_tokens is None:
        raise _invalid(rule, "exige 'max_chars' e/ou 'max_tokens' inteiros >= 1")


def _validate_json_schema(rule: GuardrailRule) -> None:
    """`json_schema`: o schema precisa ser um objeto JSON."""
    schema = rule.config.get("schema")
    if not isinstance(schema, Mapping):
        raise _invalid(
            rule,
            "exige 'schema' como objeto JSON Schema",
            recebido=type(schema).__name__,
        )


def _validate_language(rule: GuardrailRule) -> None:
    """`language_allow`: idiomas cobertos pela heuristica e confianca em 0..1."""
    languages = _text_list(rule, "languages")
    if not languages:
        raise _invalid(
            rule,
            "exige 'languages' com ao menos um idioma",
            suportados=list(SUPPORTED_LANGUAGES),
        )
    unknown = sorted({name for name in languages if name.lower() not in SUPPORTED_LANGUAGES})
    if unknown:
        raise _invalid(
            rule,
            f"idiomas nao suportados: {', '.join(unknown)}",
            suportados=list(SUPPORTED_LANGUAGES),
        )
    _number(rule, "min_confidence", minimum=0.0, maximum=1.0)


def _validate_topic(rule: GuardrailRule) -> None:
    """`topic_block`: topicos com nome e termos, e limiar inteiro positivo."""
    raw = rule.config.get("topics")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise _invalid(rule, "exige 'topics' com ao menos um topico")
    for index, item in enumerate(raw):
        position = index + 1
        if not isinstance(item, Mapping):
            raise _invalid(rule, f"o topico #{position} deve ser um objeto com 'name' e 'terms'")
        name = item.get("name")
        terms = item.get("terms")
        if not isinstance(name, str) or not name.strip():
            raise _invalid(rule, f"o topico #{position} precisa de 'name' textual")
        if not isinstance(terms, (list, tuple)) or not terms:
            raise _invalid(rule, f"o topico '{name}' precisa de 'terms' nao vazio")
        if any(not isinstance(term, str) or not term.strip() for term in terms):
            raise _invalid(rule, f"o topico '{name}' so aceita termos textuais")
    _integer(rule, "threshold")


def _validate_judge(rule: GuardrailRule) -> None:
    """`llm_judge`: criterio textual obrigatorio e nota de corte em 0..1."""
    if not _text(rule, "criteria").strip():
        raise _invalid(rule, "exige 'criteria' descrevendo o que julgar")
    _number(rule, "threshold", minimum=0.0, maximum=1.0)
    _text(rule, "model")


def _validate_secret(rule: GuardrailRule) -> None:
    """`secret_scan`: padroes extras opcionais que precisam compilar."""
    _check_patterns(
        rule, _text_list(rule, "extra_patterns"), _flag_bits(rule, _text(rule, "flags"))
    )


_RULE_VALIDATORS: Final[dict[GuardrailRuleKind, Callable[[GuardrailRule], None]]] = {
    GuardrailRuleKind.REGEX_BLOCK: _validate_regex,
    GuardrailRuleKind.REGEX_REQUIRE: _validate_regex,
    GuardrailRuleKind.KEYWORD_BLOCK: _validate_keywords,
    GuardrailRuleKind.PII_REDACT: _validate_pii,
    GuardrailRuleKind.MAX_LENGTH: _validate_length,
    GuardrailRuleKind.MIN_LENGTH: _validate_length,
    GuardrailRuleKind.JSON_SCHEMA: _validate_json_schema,
    GuardrailRuleKind.LANGUAGE_ALLOW: _validate_language,
    GuardrailRuleKind.TOPIC_BLOCK: _validate_topic,
    GuardrailRuleKind.LLM_JUDGE: _validate_judge,
    GuardrailRuleKind.SECRET_SCAN: _validate_secret,
}
"""Validador de config por tipo de regra (espelha a SPEC-0003 secao 3)."""


def validate_rule(rule: GuardrailRule) -> None:
    """Valida id, acao e config de uma regra; incoerencia levanta `ValidationError`."""
    if not rule.id.strip():
        raise _invalid(rule, "precisa de um 'id' nao vazio, unico dentro da politica")
    supported = RULE_SUPPORTED_ACTIONS.get(rule.kind)
    if supported is None:
        raise _invalid(
            rule,
            f"tipo de regra sem validador nesta instalacao: '{rule.kind.value}'",
            suportados=[kind.value for kind in _RULE_VALIDATORS],
        )
    if rule.action not in supported:
        raise _invalid(
            rule,
            f"a acao '{rule.action.value}' nao faz sentido para este tipo",
            acoes_suportadas=[action.value for action in supported],
        )
    _RULE_VALIDATORS[rule.kind](rule)


def validate_rules(rules: Sequence[GuardrailRule]) -> None:
    """Valida a lista inteira: ids unicos e config coerente em **todas** as regras.

    Regras desabilitadas tambem sao validadas: uma config quebrada guardada em
    silencio so apareceria no dia em que alguem ligasse a chave.
    """
    seen: set[str] = set()
    duplicated: list[str] = []
    for rule in rules:
        if rule.id in seen and rule.id not in duplicated:
            duplicated.append(rule.id)
        seen.add(rule.id)
    if duplicated:
        raise ValidationError(
            f"A politica repete os ids de regra: {', '.join(duplicated)}.",
            details={"rule_id": duplicated[0], "problema": "id de regra repetido na politica"},
        )
    for rule in rules:
        validate_rule(rule)


# ---------------------------------------------------------------------------
# Conversao de entrada
# ---------------------------------------------------------------------------
_RULE_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "kind", "action", "severity", "config", "message", "enabled", "order"}
)
"""Campos aceitos ao montar uma `GuardrailRule` a partir de JSON."""

_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "slug",
        "name",
        "description",
        "stage",
        "rules",
        "fail_open",
        "is_active",
        "created_at",
        "updated_at",
    }
)
"""Campos aceitos ao montar uma `GuardrailPolicy` avulsa a partir de JSON."""


def _coerce(data: Any, factory: type[_T], *, what: str) -> _T:
    """Aceita o DTO ja montado ou o objeto JSON cru vindo da borda HTTP/UI."""
    if isinstance(data, factory):
        return data
    if isinstance(data, Mapping):
        known = {item.name for item in fields(cast(Any, factory))}
        unknown = sorted(str(key) for key in data if str(key) not in known)
        if unknown:
            raise ValidationError(
                f"Campos desconhecidos em {what}: {', '.join(unknown)}.",
                details={"unknown": unknown, "supported": sorted(known)},
            )
        return factory(**{str(key): value for key, value in data.items()})
    raise ValidationError(
        f"{what} deve ser um objeto JSON ou um {factory.__name__}.",
        details={"received": type(data).__name__},
    )


def _as_stage(value: Any) -> GuardrailStage:
    """Normaliza o estagio informado como enum ou como texto."""
    if isinstance(value, GuardrailStage):
        return value
    try:
        return GuardrailStage(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"Estagio de guardrail invalido: '{value}'.",
            details={
                "field": "stage",
                "supported": [item.value for item in GuardrailStage],
            },
        ) from exc


def coerce_rule(raw: GuardrailRule | Mapping[str, Any]) -> GuardrailRule:
    """Converte JSON em `GuardrailRule`, recusando tipo desconhecido ou campo estranho."""
    if isinstance(raw, GuardrailRule):
        return raw
    if not isinstance(raw, Mapping):
        raise ValidationError(
            "Cada regra deve ser um objeto JSON.",
            details={"rule_id": "", "problema": f"regra do tipo {type(raw).__name__}"},
        )
    data = {str(key): value for key, value in raw.items()}
    rule_id = str(data.get("id", "")).strip()
    unknown = sorted(key for key in data if key not in _RULE_FIELDS)
    if unknown:
        raise ValidationError(
            f"Regra '{rule_id}': campos desconhecidos ({', '.join(unknown)}).",
            details={
                "rule_id": rule_id,
                "problema": f"campos desconhecidos: {', '.join(unknown)}",
                "suportados": sorted(_RULE_FIELDS),
            },
        )
    kind = data.get("kind")
    try:
        GuardrailRuleKind(kind if isinstance(kind, GuardrailRuleKind) else str(kind))
    except ValueError as exc:
        raise ValidationError(
            f"Regra '{rule_id}': tipo de regra desconhecido '{kind}'.",
            details={
                "rule_id": rule_id,
                "problema": f"tipo de regra desconhecido: '{kind}'",
                "suportados": [item.value for item in GuardrailRuleKind],
            },
        ) from exc
    try:
        return GuardrailRule(**data)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"Regra '{rule_id}': campos invalidos.",
            details={
                "rule_id": rule_id,
                "problema": "campos invalidos para uma regra de guardrail",
                "erros": exc.errors(include_url=False),
            },
        ) from exc


def coerce_rules(raw: Sequence[GuardrailRule | Mapping[str, Any]]) -> list[GuardrailRule]:
    """Converte e valida a lista de regras recebida da borda."""
    if isinstance(raw, (str, bytes, Mapping)):
        raise ValidationError(
            "'rules' deve ser uma lista de regras.",
            details={"received": type(raw).__name__},
        )
    rules = [coerce_rule(item) for item in raw]
    validate_rules(rules)
    return rules


def _inline_policy(
    raw: GuardrailPolicy | Mapping[str, Any], *, stage: GuardrailStage | None
) -> GuardrailPolicy:
    """Monta (sem persistir) a politica avulsa que o testador da UI enviou."""
    if isinstance(raw, GuardrailPolicy):
        validate_rules(raw.rules)
        return raw
    data = {str(key): value for key, value in raw.items()}
    unknown = sorted(key for key in data if key not in _POLICY_FIELDS)
    if unknown:
        raise ValidationError(
            f"Campos desconhecidos na politica em teste: {', '.join(unknown)}.",
            details={"unknown": unknown, "supported": sorted(_POLICY_FIELDS)},
        )
    return GuardrailPolicy(
        id=str(data.get("id") or new_id()),
        slug=slugify(str(data.get("slug") or "politica-em-teste")),
        name=str(data.get("name") or "Politica em teste"),
        description=str(data.get("description") or ""),
        stage=_as_stage(data.get("stage", stage or GuardrailStage.INPUT)),
        rules=coerce_rules(data.get("rules") or []),
        fail_open=bool(data.get("fail_open", False)),
        is_active=bool(data.get("is_active", True)),
    )


async def _find_policy(uow: UnitOfWork, reference: str) -> GuardrailPolicy | None:
    """Resolve a politica por slug e, em seguida, por identificador."""
    candidate = (reference or "").strip()
    if not candidate:
        return None
    found = await uow.guardrails.get_by_slug(candidate)
    if found is not None:
        return found
    return await uow.guardrails.get(candidate)


async def _require_policy(uow: UnitOfWork, reference: str) -> GuardrailPolicy:
    """Resolve a politica ou levanta :class:`NotFoundError`."""
    found = await _find_policy(uow, reference)
    if found is None:
        raise NotFoundError(
            f"Politica de guardrail '{reference}' nao encontrada.",
            details={"reference": reference},
        )
    return found


# ---------------------------------------------------------------------------
# DTOs de entrada
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PolicyCreateInput:
    """Dados de criacao de uma `GuardrailPolicy`."""

    slug: str
    name: str = ""
    description: str = ""
    stage: GuardrailStage | str = GuardrailStage.INPUT
    rules: Sequence[GuardrailRule | Mapping[str, Any]] = ()
    fail_open: bool = False
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class PolicyUpdateInput:
    """Atualizacao parcial de uma politica; campos ausentes ficam :data:`UNSET`."""

    name: Maybe[str] = UNSET
    description: Maybe[str] = UNSET
    stage: Maybe[GuardrailStage | str] = UNSET
    rules: Maybe[Sequence[GuardrailRule | Mapping[str, Any]]] = UNSET
    fail_open: Maybe[bool] = UNSET
    is_active: Maybe[bool] = UNSET

    def changes(self) -> Json:
        """Mapa `campo -> valor` ja normalizado, apenas com o que foi informado."""
        changed: Json = {}
        if is_set(self.name):
            changed["name"] = self.name
        if is_set(self.description):
            changed["description"] = self.description
        if is_set(self.stage):
            changed["stage"] = _as_stage(self.stage)
        if is_set(self.rules):
            changed["rules"] = coerce_rules(self.rules)
        if is_set(self.fail_open):
            changed["fail_open"] = self.fail_open
        if is_set(self.is_active):
            changed["is_active"] = self.is_active
        return changed


@dataclass(frozen=True, slots=True)
class PolicyFilter:
    """Filtros de listagem de politicas."""

    stage: GuardrailStage | None = None
    is_active: bool | None = None
    search: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao pelos limites normativos."""
        window = PageRequest(limit=self.limit, offset=self.offset)
        object.__setattr__(self, "limit", window.limit)
        object.__setattr__(self, "offset", window.offset)
        if self.stage is not None:
            object.__setattr__(self, "stage", _as_stage(self.stage))

    @property
    def page(self) -> PageRequest:
        """Janela de paginacao correspondente a este filtro."""
        return PageRequest(limit=self.limit, offset=self.offset)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _GuardrailUseCase:
    """Base dos casos de uso de guardrail: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
class CreatePolicy(_GuardrailUseCase):
    """Cria uma politica de guardrail com as regras ja validadas."""

    async def execute(
        self, data: PolicyCreateInput | Mapping[str, Any], principal: Principal
    ) -> GuardrailPolicy:
        """Grava a politica; slug duplicado levanta :class:`ConflictError`."""
        authorize(principal, Permission.GUARDRAIL_WRITE, "criar politicas de guardrail")
        payload = _coerce(data, PolicyCreateInput, what="a criacao de politica")
        slug = slugify(payload.slug or payload.name)
        policy = GuardrailPolicy(
            slug=slug,
            name=payload.name or slug,
            description=payload.description,
            stage=_as_stage(payload.stage),
            rules=coerce_rules(payload.rules),
            fail_open=payload.fail_open,
            is_active=payload.is_active,
        )
        async with self._container.uow_factory() as uow:
            if await uow.guardrails.get_by_slug(slug) is not None:
                raise ConflictError(
                    f"Ja existe uma politica de guardrail com o slug '{slug}'.",
                    details={"slug": slug},
                )
            created = await uow.guardrails.add(policy)
            await uow.commit()
        _logger.info(
            "guardrail_policy_created",
            slug=created.slug,
            stage=created.stage.value,
            rules=len(created.rules),
        )
        return created


class GetPolicy(_GuardrailUseCase):
    """Busca uma politica por identificador ou por slug."""

    async def execute(self, reference: str, principal: Principal) -> GuardrailPolicy:
        """Devolve a politica; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.GUARDRAIL_READ, "ler politicas de guardrail")
        async with self._container.uow_factory() as uow:
            return await _require_policy(uow, reference)


class GetPolicyBySlug(_GuardrailUseCase):
    """Busca uma politica pelo slug unico."""

    async def execute(self, slug: str, principal: Principal) -> GuardrailPolicy:
        """Devolve a politica do slug; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.GUARDRAIL_READ, "ler politicas de guardrail")
        async with self._container.uow_factory() as uow:
            found = await uow.guardrails.get_by_slug((slug or "").strip())
        if found is None:
            raise NotFoundError(
                f"Politica de guardrail '{slug}' nao encontrada.",
                details={"slug": slug},
            )
        return found


class ListPolicies(_GuardrailUseCase):
    """Lista politicas paginadas, com filtros de estagio, atividade e busca."""

    async def execute(
        self, filters: PolicyFilter | Mapping[str, Any], principal: Principal
    ) -> Page[GuardrailPolicy]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, Permission.GUARDRAIL_READ, "listar politicas de guardrail")
        criteria = _coerce(filters, PolicyFilter, what="o filtro de politicas")
        selection: Json = {}
        if criteria.stage is not None:
            selection["stage"] = criteria.stage
        if criteria.is_active is not None:
            selection["is_active"] = criteria.is_active
        if criteria.search:
            selection["search"] = criteria.search
        async with self._container.uow_factory() as uow:
            items = await uow.guardrails.list(
                **selection, limit=criteria.limit, offset=criteria.offset
            )
            total = await uow.guardrails.count(**selection)
        return Page(items=list(items), total=total, limit=criteria.limit, offset=criteria.offset)


class UpdatePolicy(_GuardrailUseCase):
    """Atualiza uma politica existente, revalidando as regras informadas.

    Trocar as regras de uma politica ja vinculada a um modulo muda o
    comportamento **sem redeploy** (SPEC-0003 secao 6, criterio 4): por isso a
    validacao acontece antes da gravacao, nunca durante a execucao.
    """

    async def execute(
        self,
        reference: str,
        data: PolicyUpdateInput | Mapping[str, Any],
        principal: Principal,
    ) -> GuardrailPolicy:
        """Aplica somente os campos informados e grava a politica."""
        authorize(principal, Permission.GUARDRAIL_WRITE, "alterar politicas de guardrail")
        payload = _coerce(data, PolicyUpdateInput, what="a atualizacao de politica")
        changes = payload.changes()
        async with self._container.uow_factory() as uow:
            policy = await _require_policy(uow, reference)
            if not changes:
                return policy
            stored = await uow.guardrails.update(
                policy.model_copy(update={**changes, "updated_at": utcnow()})
            )
            await uow.commit()
        _logger.info(
            "guardrail_policy_updated",
            slug=stored.slug,
            fields=sorted(changes),
            rules=len(stored.rules),
        )
        return stored


class DeletePolicy(_GuardrailUseCase):
    """Remove uma politica de guardrail do catalogo."""

    async def execute(self, reference: str, principal: Principal) -> None:
        """Apaga a politica; modulos vinculados passam a operar sem restricao no estagio."""
        authorize(principal, Permission.GUARDRAIL_WRITE, "remover politicas de guardrail")
        async with self._container.uow_factory() as uow:
            policy = await _require_policy(uow, reference)
            await uow.guardrails.delete(policy.id)
            await uow.commit()
        _logger.info("guardrail_policy_deleted", slug=policy.slug, stage=policy.stage.value)


# ---------------------------------------------------------------------------
# Testador e catalogo
# ---------------------------------------------------------------------------
class TestPolicy(_GuardrailUseCase):
    """Aplica uma politica a um texto e devolve o veredito completo.

    E o motor do testador do console: aceita o **slug/id de uma politica salva**
    ou uma **politica avulsa** (o rascunho aberto no editor) e devolve o
    `GuardrailVerdict` inteiro — conteudo final ja redigido, achados por regra,
    latencia e o `allowed`. Nada e persistido e nenhuma execucao e criada.
    """

    async def execute(
        self,
        policy: str | GuardrailPolicy | Mapping[str, Any] | None,
        content: str,
        principal: Principal,
        *,
        stage: GuardrailStage | str | None = None,
        context: Json | None = None,
    ) -> GuardrailVerdict:
        """Roda `container.guardrails.apply` sobre o conteudo e devolve o veredito.

        Politica salva exige apenas leitura; rascunho avulso e trabalho de autoria
        e exige `guardrail:write`. Politica `None` exercita o caminho permissivo
        (estagio sem restricao), que tambem precisa ser demonstravel na UI.
        """
        wanted = _as_stage(stage) if stage is not None else None
        resolved = await self._resolve(policy, principal, stage=wanted)
        applied = resolved.stage if resolved is not None else (wanted or GuardrailStage.INPUT)
        evaluation: Json = dict(context or {})
        evaluation.setdefault("stage", applied.value)
        evaluation.setdefault("tenant_id", principal.tenant_id)
        evaluation.setdefault("actor", principal.subject)
        verdict = await self._container.guardrails.apply(
            content or "", resolved, context=evaluation
        )
        _logger.info(
            "guardrail_policy_tested",
            slug=resolved.slug if resolved is not None else None,
            stage=verdict.stage.value,
            allowed=verdict.allowed,
            findings=len(verdict.findings),
            latency_ms=verdict.latency_ms,
        )
        return verdict

    async def _resolve(
        self,
        policy: str | GuardrailPolicy | Mapping[str, Any] | None,
        principal: Principal,
        *,
        stage: GuardrailStage | None,
    ) -> GuardrailPolicy | None:
        """Resolve a politica salva ou monta a avulsa, exigindo a permissao devida."""
        if policy is None:
            authorize(principal, Permission.GUARDRAIL_READ, "testar politicas de guardrail")
            return None
        if isinstance(policy, str):
            authorize(principal, Permission.GUARDRAIL_READ, "testar politicas de guardrail")
            async with self._container.uow_factory() as uow:
                return await _require_policy(uow, policy)
        authorize(principal, Permission.GUARDRAIL_WRITE, "testar politicas avulsas de guardrail")
        return _inline_policy(policy, stage=stage)


class ListRuleKinds(_GuardrailUseCase):
    """Descreve os tipos de regra disponiveis para o editor de politicas da UI."""

    async def execute(self, principal: Principal) -> list[Json]:
        """Devolve `{"kind", "descricao", "config_schema", "acoes_suportadas"}` por tipo."""
        authorize(principal, Permission.GUARDRAIL_READ, "ler o catalogo de regras de guardrail")
        return deepcopy(rule_kind_catalog())
