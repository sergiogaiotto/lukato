"""Contrato unico dos building blocks do lukato.

Define o que entra (`ModuleRequest`), o que sai (`ModuleResponse`), tudo o que o
modulo recebe pronto (`ModuleContext`), como ele se anuncia na UI (`UIDescriptor`)
e a classe base que todo building block herda (`BaseModule`).

Um building block **nunca** importa adaptadores nem interfaces: qualquer porta de
que precise chega injetada pelo contexto.
"""

from __future__ import annotations

import copy
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar, cast

from pydantic import Field

from lukato.domain.errors import ConfigurationError, UnsupportedCapability, ValidationError
from lukato.domain.models.base import DomainModel
from lukato.domain.models.guardrail import GuardrailFinding
from lukato.domain.models.identity import Principal
from lukato.domain.models.module import ModuleBinding, ModuleDefinition, ModuleKind
from lukato.domain.models.run import TokenUsage
from lukato.domain.ports.embeddings import EmbeddingPort
from lukato.domain.ports.guardrail import GuardrailPort
from lukato.domain.ports.llm import ChatMessage, LLMPort
from lukato.domain.ports.observability import TracerPort
from lukato.domain.ports.orchestrator import OrchestratorPort
from lukato.domain.ports.unit_of_work import UnitOfWorkFactory
from lukato.domain.types import Id, Json

__all__ = [
    "SCHEMA_TYPES",
    "BaseModule",
    "ModuleContext",
    "ModuleRequest",
    "ModuleResponse",
    "UIDescriptor",
    "UINavItem",
    "validate_against_schema",
]

_logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Troca de mensagens
# ---------------------------------------------------------------------------
class ModuleRequest(DomainModel):
    """Entrada de uma execucao de building block."""

    input: str = ""
    payload: Json = Field(default_factory=dict)
    variables: Json = Field(default_factory=dict)
    history: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False


class ModuleResponse(DomainModel):
    """Saida de uma execucao de building block, com consumo e evidencias."""

    output: str = ""
    data: Json = Field(default_factory=dict)
    run_id: Id | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    findings: list[GuardrailFinding] = Field(default_factory=list)
    metadata: Json = Field(default_factory=dict)

    @classmethod
    def text(cls, output: str, **kw: Any) -> ModuleResponse:
        """Cria uma resposta puramente textual."""
        return cls(output=output, **kw)

    @classmethod
    def structured(cls, data: Json, **kw: Any) -> ModuleResponse:
        """Cria uma resposta com carga estruturada em `data`."""
        return cls(data=data, **kw)


# ---------------------------------------------------------------------------
# Descricao da presenca na UI
# ---------------------------------------------------------------------------
class UINavItem(DomainModel):
    """Item de menu que o modulo publica na sidebar do console."""

    label: str
    icon: str
    endpoint: str
    section: str = "FUNCIONALIDADE"
    order: int = 100


class UIDescriptor(DomainModel):
    """Presenca do modulo na UI: itens de menu, templates e cor de destaque."""

    nav: list[UINavItem] = Field(default_factory=list)
    center_template: str | None = None
    context_template: str | None = None
    accent: str = "#c8102e"


# ---------------------------------------------------------------------------
# Contexto de execucao
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ModuleContext:
    """Tudo o que um building block recebe pronto para executar.

    Nenhum modulo abre conexao propria nem le variaveis de ambiente: portas,
    orquestradores e servicos auxiliares chegam por aqui.
    """

    definition: ModuleDefinition
    principal: Principal
    llm: LLMPort
    embeddings: EmbeddingPort
    guardrails: GuardrailPort
    tracer: TracerPort
    uow_factory: UnitOfWorkFactory
    orchestrators: Mapping[str, OrchestratorPort]
    settings: Any
    services: Mapping[str, Any] = field(default_factory=dict)

    def service(self, name: str, expected: type[T] | None = None) -> T:
        """Resolve um servico auxiliar pelo nome, opcionalmente checando o tipo.

        Servico ausente levanta :class:`UnsupportedCapability` listando os
        disponiveis; servico presente com o tipo errado levanta
        :class:`ConfigurationError`.
        """
        available = sorted(self.services)
        try:
            found = self.services[name]
        except KeyError:
            listing = ", ".join(available) if available else "nenhum"
            raise UnsupportedCapability(
                f"Servico '{name}' indisponivel para o modulo "
                f"'{self.definition.slug}'. Servicos disponiveis: {listing}.",
                details={"service": name, "available": available},
            ) from None

        if expected is not None and not self._is_expected(found, expected, name):
            raise ConfigurationError(
                f"Servico '{name}' tem o tipo errado: esperado "
                f"{expected.__name__}, recebido {type(found).__name__}.",
                details={
                    "service": name,
                    "expected": expected.__name__,
                    "received": type(found).__name__,
                },
            )
        return cast(T, found)

    @staticmethod
    def _is_expected(found: Any, expected: type[Any], name: str) -> bool:
        """Aplica `isinstance` tolerando protocolos nao verificaveis em runtime."""
        try:
            return isinstance(found, expected)
        except TypeError:
            _logger.debug(
                "Tipo do servico '%s' nao e verificavel em runtime (%s); checagem ignorada.",
                name,
                getattr(expected, "__name__", expected),
            )
            return True


