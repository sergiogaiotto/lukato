"""Schemas do recurso `/api/v1/modules`: definicao, trinca e invocacao.

Um building block e *codigo*; a `ModuleDefinition` e *configuracao*. O que este
modulo expoe e a configuracao: a trinca parametrizavel
(guardrail de entrada -> system prompt -> guardrail de saida), o runtime e o
resultado de uma invocacao.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from lukato.application.dto import UNSET, Maybe, ModuleCreateInput, ModuleUpdateInput
from lukato.domain.models.module import (
    MODULE_SLUG_PATTERN,
    ModuleBinding,
    ModuleDefinition,
    ModuleKind,
    ModuleStatus,
)
from lukato.domain.ports.llm import ChatMessage
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import InSchema, OutSchema, TokenUsageOut
from lukato.interfaces.http.schemas.guardrails import GuardrailFindingOut
from lukato.modules.base import ModuleRequest, ModuleResponse

__all__ = [
    "ChatMessageIn",
    "ModuleBindingIn",
    "ModuleBindingOut",
    "ModuleCreate",
    "ModuleInvokeRequest",
    "ModuleInvokeResponse",
    "ModuleOut",
    "ModuleStatusUpdate",
    "ModuleUpdate",
]

_BINDING_EXAMPLE: dict[str, Any] = {
    "input_guardrail_id": "entrada-padrao",
    "system_prompt_id": "atendimento-base",
    "output_guardrail_id": "saida-padrao",
    "model": "qwen-latest",
    "temperature": 0.2,
    "max_tokens": 1024,
    "timeout_seconds": 60.0,
    "tools": ["knowledge_search", "now"],
}


# ---------------------------------------------------------------------------
# Trinca parametrizavel
# ---------------------------------------------------------------------------
class ModuleBindingIn(InSchema):
    """Trinca parametrizavel enviada na criacao ou atualizacao de um modulo."""

    input_guardrail_id: Id | None = Field(
        default=None, description="Politica aplicada ao conteudo de ENTRADA."
    )
    system_prompt_id: Id | None = Field(
        default=None, description="Template do system prompt renderizado antes do runtime."
    )
    output_guardrail_id: Id | None = Field(
        default=None, description="Politica aplicada ao conteudo de SAIDA."
    )
    model: str | None = Field(default=None, description="Modelo de LLM; ausente usa o padrao.")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0, description="Temperatura.")
    max_tokens: int | None = Field(default=None, ge=1, le=8192, description="Teto de tokens.")
    timeout_seconds: float = Field(default=60.0, gt=0.0, description="Teto de tempo da execucao.")
    tools: list[str] = Field(default_factory=list, description="Ferramentas liberadas ao modulo.")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": _BINDING_EXAMPLE})

    def to_domain(self) -> ModuleBinding:
        """Converte para o value object de dominio."""
        return ModuleBinding(
            input_guardrail_id=self.input_guardrail_id,
            system_prompt_id=self.system_prompt_id,
            output_guardrail_id=self.output_guardrail_id,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            tools=list(self.tools),
        )


class ModuleBindingOut(OutSchema):
    """Trinca parametrizavel devolvida com a definicao do modulo."""

    input_guardrail_id: Id | None = None
    system_prompt_id: Id | None = None
    output_guardrail_id: Id | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float = 60.0
    tools: list[str] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, binding: ModuleBinding) -> ModuleBindingOut:
        """Converte o value object de dominio."""
        return cls(
            input_guardrail_id=binding.input_guardrail_id,
            system_prompt_id=binding.system_prompt_id,
            output_guardrail_id=binding.output_guardrail_id,
            model=binding.model,
            temperature=binding.temperature,
            max_tokens=binding.max_tokens,
            timeout_seconds=binding.timeout_seconds,
            tools=list(binding.tools),
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
class ModuleCreate(InSchema):
    """Corpo de `POST /api/v1/modules`."""

    slug: str = Field(pattern=MODULE_SLUG_PATTERN, description="Identificador estavel do modulo.")
    name: str = Field(default="", description="Nome exibido no console.")
    description: str = Field(default="", description="Para que serve este modulo.")
    kind: ModuleKind = Field(default=ModuleKind.AGENT, description="Tipo funcional.")
    status: ModuleStatus = Field(default=ModuleStatus.DRAFT, description="Ciclo de vida inicial.")
    runtime: str = Field(default="langgraph", description="Runtime de orquestracao.")
    binding: ModuleBindingIn | None = Field(default=None, description="Trinca parametrizavel.")
    config: Json = Field(default_factory=dict, description="Configuracao livre do building block.")
    tags: list[str] = Field(default_factory=list, description="Etiquetas de organizacao.")
    owner: str | None = Field(default=None, description="Responsavel pelo modulo.")
    version: str = Field(default="1.0.0", description="Versao desta configuracao.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "slug": "atendimento",
                "name": "Atendimento ao cliente",
                "description": "Responde duvidas de plano e fatura.",
                "kind": "agent",
                "status": "active",
                "runtime": "langgraph",
                "binding": _BINDING_EXAMPLE,
                "config": {"max_steps": 6},
                "tags": ["suporte"],
                "owner": "squad-atendimento",
                "version": "1.0.0",
            }
        },
    )

    def to_input(self) -> ModuleCreateInput:
        """Converte para o DTO do caso de uso `CreateModule`."""
        return ModuleCreateInput(
            slug=self.slug,
            name=self.name,
            description=self.description,
            kind=self.kind,
            status=self.status,
            runtime=self.runtime,
            binding=self.binding.to_domain() if self.binding is not None else None,
            config=dict(self.config),
            tags=list(self.tags),
            owner=self.owner,
            version=self.version,
        )


class ModuleUpdate(InSchema):
    """Corpo de `PUT /api/v1/modules/{ref}`: apenas o que foi enviado muda."""

    name: str | None = None
    description: str | None = None
    kind: ModuleKind | None = None
    status: ModuleStatus | None = None
    runtime: str | None = None
    binding: ModuleBindingIn | None = None
    config: Json | None = None
    tags: list[str] | None = None
    owner: str | None = None
    version: str | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"status": "paused", "binding": {"temperature": 0.0, "tools": []}}
        },
    )

    def to_input(self) -> ModuleUpdateInput:
        """Converte para o DTO parcial, preservando o que nao foi enviado.

        `owner: null` apaga o dono; `owner` ausente o mantem — a distincao vem de
        `model_fields_set`, nao do valor.
        """
        sent = self.model_fields_set

        def maybe(field: str, value: Any) -> Maybe[Any]:
            return value if field in sent else UNSET

        binding = self.binding.to_domain() if self.binding is not None else None
        return ModuleUpdateInput(
            name=maybe("name", self.name),
            description=maybe("description", self.description),
            kind=maybe("kind", self.kind),
            status=maybe("status", self.status),
            runtime=maybe("runtime", self.runtime),
            binding=maybe("binding", binding),
            config=maybe("config", dict(self.config) if self.config is not None else None),
            tags=maybe("tags", list(self.tags) if self.tags is not None else None),
            owner=maybe("owner", self.owner),
            version=maybe("version", self.version),
        )


class ModuleStatusUpdate(InSchema):
    """Corpo de `PATCH /api/v1/modules/{ref}/status`."""

    status: ModuleStatus = Field(description="Novo estado do ciclo de vida.")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {"status": "active"}})


class ModuleOut(OutSchema):
    """Definicao de modulo devolvida pela API."""

    id: Id
    slug: str
    name: str
    description: str = ""
    kind: ModuleKind = ModuleKind.AGENT
    status: ModuleStatus = ModuleStatus.DRAFT
    runtime: str = "langgraph"
    binding: ModuleBindingOut = Field(default_factory=ModuleBindingOut)
    config: Json = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    version: str = "1.0.0"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, definition: ModuleDefinition) -> ModuleOut:
        """Converte a entidade de dominio."""
        return cls(
            id=definition.id,
            slug=definition.slug,
            name=definition.name,
            description=definition.description,
            kind=definition.kind,
            status=definition.status,
            runtime=definition.runtime,
            binding=ModuleBindingOut.from_domain(definition.binding),
            config=dict(definition.config),
            tags=list(definition.tags),
            owner=definition.owner,
            version=definition.version,
            created_at=definition.created_at,
            updated_at=definition.updated_at,
        )


# ---------------------------------------------------------------------------
# Invocacao
# ---------------------------------------------------------------------------
class ChatMessageIn(InSchema):
    """Mensagem previa da conversa, enviada como historico."""

    role: str = Field(description="Papel da mensagem: system, user ou assistant.")
    content: str = Field(description="Conteudo textual da mensagem.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"role": "user", "content": "Qual o valor da fatura?"}},
    )

    def to_domain(self) -> ChatMessage:
        """Converte para a mensagem aceita pela porta de LLM."""
        return ChatMessage(role=self.role, content=self.content)


class ModuleInvokeRequest(InSchema):
    """Corpo de `POST /api/v1/modules/{slug}/invoke`."""

    input: str = Field(default="", description="Texto de entrada do usuario.")
    payload: Json = Field(default_factory=dict, description="Dados estruturados do pedido.")
    variables: Json = Field(
        default_factory=dict, description="Variaveis do system prompt (`{{ var }}`)."
    )
    history: list[ChatMessageIn] = Field(
        default_factory=list, description="Historico da conversa, do mais antigo ao mais recente."
    )
    stream: bool = Field(default=False, description="Pedido de resposta incremental.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "input": "Explique a cobranca de julho.",
                "payload": {"conta": "123456"},
                "variables": {"canal": "app"},
                "history": [],
                "stream": False,
            }
        },
    )

    def to_request(self) -> ModuleRequest:
        """Converte para o pedido consumido por `InvokeModule`."""
        return ModuleRequest(
            input=self.input,
            payload=dict(self.payload),
            variables=dict(self.variables),
            history=[message.to_domain() for message in self.history],
            stream=self.stream,
        )


class ModuleInvokeResponse(OutSchema):
    """Resultado de uma invocacao: saida, custo, achados e rastro da execucao."""

    output: str = Field(default="", description="Texto final ja aprovado pelo guardrail de saida.")
    data: Json = Field(default_factory=dict, description="Resultado estruturado do modulo.")
    run_id: Id | None = Field(default=None, description="Execucao registrada para auditoria.")
    usage: TokenUsageOut = Field(default_factory=TokenUsageOut, description="Consumo de tokens.")
    cost_usd: float = Field(default=0.0, ge=0.0, description="Custo apurado em USD.")
    findings: list[GuardrailFindingOut] = Field(
        default_factory=list, description="Achados dos guardrails de entrada e de saida."
    )
    metadata: Json = Field(default_factory=dict, description="Rastro: runtime, trace e latencia.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "output": "A cobranca de julho inclui o plano e um servico avulso.",
                "data": {},
                "run_id": "0f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
                "usage": {"prompt_tokens": 312, "completion_tokens": 88, "total_tokens": 400},
                "cost_usd": 0.0012,
                "findings": [],
                "metadata": {"runtime": "langgraph", "latency_ms": 842.5},
            }
        }
    )

    @classmethod
    def from_domain(cls, response: ModuleResponse) -> ModuleInvokeResponse:
        """Converte a resposta do building block."""
        return cls(
            output=response.output,
            data=dict(response.data),
            run_id=response.run_id,
            usage=TokenUsageOut.from_domain(response.usage),
            cost_usd=response.cost_usd,
            findings=[GuardrailFindingOut.from_domain(item) for item in response.findings],
            metadata=dict(response.metadata),
        )
