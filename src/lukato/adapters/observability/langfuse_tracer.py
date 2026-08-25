"""Tracer Langfuse 4 (OpenTelemetry) adaptado a `TracerPort` (SPEC-0008).

Tres decisoes moldam este adaptador:

1. **Telemetria nunca derruba negocio.** Toda chamada ao SDK esta cercada por
   `try/except`. Quando algo falha, o span degrada para `NoopSpan` e a requisicao
   segue como se nada tivesse acontecido (SPEC-0008 secao 3 e criterio de aceite 4).
2. **Os context managers do Langfuse 4 sao sincronos.** A porta e assincrona, entao
   cada observacao e aberta dentro de um `contextlib.ExitStack` e o metodo publico e
   embrulhado com `@asynccontextmanager` — o padrao recomendado em `LIBRARY-NOTES`.
3. **Nada de API v2.** Nao existem `client.trace()` nem `trace.span()`; tudo passa por
   `start_as_current_observation(as_type=...)`, e os atributos de trace (`user_id`,
   `session_id`, `tags`) por `propagate_attributes`.

`flush()`, `health()` e `aclose()` chamam metodos sincronos e bloqueantes do SDK: vao
para uma thread *daemon* com timeout, porque um backend inalcancavel (o caso normal
neste ambiente, sem rede) nao pode segurar o event loop. A thread e daemon de
proposito, e nao um `ThreadPoolExecutor`: `Langfuse.flush()` termina em
`queue.join()`, uma espera sem timeout que trava para sempre quando o consumidor da
fila ja morreu, e o `concurrent.futures` faz *join* de todas as threads do pool no
encerramento do interpretador — bastaria uma chamada presa para pendurar o processo
inteiro no shutdown. Thread daemon nao e aguardada, entao o processo sempre sai.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager
from typing import Any, Final

from langfuse import Langfuse, propagate_attributes

from lukato.adapters.observability.noop_tracer import NOOP_SPAN
from lukato.config import get_logger
from lukato.domain.ports.observability import SpanHandle
from lukato.domain.types import Json

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FLUSH_TIMEOUT_SECONDS",
    "HEALTH_TIMEOUT_SECONDS",
    "MAX_CONSECUTIVE_FAILURES",
    "OBSERVATION_TYPES",
    "LangfuseSpanHandle",
    "LangfuseTracer",
]

_logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[int] = 5
"""Timeout HTTP do cliente Langfuse, em segundos.

Curto de proposito: o exportador roda em background e um backend inalcancavel deve
desistir rapido, nao acumular threads presas em socket.
"""

FLUSH_TIMEOUT_SECONDS: Final[float] = 8.0
"""Teto de espera do `flush()`: o buffer nao vale uma requisicao pendurada.

Fica acima de `DEFAULT_TIMEOUT_SECONDS` para que o SDK conclua a tentativa e reporte
o proprio erro; o teto aqui e a rede de seguranca contra um exportador travado.
"""

HEALTH_TIMEOUT_SECONDS: Final[float] = 5.0
"""Teto de espera do `auth_check()`: readiness nunca segura o boot."""

MAX_CONSECUTIVE_FAILURES: Final[int] = 10
"""Falhas seguidas do SDK apos as quais o tracer se desliga sozinho.

Um backend derrubado geraria uma linha de WARNING por span, inundando o log sem
acrescentar informacao. Depois deste limite o adaptador vira no-op silencioso; uma
unica operacao bem-sucedida zera a contagem.
"""

OBSERVATION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "span",
        "generation",
        "embedding",
        "agent",
        "tool",
        "chain",
        "retriever",
        "evaluator",
        "guardrail",
    }
)
"""Valores aceitos por `as_type` no Langfuse 4.14.5."""

_UPDATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "input",
        "output",
        "metadata",
        "version",
        "level",
        "status_message",
        "completion_start_time",
        "model",
        "model_parameters",
        "usage_details",
        "cost_details",
        "prompt",
    }
)
"""Parametros nomeados aceitos por `LangfuseObservationWrapper.update`."""

_USAGE_KEYS: Final[frozenset[str]] = frozenset({"usage", "usage_details"})
_COST_KEYS: Final[frozenset[str]] = frozenset({"cost", "cost_usd", "cost_details"})

_MAX_DETAIL_CHARS: Final[int] = 200
"""Corte das mensagens de erro registradas em log e em `status_message`."""

_BLOCKING_THREAD_NAME: Final[str] = "lukato-langfuse"
"""Nome das threads daemon que executam as chamadas bloqueantes do SDK."""


def _settle(future: asyncio.Future[Any], result: Any, error: BaseException | None) -> None:
    """Conclui o future no event loop, ignorando-o se ja foi cancelado pelo timeout."""
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


def _usage_details(value: Any) -> dict[str, int]:
    """Normaliza consumo de tokens para o formato `usage_details` do Langfuse."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {
            str(key): int(item)
            for key, item in value.items()
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        }
    prompt = getattr(value, "prompt_tokens", None)
    completion = getattr(value, "completion_tokens", None)
    if prompt is None and completion is None:
        return {}
    total = getattr(value, "total_tokens", None) or (int(prompt or 0) + int(completion or 0))
    return {"input": int(prompt or 0), "output": int(completion or 0), "total": int(total)}