# ---------------------------------------------------------------------------
# Validador minimo de JSON Schema (subconjunto usado por `config_schema`)
# ---------------------------------------------------------------------------
SCHEMA_TYPES: frozenset[str] = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
"""Tipos de JSON Schema aceitos por :func:`validate_against_schema`."""


def _type_name(value: Any) -> str:
    """Nome JSON Schema do tipo de um valor Python."""
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
    if isinstance(value, list | tuple):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, declared: str) -> bool:
    """True quando `value` satisfaz o tipo JSON Schema `declared`."""
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list | tuple)
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if declared == "null":
        return value is None
    raise ConfigurationError(
        f"Tipo de JSON Schema desconhecido: '{declared}'.",
        details={"type": declared, "supported": sorted(SCHEMA_TYPES)},
    )


def _invalid(path: str, expected: str, value: Any, **extra: Any) -> ValidationError:
    """Monta o erro de validacao com o caminho do campo em `details`."""
    details: Json = {
        "path": path,
        "expected": expected,
        "received": _type_name(value),
    }
    details.update(extra)
    return ValidationError(f"{path}: esperado {expected}", details=details)


def _declared_types(schema: Json, path: str) -> list[str]:
    """Extrai a lista de tipos declarados no sub-schema (aceita `type` como lista)."""
    declared = schema.get("type")
    if declared is None:
        return []
    names = [declared] if isinstance(declared, str) else list(declared)
    for name in names:
        if not isinstance(name, str) or name not in SCHEMA_TYPES:
            raise ConfigurationError(
                f"Tipo de JSON Schema desconhecido em {path}: {name!r}.",
                details={"path": path, "type": name, "supported": sorted(SCHEMA_TYPES)},
            )
    return names


