"""Adaptador de LLM para hubs com API compativel com OpenAI (SPEC-0000 secao 7.1).

Fala com `settings.llm.base_url` (por padrao o hub Qwen interno da Claro) usando o
SDK oficial `openai` 3.3.1 em modo assincrono. Tres decisoes valem registro:

* **A retentativa e nossa.** O cliente e criado com `max_retries=0` e a politica de
  repeticao fica em `tenacity`, para que backoff, limite de tentativas e log fiquem
  no mesmo lugar em todos os adaptadores de borda do projeto.
* **So o que e transitorio repete.** `APIConnectionError`, `APITimeoutError` e
  `RateLimitError` sao retentados; qualquer outro `APIStatusError` (tipicamente 4xx
  de contrato) falha na primeira tentativa — repetir um 400 so queima tempo e cota.
* **Nenhum erro de biblioteca escapa.** Tudo vira `LukatoError`: `RateLimitedError`
  para 429 e `ProviderError` (com `details = {"status", "body"}`) para o resto.

`health()` nunca levanta: erra para `False` e deixa o composition root decidir se cai
para o `EchoLLM`. Importar este modulo nao abre conexao alguma.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, ClassVar, Final, TypeVar, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lukato.adapters.llm.echo import estimate_usage
from lukato.config import Settings, get_logger
from lukato.domain.errors import LukatoError, ProviderError, RateLimitedError, ValidationError
from lukato.domain.models.run import TokenUsage
from lukato.domain.ports.llm import ChatMessage, LLMResponse
from lukato.domain.types import Json

__all__ = [
    "HEALTH_TIMEOUT_SECONDS",
    "MAX_BODY_CHARS",
    "PLACEHOLDER_API_KEY",
    "RETRY_ATTEMPTS",
    "RETRY_WAIT_MAX",
    "RETRY_WAIT_MIN",
    "OpenAICompatibleLLM",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

RETRY_ATTEMPTS: Final[int] = 3
"""Tentativas totais (a primeira mais duas repeticoes) por chamada ao hub."""

RETRY_WAIT_MIN: Final[float] = 1.0
"""Espera minima, em segundos, do backoff exponencial."""

RETRY_WAIT_MAX: Final[float] = 8.0
"""Espera maxima, em segundos, do backoff exponencial."""

HEALTH_TIMEOUT_SECONDS: Final[float] = 5.0
"""Teto do `health()`: uma verificacao barata nunca segura o boot por muito tempo."""

MAX_BODY_CHARS: Final[int] = 2000
"""Corte do corpo de erro copiado para `details` (evita log e resposta gigantes)."""

PLACEHOLDER_API_KEY: Final[str] = "lukato-unauthenticated"
"""Valor enviado quando o hub nao exige credencial.