def _cost_details(value: Any) -> dict[str, float]:
    """Normaliza custo para o formato `cost_details` do Langfuse."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {
            str(key): float(item)
            for key, item in value.items()
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"total": float(value)}
    return {}


def _translate_update_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Traduz o vocabulario da porta para o do SDK.

    A porta expoe `update(usage=..., cost=...)`; o Langfuse 4 espera `usage_details` e
    `cost_details`. Qualquer chave desconhecida vai para `metadata` em vez de ser
    repassada crua ao SDK, que a rejeitaria com um aviso.
    """
    translated: dict[str, Any] = {}
    extra: Json = {}
    for key, value in kwargs.items():
        if key in _USAGE_KEYS:
            details = _usage_details(value)
            if details:
                translated["usage_details"] = details
        elif key in _COST_KEYS:
            costs = _cost_details(value)
            if costs:
                translated["cost_details"] = costs
        elif key in _UPDATE_KEYS:
            translated[key] = value
        elif value is not None:
            extra[key] = value
    if extra:
        current = translated.get("metadata")
        translated["metadata"] = {**current, **extra} if isinstance(current, Mapping) else extra
    return translated


class LangfuseSpanHandle:
    """`SpanHandle` sobre uma observacao do Langfuse, blindado contra excecoes."""

    __slots__ = ("_ended", "_observation", "_on_error")

    def __init__(self, observation: Any, on_error: Callable[..., None]) -> None:
        self._observation = observation
        self._on_error = on_error
        self._ended = False

    def update(self, **kwargs: Any) -> None:
        """Atualiza a observacao, traduzindo `usage`/`cost` para o formato do SDK."""
        payload = _translate_update_kwargs(kwargs)
        if not payload:
            return
        try:
            self._observation.update(**payload)
        except Exception as exc:
            self._on_error("langfuse_span_update_failed", exc)

    def end(self, **kwargs: Any) -> None:
        """Aplica os ultimos atributos e encerra a observacao (idempotente)."""
        self.update(**kwargs)
        if self._ended:
            return
        self._ended = True
        try:
            self._observation.end()
        except Exception as exc:
            self._on_error("langfuse_span_end_failed", exc)

    @property
    def id(self) -> str | None:
        """Identificador da observacao no Langfuse, quando disponivel."""
        return self._attribute("id")

    @property
    def trace_id(self) -> str | None:
        """Identificador do trace ao qual a observacao pertence."""
        return self._attribute("trace_id")

    def _attribute(self, name: str) -> str | None:
        """Le um identificador da observacao sem deixar erro escapar."""
        try:
            value = getattr(self._observation, name, None)
        except Exception as exc:
            self._on_error("langfuse_span_attribute_failed", exc, attribute=name)
            return None
        return str(value) if value else None

    def __repr__(self) -> str:
        return f"LangfuseSpanHandle(id={self.id!r}, trace_id={self.trace_id!r})"


