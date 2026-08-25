"""Porta de modelo de linguagem (LLM) e os modelos de troca de mensagens."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from lukato.domain.models.base import DomainModel
from lukato.domain.models.run import TokenUsage
from lukato.domain.types import Json

__all__ = ["ChatMessage", "LLMPort", "LLMResponse"]


class ChatMessage(DomainModel):
    """Mensagem de uma conversa com o LLM (`system`, `user`, `assistant`, ...)."""

    role: str
    content: str

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        """Cria uma mensagem de sistema."""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        """Cria uma mensagem do usuario."""
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> ChatMessage:
        """Cria uma mensagem do assistente."""
        return cls(role="assistant", content=content)


class LLMResponse(DomainModel):
    """Resposta completa de uma chamada de chat, com consumo e metadados do provedor."""

    content: str
    model: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    raw: Json = Field(default_factory=dict)
    latency_ms: float = 0.0


@runtime_checkable
class LLMPort(Protocol):
    """Contrato de um provedor de chat completions (rede ou fallback deterministico)."""

    @property
    def default_model(self) -> str:
        """Modelo usado quando a chamada nao informa um explicitamente."""
        ...

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
        response_format: Json | None = None,
        metadata: Json | None = None,
    ) -> LLMResponse:
        """Executa uma chamada de chat e devolve a resposta consolidada."""
        ...

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """Devolve um iterador assincrono com os fragmentos incrementais da resposta."""
        ...

    async def list_models(self) -> list[str]:
        """Lista os modelos disponiveis no provedor."""
        ...

    async def health(self) -> bool:
        """True quando o provedor responde a uma verificacao barata."""
        ...
