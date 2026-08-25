"""Avaliador `json_schema`: valida a saida como JSON contra um subconjunto de JSON Schema.

Validador proprio, sem dependencia nova (SPEC-0003, secao 3). Palavras-chave
suportadas: `type` (object, array, string, number, integer, boolean, null — ou uma
lista delas), `properties`, `required`, `items`, `enum`, `minimum`, `maximum`,
`minLength`, `maxLength`, `pattern` e `additionalProperties`. Cada erro sai com o
caminho JSONPath do valor ofensor, por exemplo
`$.evidence.speech_match: esperado number, recebido string`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    compile_pattern,
    config_bool,
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
    "SUPPORTED_KEYWORDS",
    "JsonSchemaEvaluator",
    "extract_json",
    "validate_instance",
    "validate_rule",
]

SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "additionalProperties",
        "title",
        "description",
        "default",
        "examples",
    }
)
"""Palavras-chave reconhecidas (as quatro ultimas sao apenas documentais)."""

_MAX_ERRORS: Final[int] = 20
_MESSAGE_ERRORS: Final[int] = 5
_SIMPLE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FENCE = re.compile(r"^```[A-Za-z0-9_+\-]*\s*|\s*```$")


def _type_name(value: Any) -> str:
    """Nome JSON do tipo de um valor Python."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    """Confere um valor contra um nome de tipo JSON Schema."""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValidationError(
        f"Tipo '{expected}' nao suportado pelo validador de JSON Schema do lukato.",
        details={"type": expected, "supported": sorted(SUPPORTED_KEYWORDS)},
    )


def _child_path(path: str, key: str) -> str:
    """Monta o caminho de uma propriedade filha."""
    if _SIMPLE_KEY.match(key):
        return f"{path}.{key}"
    return f'{path}["{key}"]'


def validate_instance(instance: Any, schema: Any, path: str = "$") -> list[str]:
    """Valida um valor ja desserializado contra o schema; devolve os erros achados."""
    errors: list[str] = []
    if schema is True or schema is None:
        return errors
    if schema is False:
        return [f"{path}: nenhum valor e aceito neste ponto do schema"]
    if not isinstance(schema, dict):
        raise ValidationError(
            "Schema JSON invalido: cada no deve ser um objeto ou um booleano.",
            details={"path": path, "type": type(schema).__name__},
        )

    expected = schema.get("type")
    if expected is not None:
        candidates = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(instance, str(name)) for name in candidates):
            errors.append(
                f"{path}: esperado {' | '.join(str(name) for name in candidates)}, "
                f"recebido {_type_name(instance)}"
            )
            return errors  # sem o tipo certo, as demais restricoes nao fazem sentido

    if "enum" in schema:
        allowed = schema["enum"]
        if isinstance(allowed, list) and instance not in allowed:
            errors.append(
                f"{path}: valor {json.dumps(instance, ensure_ascii=False)} fora do enum "
                f"{json.dumps(allowed, ensure_ascii=False)}"
            )

    if isinstance(instance, str):
        errors.extend(_validate_string(instance, schema, path))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        errors.extend(_validate_number(instance, schema, path))
    elif isinstance(instance, dict):
        errors.extend(_validate_object(instance, schema, path))
    elif isinstance(instance, list):
        errors.extend(_validate_array(instance, schema, path))

    return errors[:_MAX_ERRORS]


def _validate_string(instance: str, schema: dict[str, Any], path: str) -> list[str]:
    """Restricoes de texto: `minLength`, `maxLength` e `pattern`."""
    errors: list[str] = []
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        errors.append(f"{path}: texto com {len(instance)} caracteres, minimo {minimum}")
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and len(instance) > maximum:
        errors.append(f"{path}: texto com {len(instance)} caracteres, maximo {maximum}")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and not compile_pattern(pattern).search(instance):
        errors.append(f"{path}: texto nao casa com o padrao '{pattern}'")
    return errors


def _validate_number(instance: float, schema: dict[str, Any], path: str) -> list[str]:
    """Restricoes numericas: `minimum` e `maximum`."""
    errors: list[str] = []
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and instance < minimum:
        errors.append(f"{path}: valor {instance} abaixo do minimo {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and instance > maximum:
        errors.append(f"{path}: valor {instance} acima do maximo {maximum}")
    return errors


def _validate_object(instance: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    """Restricoes de objeto: `required`, `properties` e `additionalProperties`."""
    errors: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in instance:
                errors.append(f"{path}: campo obrigatorio '{key}' ausente")

    for key, subschema in properties.items():
        if key in instance:
            errors.extend(validate_instance(instance[key], subschema, _child_path(path, key)))

    additional = schema.get("additionalProperties", True)
    extras = [key for key in instance if key not in properties]
    if additional is False:
        for key in extras:
            errors.append(f"{path}: propriedade '{key}' nao permitida pelo schema")
    elif isinstance(additional, dict):
        for key in extras:
            errors.extend(validate_instance(instance[key], additional, _child_path(path, key)))
    return errors


def _validate_array(instance: list[Any], schema: dict[str, Any], path: str) -> list[str]:
    """Restricoes de lista: `items` (schema unico ou posicional)."""
    errors: list[str] = []
    items = schema.get("items")
    if isinstance(items, list):
        for index, subschema in enumerate(items):
            if index < len(instance):
                errors.extend(validate_instance(instance[index], subschema, f"{path}[{index}]"))
    elif items is not None:
        for index, element in enumerate(instance):
            errors.extend(validate_instance(element, items, f"{path}[{index}]"))
    return errors


def extract_json(content: str) -> str:
    """Extrai o trecho JSON de uma resposta com cerca markdown ou texto ao redor."""
    text = content.strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()
    if text.startswith(("{", "[")):
        return text
    starts = [position for position in (text.find("{"), text.find("[")) if position >= 0]
    if not starts:
        return text
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        return text
    return text[start : end + 1]


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `json_schema`."""
    schema = rule.config.get("schema")
    if not isinstance(schema, dict):
        raise ValidationError(
            f"A regra json_schema '{rule.id}' exige 'schema' como objeto JSON Schema.",
            details={"rule_id": rule.id, "type": type(schema).__name__},
        )


