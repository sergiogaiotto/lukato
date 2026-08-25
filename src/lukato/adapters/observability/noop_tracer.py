"""Tracer sem efeito: implementacao padrao de `TracerPort` (SPEC-0008 secao 3).

O codigo de negocio nunca pergunta se existe tracer — ele sempre existe. Quando o
Langfuse esta desligado ou sem credenciais, quem ocupa o lugar e o `NoopTracer`:
mesmos metodos, mesma forma, custo zero e, sobretudo, **nenhum caminho que levante**.

Esta e a rede de seguranca de toda a observabilidade do lukato. O
`LangfuseTracer` degrada para o `NoopSpan` deste modulo sempre que o SDK falha, de
modo que uma falha de telemetria jamais derruba a requisicao de negocio.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from lukato.domain.ports.observability import SpanHandle
from lukato.domain.types import Json

__all__ = ["NOOP_SPAN", "NoopSpan", "NoopTracer"]


class NoopSpan:
    """Observacao inerte: aceita qualquer atualizacao e descarta tudo."""

    __slots__ = ()

    def update(self, **kwargs: Any) -> None:
        """Descarta a atualizacao (assinatura compativel com `SpanHandle`)."""

    def end(self, **kwargs: Any) -> None:
        """Descarta o encerramento (assinatura compativel com `SpanHandle`)."""

    @property
    def id(self) -> str | None:
        """Sempre `None`: nao existe observacao em backend nenhum."""
        return None

    @property
    def trace_id(self) -> str | None:
        """Sempre `None`: nao existe trace em backend nenhum."""
        return None

    def __repr__(self) -> str:
        return "NoopSpan()"


NOOP_SPAN: Final[NoopSpan] = NoopSpan()
"""Instancia unica reaproveitada por todos os context managers inertes.

`NoopSpan` nao guarda estado (`__slots__ = ()`), entao compartilhar uma unica
instancia entre corrotinas concorrentes e seguro e evita alocacao por span.
"""


class NoopTracer:
    """`TracerPort` que nao envia nada a lugar nenhum.

    E o adaptador padrao (SPEC-0008 secao 3) e tambem o modo degradado do
    `LangfuseTracer`. Nenhum metodo faz I/O, nenhum metodo levanta.
    """

    __slots__ = ()

    @property
    def enabled(self) -> bool:
        """Sempre `False`: nao ha backend recebendo os traces."""
        return False

    @asynccontextmanager
    async def trace(
        self,
        name: str,
        *,
        input: Json | None = None,
        metadata: Json | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> AsyncIterator[SpanHandle]:
        """Abre um trace raiz inerte."""
        yield NOOP_SPAN

    @asynccontextmanager
    async def span(
        self,
        name: str,
        *,
        kind: str = "span",
        input: Json | None = None,
        metadata: Json | None = None,
    ) -> AsyncIterator[SpanHandle]:
        """Abre uma observacao filha inerte."""
        yield NOOP_SPAN

    @asynccontextmanager
    async def generation(
        self,
        name: str,
        *,
        model: str,
        input: Json | None = None,
        metadata: Json | None = None,
    ) -> AsyncIterator[SpanHandle]:
        """Abre uma observacao de chamada de modelo inerte."""
        yield NOOP_SPAN

    async def score(
        self,
        *,
        name: str,
        value: float,
        trace_id: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Descarta a avaliacao."""

    async def flush(self) -> None:
        """Nao ha buffer para descarregar."""

    def current_trace_id(self) -> str | None:
        """Sempre `None`: nao existe trace ativo."""
        return None

    def __repr__(self) -> str:
        return "NoopTracer()"