class LangfuseTracer:
    """Adaptador de `TracerPort` para o Langfuse 4.

    Construir a instancia nao faz I/O de rede: o cliente so enfileira spans em um
    processador em background. Por isso o adaptador pode ser montado no boot mesmo
    quando o host do Langfuse esta inalcancavel — e o que acontece offline.
    """

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str,
        *,
        environment: str | None = None,
        release: str | None = None,
        enabled: bool = True,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS,
        health_timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._environment = environment
        self._release = release
        self._flush_timeout = flush_timeout_seconds
        self._health_timeout = health_timeout_seconds
        self._failures = 0
        self._client: Langfuse | None = None
        self._enabled = False

        if not enabled:
            _logger.info("langfuse_tracer_off", host=host, reason="desabilitado na configuracao")
            return
        if not public_key or not secret_key:
            _logger.warning(
                "langfuse_tracer_off", host=host, reason="credenciais ausentes ou vazias"
            )
            return
        try:
            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
                environment=environment,
                release=release,
                timeout=timeout_seconds,
            )
        except Exception as exc:
            _logger.warning(
                "langfuse_client_init_failed",
                host=host,
                error=type(exc).__name__,
                detail=str(exc)[:_MAX_DETAIL_CHARS],
            )
            self._client = None
            return
        self._enabled = True
        _logger.info("langfuse_tracer_ready", host=host, environment=environment, release=release)

    # ------------------------------------------------------------------ estado

    @property
    def enabled(self) -> bool:
        """True enquanto houver cliente ativo e nenhuma degradacao acumulada."""
        return self._enabled and self._client is not None

    @property
    def host(self) -> str:
        """Host do Langfuse configurado (usado em logs e no detalhe de `/readyz`)."""
        return self._host

    # ------------------------------------------------------------ observacoes

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
        """Abre o trace raiz e propaga os atributos de trace para os filhos."""
        return self._observation(
            name=name,
            as_type="span",
            attributes={"input": input, "metadata": metadata},
            propagation={
                "trace_name": name,
                "user_id": user_id,
                "session_id": session_id,
                "tags": list(tags) if tags else None,
                "metadata": dict(metadata) if metadata else None,
                "version": self._release,
            },
        )

    def span(
        self,
        name: str,
        *,
        kind: str = "span",
        input: Json | None = None,
        metadata: Json | None = None,
    ) -> AbstractAsyncContextManager[SpanHandle]:
        """Abre uma observacao filha do contexto atual."""
        return self._observation(
            name=name,
            as_type=self._observation_type(kind),
            attributes={"input": input, "metadata": metadata},
        )

    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Json | None = None,
        metadata: Json | None = None,
    ) -> AbstractAsyncContextManager[SpanHandle]:
        """Abre uma observacao de chamada de modelo (`as_type="generation"`)."""
        return self._observation(
            name=name,
            as_type="generation",
            attributes={"input": input, "metadata": metadata, "model": model},
        )

    @asynccontextmanager
    async def _observation(
        self,
        *,
        name: str,
        as_type: str,
        attributes: Json,
        propagation: Json | None = None,
    ) -> AsyncIterator[SpanHandle]:
        """Abre uma observacao sincrona do SDK dentro de um `ExitStack` assincrono."""
        stack = ExitStack()
        handle = self._open(stack, name=name, as_type=as_type, attributes=attributes)
        if propagation is not None and handle is not NOOP_SPAN:
            self._propagate(stack, propagation)
        try:
            yield handle
        except BaseException as exc:
            handle.update(
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}"[:_MAX_DETAIL_CHARS],
            )
            self._close(stack, exc)
            raise
        self._close(stack, None)

    def _open(self, stack: ExitStack, *, name: str, as_type: str, attributes: Json) -> SpanHandle:
        """Entra no context manager sincrono da observacao; em falha devolve `NoopSpan`."""
        client = self._client
        if client is None or not self._enabled:
            return NOOP_SPAN
        payload: dict[str, Any] = {"name": name, "as_type": as_type}
        payload.update({key: value for key, value in attributes.items() if value is not None})
        try:
            observation = stack.enter_context(client.start_as_current_observation(**payload))
        except Exception as exc:
            self._telemetry_failed("langfuse_observation_start_failed", exc, observation=name)
            return NOOP_SPAN
        self._telemetry_succeeded()
        return LangfuseSpanHandle(observation, self._telemetry_failed)

    def _propagate(self, stack: ExitStack, attributes: Json) -> None:
        """Aplica atributos de trace (`user_id`, `session_id`, `tags`) ao contexto."""
        payload = {key: value for key, value in attributes.items() if value is not None}
        if not payload:
            return
        try:
            stack.enter_context(propagate_attributes(**payload))
        except Exception as exc:
            self._telemetry_failed("langfuse_propagate_attributes_failed", exc)

    def _close(self, stack: ExitStack, exc: BaseException | None) -> None:
        """Fecha o `ExitStack`, repassando a excecao do corpo para o SDK marcar o span."""
        try:
            if exc is None:
                stack.close()
            else:
                stack.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as close_error:
            self._telemetry_failed("langfuse_observation_close_failed", close_error)

    def _observation_type(self, kind: str) -> str:
        """Valida `kind` contra os tipos do SDK, caindo para `span` quando desconhecido."""
        if kind in OBSERVATION_TYPES:
            return kind
        _logger.debug("langfuse_observation_type_unknown", kind=kind, fallback="span")
        return "span"

    # ------------------------------------------------------------------ scores

    async def score(
        self,
        *,
        name: str,
        value: float,
        trace_id: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Grava um score numerico no trace informado (ou no trace ativo).

        `create_score` apenas enfileira o evento no processador em background, entao
        roda inline: mandar para o executor custaria mais que a propria chamada.
        """
        client = self._client
        if client is None or not self._enabled:
            return
        try:
            client.create_score(
                name=name,
                value=float(value),
                trace_id=trace_id,
                comment=comment,
                data_type="NUMERIC",
            )
        except Exception as exc:
            self._telemetry_failed("langfuse_score_failed", exc, score=name)
            return
        self._telemetry_succeeded()

    def current_trace_id(self) -> str | None:
        """Devolve o `trace_id` ativo no contexto OpenTelemetry corrente."""
        client = self._client
        if client is None or not self._enabled:
            return None
        try:
            trace_id = client.get_current_trace_id()
        except Exception as exc:
            self._telemetry_failed("langfuse_current_trace_id_failed", exc)
            return None
        return str(trace_id) if trace_id else None

    # ------------------------------------------------------- I/O bloqueante

    async def flush(self) -> None:
        """Descarrega o buffer do SDK em um executor, com teto de espera."""
        client = self._client
        if client is None:
            return
        await self._run_blocking(client.flush, "langfuse_flush_failed", self._flush_timeout)

    async def health(self) -> bool:
        """Verifica as credenciais com `auth_check()`; qualquer falha vira `False`."""
        client = self._client
        if client is None or not self._enabled:
            return False
        result = await self._run_blocking(
            client.auth_check, "langfuse_auth_check_failed", self._health_timeout
        )
        return bool(result)

    async def aclose(self) -> None:
        """Encerra o cliente do Langfuse; seguro de chamar mais de uma vez."""
        client = self._client
        self._client = None
        self._enabled = False
        if client is not None:
            await self._run_blocking(
                client.shutdown, "langfuse_shutdown_failed", self._flush_timeout
            )

    async def _run_blocking(
        self, operation: Callable[[], Any], event: str, limit_seconds: float
    ) -> Any:
        """Roda uma chamada sincrona do SDK em thread daemon, com timeout e sem levantar.

        No estouro do prazo a corrotina segue em frente e a thread e abandonada: ela e
        daemon, entao no maximo desperdica um socket ate o processo terminar — bem
        melhor que prender o event loop ou o encerramento da aplicacao.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def runner() -> None:
            error: BaseException | None = None
            result: Any = None
            try:
                result = operation()
            except BaseException as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(_settle, future, result, error)
            except RuntimeError:
                _logger.debug("langfuse_result_discarded", operation=event)

        threading.Thread(target=runner, name=_BLOCKING_THREAD_NAME, daemon=True).start()
        try:
            value = await asyncio.wait_for(future, limit_seconds)
        except TimeoutError as exc:
            self._telemetry_failed(event, exc, limit_seconds=limit_seconds)
            return None
        except Exception as exc:
            self._telemetry_failed(event, exc)
            return None
        self._telemetry_succeeded()
        return value

    # ------------------------------------------------------------- degradacao

    def _telemetry_failed(self, event: str, exc: BaseException, **fields: Any) -> None:
        """Registra a falha do SDK e desliga o tracer se elas se acumularem."""
        self._failures += 1
        _logger.warning(
            event,
            host=self._host,
            error=type(exc).__name__,
            detail=str(exc)[:_MAX_DETAIL_CHARS],
            consecutive_failures=self._failures,
            **fields,
        )
        if self._enabled and self._failures >= MAX_CONSECUTIVE_FAILURES:
            self._enabled = False
            _logger.warning(
                "langfuse_tracer_degraded",
                host=self._host,
                failures=self._failures,
                reason="falhas consecutivas do SDK; seguindo sem telemetria",
            )

    def _telemetry_succeeded(self) -> None:
        """Zera a contagem de falhas apos qualquer operacao bem-sucedida."""
        self._failures = 0

    def __repr__(self) -> str:
        return f"LangfuseTracer(host={self._host!r}, enabled={self.enabled})"
