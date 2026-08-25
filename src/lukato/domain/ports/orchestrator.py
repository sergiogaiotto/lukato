"""Porta de orquestracao de runtimes de agente (LangGraph, Deep-Agent, direto)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from lukato.domain.models.base import DomainModel
from lukato.domain.models.module import ModuleDefinition
from lukato.domain.models.run import RunStep, TokenUsage
from lukato.domain.ports.llm import ChatMessage
from lukato.domain.types import Json

__all__ = ["OrchestratorPort", "OrchestratorRequest", "OrchestratorResult"]


class OrchestratorRequest(DomainModel):
    """Pedido entregue ao runtime depois do guardrail de entrada e do prompt resolvido."""

    module: ModuleDefinition
    input_text: str
    variables: Json = Field(default_factory=dict)
    history: list[ChatMessage] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    metadata: Json = Field(default_factory=dict)


class OrchestratorResult(DomainModel):
    """Resultado bruto do runtime, antes do guardrail de saida."""

    output_text: str
    steps: list[RunStep] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metadata: Json = Field(default_factory=dict)


@runtime_checkable
class OrchestratorPort(Protocol):
    """Contrato de um runtime capaz de executar a definicao de um modulo."""

    name: str

    async def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        """Executa o pedido no runtime e devolve texto, passos e consumo."""
        ...

    def supports(self, runtime: str) -> bool:
        """True quando este orquestrador atende o runtime declarado pelo modulo."""
        ...
