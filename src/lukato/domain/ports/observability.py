"""Porta de observabilidade: traces, spans, generations e scores."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from lukato.domain.types import Json

__all__ = ["MetricsPort", "SpanHandle", "TracerPort"]


class SpanHandle(Protocol):
    """Referencia a uma observacao aberta (trace, span ou generation)."""

    def update(self, **kwargs: Any) -> None:
        """Atualiza atributos da observacao (`output`, `metadata`, `usage`, ...)."""
        ...

    def end(self, **kwargs: Any) -> None:
        """Encerra a observacao, opcionalmente atualizando atributos finais."""
        ...

    @property
    def id(self) -> str | None:
        """Identificador da observacao no backend, quando houver."""
        ...

    @property
    def trace_id(self) -> str | None:
        """Identificador do trace ao qual a observacao pertence, quando houver."""
        ...


@runtime_checkable
class TracerPort(Protocol):
    """Contrato do tracer da aplicacao (Langfuse ou implementacao no-op).

    `trace`, `span` e `generation` sao metodos sincronos que devolvem um
    *context manager assincrono*; nos adaptadores use `@asynccontextmanager`.
    """

    @property
    def enabled(self) -> bool:
        """True quando o tracer envia dados a um backend real."""
        ...

    def trace(
        self,
        name: str,
        *,
        input: Json | None = None,
        metadata: Json | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> AbstractAsyncContextManager[SpanHandle]:
        """Abre um trace raiz que agrupa toda a execucao."""
        ...

    def span(
        self,
        name: str,
        *,
        kind: str = "span",
        input: Json | None = None,
        metadata: Json | None = None,
    ) -> AbstractAsyncContextManager[SpanHandle]:
        """Abre uma observacao filha do contexto atual (`span`, `tool`, `retriever`, ...)."""
        ...

    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Json | None = None,
        metadata: Json | None = None,
    ) -> AbstractAsyncContextManager[SpanHandle]:
        """Abre uma observacao de chamada de modelo, com o modelo declarado."""
        ...

    async def score(
        self,
        *,
        name: str,
        value: float,
        trace_id: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Registra uma avaliacao numerica no trace informado (ou no trace atual)."""
        ...

    async def flush(self) -> None:
        """Descarrega no backend tudo o que ainda estiver em buffer."""
        ...

    def current_trace_id(self) -> str | None:
        """Identificador do trace ativo no contexto corrente, se houver."""
        ...


@runtime_checkable
class MetricsPort(Protocol):
    """Contadores de negocio da SPEC-0008 secao 4.

    Separada de `TracerPort` de proposito: trace responde "o que aconteceu nesta
    requisicao", metrica responde "quanto disso aconteceu no total". As tres
    primeiras sao alimentadas de dentro do `InvokeModule` — sem isso, seis das
    nove metricas da SPEC ficariam declaradas e permanentemente vazias.
    """

    def observe_module(self, module: str, runtime: str, status: str, duration: float) -> None:
        """Uma invocacao de building block e sua latencia."""
        ...

    def observe_llm(
        self, model: str, module: str, usage: Any = None, cost: float | None = None
    ) -> None:
        """Tokens e custo de uma chamada de LLM."""
        ...

    def observe_guardrail(
        self,
        stage: str,
        kind: str,
        action: str,
        blocked: bool = False,
        policy: str | None = None,
    ) -> None:
        """Um achado de guardrail e, quando houve, o bloqueio."""
        ...

    def observe_provider_error(self, provider: str, code: str | int) -> None:
        """Um erro devolvido por provedor externo."""
        ...