O SDK recusa construir o cliente sem `api_key`, e alguns gateways compativeis rodam
abertos na rede interna. Nao e segredo nem credencial valida em lugar algum: e um
marcador publico que apenas satisfaz a validacao do construtor.
"""

_RETRYABLE_ERRORS: Final[tuple[type[Exception], ...]] = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
)
"""Falhas transitorias elegiveis a retentativa (`APITimeoutError` herda de conexao)."""


def _log_retry(state: RetryCallState) -> None:
    """Registra cada repeticao com o tipo do erro que a motivou."""
    error = state.outcome.exception() if state.outcome is not None else None
    _logger.warning(
        "llm_call_retry",
        attempt=state.attempt_number,
        max_attempts=RETRY_ATTEMPTS,
        error=type(error).__name__ if error is not None else None,
    )


def _retrying() -> AsyncRetrying:
    """Politica de retentativa: 3 tentativas, backoff exponencial de 1s a 8s."""
    return AsyncRetrying(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1.0, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type(_RETRYABLE_ERRORS),
        before_sleep=_log_retry,
        reraise=True,
    )


def _safe_body(body: object) -> Json | str | None:
    """Normaliza o corpo do erro do provedor em algo serializavel e curto."""
    if body is None:
        return None
    if isinstance(body, dict):
        return {str(key): value for key, value in list(body.items())[:20]}
    return str(body)[:MAX_BODY_CHARS]


def _translate_error(exc: OpenAIError, *, action: str) -> LukatoError:
    """Converte um erro do SDK em erro de dominio, preservando status e corpo."""
    if isinstance(exc, RateLimitError):
        return RateLimitedError(
            f"o hub de LLM aplicou limite de taxa em {action}",
            details={"status": exc.status_code, "body": _safe_body(exc.body), "action": action},
        )
    if isinstance(exc, APIStatusError):
        return ProviderError(
            f"o hub de LLM respondeu {exc.status_code} em {action}",
            details={"status": exc.status_code, "body": _safe_body(exc.body), "action": action},
        )
    if isinstance(exc, APITimeoutError):
        return ProviderError(
            f"tempo esgotado ao falar com o hub de LLM em {action}",
            details={"action": action, "cause": type(exc).__name__},
        )
    if isinstance(exc, APIConnectionError):
        return ProviderError(
            f"falha de conexao com o hub de LLM em {action}",
            details={"action": action, "cause": type(exc).__name__},
        )
    return ProviderError(
        f"falha inesperada do hub de LLM em {action}: {exc}",
        details={"action": action, "cause": type(exc).__name__},
    )


async def _with_retry(action: str, operation: Callable[[], Awaitable[_T]]) -> _T:
    """Executa a chamada com retentativa e traduz qualquer erro do SDK."""

    async def attempt() -> _T:
        """Envelopa a operacao numa corotina real (tenacity so aguarda `async def`)."""
        return await operation()

    try:
        return cast(_T, await _retrying()(attempt))
    except OpenAIError as exc:
        raise _translate_error(exc, action=action) from exc


class OpenAICompatibleLLM:
    """Cliente de chat completions para qualquer endpoint compativel com OpenAI."""

    provider: ClassVar[str] = "openai_compatible"

    def __init__(self, settings: Settings, *, client: AsyncOpenAI | None = None) -> None:
        self._settings = settings
        llm = settings.llm
        self._model = llm.model
        self._fallback_model = llm.fallback_model
        self._temperature = llm.temperature
        self._max_tokens = llm.max_tokens
        self._base_url = llm.base_url
        self._health_timeout = min(llm.timeout, HEALTH_TIMEOUT_SECONDS)
        self._client = client or AsyncOpenAI(
            base_url=llm.base_url,
            api_key=llm.api_key_value or PLACEHOLDER_API_KEY,
            timeout=llm.timeout,
            max_retries=0,
        )

    @property
    def default_model(self) -> str:
        """Modelo usado quando a chamada nao informa um explicitamente."""
        return self._model

    @property
    def base_url(self) -> str:
        """Endpoint configurado (util em `/readyz` e no console)."""
        return self._base_url

    @property
    def client(self) -> AsyncOpenAI:
        """Cliente do SDK, exposto para composicao e testes."""
        return self._client

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
        """Executa uma chamada de chat e devolve conteudo, consumo e latencia."""
        payload = self._payload(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            response_format=response_format,
        )
        if metadata:
            _logger.debug("llm_chat_metadata", keys=sorted(str(key) for key in metadata))
        started = time.perf_counter()
        response = await _with_retry(
            "chat", lambda: self._client.chat.completions.create(**payload)
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        return self._to_domain(response, messages=messages, latency_ms=latency_ms)

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """Emite os fragmentos incrementais (`delta.content`) da resposta.

        A retentativa cobre apenas a abertura do fluxo: uma vez que o hub comecou a
        responder, repetir produziria texto duplicado em vez de recuperacao.
        """
        payload = self._payload(
            messages,
            model=kwargs.get("model"),
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens"),
            stop=kwargs.get("stop"),
            response_format=kwargs.get("response_format"),
        )
        payload["stream"] = True
        chunks = await _with_retry(
            "stream", lambda: self._client.chat.completions.create(**payload)
        )
        try:
            async for chunk in chunks:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None)
                if content:
                    yield content
        except OpenAIError as exc:
            raise _translate_error(exc, action="stream") from exc

    async def list_models(self) -> list[str]:
        """Lista os identificadores de modelo publicados pelo hub."""
        page = await _with_retry("list_models", self._client.models.list)
        return [str(model.id) for model in page.data]

    async def health(self) -> bool:
        """Verificacao barata com timeout curto; qualquer falha devolve `False`."""
        try:
            await self._client.models.list(timeout=self._health_timeout)
        except Exception as exc:
            _logger.info(
                "llm_health_unavailable",
                base_url=self._base_url,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return False
        return True

    async def aclose(self) -> None:
        """Fecha o cliente HTTP subjacente (chamado no shutdown da aplicacao)."""
        try:
            await self._client.close()
        except Exception as exc:
            _logger.warning("llm_close_failed", error=type(exc).__name__)

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        stop: Sequence[str] | None,
        response_format: Json | None,
    ) -> dict[str, Any]:
        """Monta o corpo da chamada, aplicando os padroes de `Settings`."""
        if not messages:
            raise ValidationError("a chamada de chat exige ao menos uma mensagem")
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        }
        if stop:
            payload["stop"] = list(stop)
        if response_format:
            payload["response_format"] = dict(response_format)
        return payload

    def _to_domain(
        self,
        response: Any,
        *,
        messages: Sequence[ChatMessage],
        latency_ms: float,
    ) -> LLMResponse:
        """Traduz a resposta do SDK em `LLMResponse`, estimando `usage` se preciso."""
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ProviderError(
                "o hub de LLM devolveu uma resposta sem `choices`",
                details={"model": getattr(response, "model", self._model)},
            )
        choice = choices[0]
        content = getattr(getattr(choice, "message", None), "content", None) or ""
        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        usage, estimated = self._usage(response, messages=messages, content=content)
        raw: Json = {
            "provider": self.provider,
            "id": getattr(response, "id", None),
            "created": getattr(response, "created", None),
            "system_fingerprint": getattr(response, "system_fingerprint", None),
            "finish_reason": finish_reason,
            "usage_estimated": estimated,
        }
        return LLMResponse(
            content=content,
            model=str(getattr(response, "model", None) or self._model),
            usage=usage,
            finish_reason=str(finish_reason),
            raw=raw,
            latency_ms=latency_ms,
        )

    def _usage(
        self,
        response: Any,
        *,
        messages: Sequence[ChatMessage],
        content: str,
    ) -> tuple[TokenUsage, bool]:
        """Le o consumo reportado ou estima por `len(texto) // 4` quando ausente."""
        reported = getattr(response, "usage", None)
        if reported is None:
            prompt_text = "".join(message.content for message in messages)
            return estimate_usage(prompt_text, content), True
        prompt_tokens = int(getattr(reported, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(reported, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(reported, "total_tokens", 0) or 0)
        return (
            TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens or prompt_tokens + completion_tokens,
            ),
            False,
        )