def _check_bounds(value: Any, schema: Json, path: str) -> None:
    """Aplica `minimum`, `maximum`, `minLength` e `maxLength`."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise ValidationError(
                f"{path}: esperado valor >= {minimum} (recebido {value})",
                details={"path": path, "minimum": minimum, "value": value},
            )
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            raise ValidationError(
                f"{path}: esperado valor <= {maximum} (recebido {value})",
                details={"path": path, "maximum": maximum, "value": value},
            )
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            raise ValidationError(
                f"{path}: esperado texto com pelo menos {min_length} caractere(s)",
                details={"path": path, "minLength": min_length, "length": len(value)},
            )
        max_length = schema.get("maxLength")
        if max_length is not None and len(value) > max_length:
            raise ValidationError(
                f"{path}: esperado texto com no maximo {max_length} caractere(s)",
                details={"path": path, "maxLength": max_length, "length": len(value)},
            )


def _validate_object(value: dict[str, Any], schema: Json, path: str) -> Json:
    """Valida um objeto: propriedades, obrigatorios, defaults e extras."""
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ConfigurationError(
            f"'properties' deve ser um objeto em {path}.", details={"path": path}
        )
    required = list(schema.get("required") or [])
    additional = schema.get("additionalProperties", True)

    result: Json = {}
    for key, subschema in properties.items():
        child_path = f"{path}.{key}"
        if key in value:
            result[key] = validate_against_schema(value[key], subschema, path=child_path)
        elif isinstance(subschema, dict) and "default" in subschema:
            result[key] = copy.deepcopy(subschema["default"])
        elif key in required:
            raise ValidationError(
                f"{child_path}: campo obrigatorio ausente",
                details={"path": child_path, "required": True},
            )

    for key in required:
        if key not in result and key not in value:
            child_path = f"{path}.{key}"
            raise ValidationError(
                f"{child_path}: campo obrigatorio ausente",
                details={"path": child_path, "required": True},
            )

    extras = [key for key in value if key not in properties]
    if additional is False and extras:
        raise ValidationError(
            f"{path}: propriedade nao permitida: {sorted(extras)[0]}",
            details={"path": path, "unexpected": sorted(extras)},
        )
    if isinstance(additional, dict):
        for key in extras:
            result[key] = validate_against_schema(value[key], additional, path=f"{path}.{key}")
    else:
        for key in extras:
            result[key] = value[key]
    return result


def validate_against_schema(value: Any, schema: Json, *, path: str = "$") -> Any:
    """Valida `value` contra um subconjunto de JSON Schema e devolve o valor normalizado.

    Suporta `type` (object/array/string/number/integer/boolean/null), `properties`,
    `required`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`,
    `default` e `additionalProperties`. Preenche os `default` ausentes — uma
    propriedade `required` que declara `default` e considerada satisfeita por ele.
    Erro de dado levanta :class:`ValidationError` com o caminho em `details["path"]`;
    erro no proprio schema levanta :class:`ConfigurationError`.
    """
    if not isinstance(schema, dict):
        raise ConfigurationError(
            f"Sub-schema invalido em {path}: esperado objeto.", details={"path": path}
        )
    if not schema:
        return value

    names = _declared_types(schema, path)
    if names and not any(_matches_type(value, name) for name in names):
        raise _invalid(path, " | ".join(names), value)

    choices = schema.get("enum")
    if choices is not None:
        if not isinstance(choices, list):
            raise ConfigurationError(
                f"'enum' deve ser uma lista em {path}.", details={"path": path}
            )
        if value not in choices:
            raise ValidationError(
                f"{path}: esperado um de {choices}",
                details={"path": path, "enum": choices, "value": value},
            )

    _check_bounds(value, schema, path)

    if isinstance(value, list | tuple):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [
                validate_against_schema(item, item_schema, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return list(value)

    is_object_schema = "properties" in schema or "required" in schema or "object" in names
    if isinstance(value, dict) and is_object_schema:
        return _validate_object(value, schema, path)
    return value


# ---------------------------------------------------------------------------
# Contrato do building block
# ---------------------------------------------------------------------------
class BaseModule(ABC):
    """Building block plugavel: unidade de funcionalidade do ecossistema.

    Subclasses declaram sua identidade nos `ClassVar` e implementam `handle`.
    Toda dependencia externa chega por :class:`ModuleContext`.
    """

    kind: ClassVar[ModuleKind]
    slug: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[tuple[str, ...]] = ()
    config_schema: ClassVar[Json] = {}
    default_binding: ClassVar[ModuleBinding] = ModuleBinding()

    async def setup(self, ctx: ModuleContext) -> None:  # noqa: B027
        """Inicializacao opcional, executada uma unica vez antes do primeiro `handle`."""

    async def teardown(self) -> None:  # noqa: B027
        """Liberacao opcional de recursos no encerramento da aplicacao."""

    @abstractmethod
    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Ponto unico de execucao do building block."""
        raise NotImplementedError

    def ui(self) -> UIDescriptor:
        """Descreve a presenca do modulo na UI; por padrao, nenhuma."""
        return UIDescriptor()

    def health(self) -> Json:
        """Resumo de saude do modulo, exposto pelo registry e por `/readyz`."""
        return {
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "capabilities": list(self.capabilities),
        }

    def validate_config(self, config: Json) -> Json:
        """Valida `config` contra `config_schema` e devolve a copia normalizada.

        Preenche os `default` declarados no schema. Campo invalido levanta
        :class:`ValidationError` com o caminho em `details["path"]`.
        """
        if not isinstance(config, dict):
            raise _invalid("$", "object", config)
        schema = self.config_schema or {}
        if not schema:
            return dict(config)
        normalized = validate_against_schema(dict(config), schema, path="$")
        if not isinstance(normalized, dict):
            raise _invalid("$", "object", normalized)
        return normalized

    def __repr__(self) -> str:
        slug = getattr(type(self), "slug", "?")
        return f"{type(self).__name__}(slug={slug!r}, version={self.version!r})"