class JsonSchemaEvaluator:
    """`json_schema`: exige que o conteudo seja JSON valido conforme o schema."""

    kind: GuardrailRuleKind = GuardrailRuleKind.JSON_SCHEMA

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Desserializa o conteudo e valida contra o schema da regra."""
        validate_rule(rule)
        schema = rule.config["schema"]
        coerce = config_bool(rule.config, "coerce", default=False)
        payload = extract_json(content) if coerce else content.strip()

        if not payload:
            return self._finding(rule, "Conteudo vazio: era esperado um documento JSON.", content)
        try:
            instance = json.loads(payload)
        except json.JSONDecodeError as exc:
            message = (
                f"Conteudo nao e JSON valido: {exc.msg} (linha {exc.lineno}, coluna {exc.colno})."
            )
            return self._finding(rule, message, content)

        errors = validate_instance(instance, schema)
        if not errors:
            return None
        head = "; ".join(errors[:_MESSAGE_ERRORS])
        suffix = "" if len(errors) <= _MESSAGE_ERRORS else f" (+{len(errors) - _MESSAGE_ERRORS})"
        return self._finding(
            rule, f"Saida JSON nao satisfaz o schema: {head}{suffix}.", content, errors
        )

    @staticmethod
    def _finding(
        rule: GuardrailRule, message: str, content: str, errors: list[str] | None = None
    ) -> GuardrailFinding:
        """Monta o achado preservando o conteudo quando a acao e de transformacao."""
        detail = "\n".join(errors) if errors else snippet(content)
        # Um schema nao sabe consertar o texto: em acao de conteudo nada e reescrito.
        evidence = content if rule.action in CONTENT_ACTIONS else snippet(detail, limit=600)
        return build_finding(rule, message, evidence=evidence, span=(0, len(content)))
