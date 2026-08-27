"""Adaptador de embeddings do hub Qwen3 (SPEC-0007 secao 1).

Fala com um endpoint compativel com OpenAI via `httpx`:

```text
POST {base_url}/embeddings
{"model": "Qwen/Qwen3-Embedding-0.6B", "input": ["texto", ...]}
```

`settings.embedding.base_url` **nao** inclui `/embeddings` — o sufixo e concatenado
aqui. Os textos vao em lotes de `settings.embedding.batch_size`, cada lote com tres
tentativas e backoff exponencial; so falhas transitorias (rede, 429, 5xx) repetem,
porque repetir um 4xx de contrato apenas queima tempo e cota.

A dimensao de cada vetor e conferida contra `settings.embedding.dimensions`. Uma
divergencia vira `ValidationError` em vez de gravacao: misturar dimensoes na mesma
colecao corrompe a busca semantica, e voltar atras exige re-embeddar tudo.

Importar este modulo nao abre conexao; `health()` nunca levanta.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any, ClassVar, Final, TypeVar, cast

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lukato.config import Settings, get_logger
from lukato.domain.errors import LukatoError, ProviderError, RateLimitedError, ValidationError

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EMBEDDINGS_PATH",
    "HEALTH_TIMEOUT_SECONDS",
    "MAX_BODY_CHARS",
    "RETRY_ATTEMPTS",
    "RETRY_WAIT_MAX",
    "RETRY_WAIT_MIN",
    "QwenEmbedder",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

EMBEDDINGS_PATH: Final[str] = "/embeddings"
"""Sufixo concatenado a `base_url` (que, por contrato, nao o inclui)."""

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
"""Timeout de cada requisicao de lote."""

HEALTH_TIMEOUT_SECONDS: Final[float] = 5.0
"""Timeout curto do `health()`: readiness nunca segura o boot."""

RETRY_ATTEMPTS: Final[int] = 3
"""Tentativas totais (a primeira mais duas repeticoes) por lote."""

RETRY_WAIT_MIN: Final[float] = 1.0
"""Espera minima, em segundos, do backoff exponencial."""

RETRY_WAIT_MAX: Final[float] = 8.0
"""Espera maxima, em segundos, do backoff exponencial."""

MAX_BODY_CHARS: Final[int] = 2000
"""Corte do corpo de erro copiado para `details`."""

_TOO_MANY_REQUESTS: Final[int] = 429
_SERVER_ERROR_FLOOR: Final[int] = 500
_HEALTH_PROBE: Final[str] = "ping"


class _TransientHubError(Exception):
    """Falha temporaria do hub de embeddings (429 ou 5xx), elegivel a retentativa."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        body: str,
        rate_limited: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.rate_limited = rate_limited

    def as_domain_error(self, *, action: str) -> LukatoError:
        """Converte a falha transitoria esgotada em erro de dominio."""
        details = {"status": self.status, "body": self.body, "action": action}
        if self.rate_limited:
            return RateLimitedError(self.message, details=details)
        return ProviderError(self.message, details=details)


def _log_retry(state: RetryCallState) -> None:
    """Registra cada repeticao com o tipo do erro que a motivou."""
    error = state.outcome.exception() if state.outcome is not None else None
    _logger.warning(
        "embedding_call_retry",
        attempt=state.attempt_number,
        max_attempts=RETRY_ATTEMPTS,
        error=type(error).__name__ if error is not None else None,
    )


def _retrying() -> AsyncRetrying:
    """Politica de retentativa: 3 tentativas, backoff exponencial de 1s a 8s."""
    return AsyncRetrying(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1.0, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, _TransientHubError)),
        before_sleep=_log_retry,
        reraise=True,
    )


async def _with_retry(action: str, operation: Callable[[], Awaitable[_T]]) -> _T:
    """Executa a chamada com retentativa e traduz qualquer erro em `LukatoError`."""

    async def attempt() -> _T:
        """Envelopa a operacao numa corotina real (tenacity so aguarda `async def`)."""
        return await operation()

    try:
        return cast(_T, await _retrying()(attempt))
    except _TransientHubError as exc:
        raise exc.as_domain_error(action=action) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"falha de rede com o hub de embeddings em {action}: {exc}",
            details={"action": action, "cause": type(exc).__name__},
        ) from exc


def _batched(texts: Sequence[str], size: int) -> list[list[str]]:
    """Divide a entrada em lotes de no maximo `size` itens, preservando a ordem."""
    return [list(texts[start : start + size]) for start in range(0, len(texts), size)]


class QwenEmbedder:
    """Cliente de embeddings para o hub Qwen3 (API compativel com OpenAI)."""

    provider: ClassVar[str] = "qwen"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        embedding = settings.embedding
        self.last_health_error: str | None = None
        self._settings = settings
        self._model = embedding.model
        self._dimensions = embedding.dimensions
        self._batch_size = embedding.batch_size
        self._collection = embedding.collection
        self._endpoint = embedding.base_url.rstrip("/") + EMBEDDINGS_PATH
        self._headers = {"Content-Type": "application/json"}
        if embedding.api_key_value:
            self._headers["Authorization"] = f"Bearer {embedding.api_key_value}"
        self._client = client or httpx.AsyncClient(timeout=request_timeout)

    @property
    def model(self) -> str:
        """Modelo de embedding configurado."""
        return self._model

    @property
    def dimensions(self) -> int:
        """Dimensao esperada dos vetores devolvidos pelo hub."""
        return self._dimensions

    @property
    def endpoint(self) -> str:
        """URL completa de `POST /embeddings` (util em `/readyz` e no console)."""
        return self._endpoint

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Gera um vetor por texto, em lotes, preservando a ordem de entrada."""
        if not texts:
            return []
        vectors: list[list[float]] = []
        for batch in _batched(texts, self._batch_size):
            payload = await _with_retry("embed", partial(self._post, batch))
            vectors.extend(self._parse(payload, expected=len(batch)))
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        """Gera o vetor de um unico texto."""
        vectors = await self.embed([text])
        if not vectors:
            raise ProviderError(
                "o hub de embeddings nao devolveu vetor para o texto informado",
                details={"model": self._model},
            )
        return vectors[0]

    async def health(self) -> bool:
        """Sonda o endpoint com um texto curto; qualquer falha devolve `False`."""
        try:
            payload = await self._post([_HEALTH_PROBE], timeout_seconds=HEALTH_TIMEOUT_SECONDS)
            vectors = _extract_vectors(payload)
        except Exception as exc:
            # Motivo preservado para o relatorio de saude — ver o comentario em
            # `OpenAICompatibleLLM.health`: e o que distingue "sem credencial"
            # de "hub fora do ar" para quem le o `/readyz`.
            self.last_health_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            _logger.info(
                "embedding_health_unavailable",
                endpoint=self._endpoint,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return False
        self.last_health_error = None
        return bool(vectors)

    async def aclose(self) -> None:
        """Fecha o cliente HTTP subjacente (chamado no shutdown da aplicacao)."""
        try:
            await self._client.aclose()
        except Exception as exc:
            _logger.warning("embedding_close_failed", error=type(exc).__name__)

    async def _post(
        self,
        inputs: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Envia um lote e devolve o JSON de resposta, classificando o status HTTP."""
        body = {"model": self._model, "input": list(inputs)}
        options: dict[str, Any] = {"json": body, "headers": self._headers}
        if timeout_seconds is not None:
            options["timeout"] = timeout_seconds
        response = await self._client.post(self._endpoint, **options)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "o hub de embeddings devolveu um corpo que nao e JSON",
                details={"status": response.status_code, "body": response.text[:MAX_BODY_CHARS]},
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "o hub de embeddings devolveu um JSON que nao e um objeto",
                details={"status": response.status_code, "type": type(payload).__name__},
            )
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Classifica o status: transitorio (repete) ou definitivo (falha na hora)."""
        status = response.status_code
        if status < httpx.codes.BAD_REQUEST:
            return
        snippet = response.text[:MAX_BODY_CHARS]
        if status == _TOO_MANY_REQUESTS:
            raise _TransientHubError(
                "o hub de embeddings aplicou limite de taxa",
                status=status,
                body=snippet,
                rate_limited=True,
            )
        if status >= _SERVER_ERROR_FLOOR:
            raise _TransientHubError(
                f"o hub de embeddings respondeu {status}",
                status=status,
                body=snippet,
            )
        raise ProviderError(
            f"o hub de embeddings recusou a requisicao com {status}",
            details={"status": status, "body": snippet, "endpoint": self._endpoint},
        )

    def _parse(self, payload: dict[str, Any], *, expected: int) -> list[list[float]]:
        """Extrai os vetores do corpo, confere a quantidade e confere a dimensao."""
        vectors = _extract_vectors(payload)
        if len(vectors) != expected:
            raise ProviderError(
                "o hub de embeddings devolveu uma quantidade de vetores diferente da enviada",
                details={"expected": expected, "received": len(vectors), "model": self._model},
            )
        for position, vector in enumerate(vectors):
            if len(vector) != self._dimensions:
                raise ValidationError(
                    f"o modelo {self._model!r} devolveu um vetor de {len(vector)} dimensoes, "
                    f"mas a colecao {self._collection!r} esta configurada para "
                    f"{self._dimensions}; gravar assim corromperia a busca semantica. "
                    "Alinhe LUKATO_EMBEDDING__DIMENSIONS ao modelo e re-embedde a colecao "
                    "inteira antes de voltar a indexar.",
                    details={
                        "expected_dimensions": self._dimensions,
                        "received_dimensions": len(vector),
                        "collection": self._collection,
                        "model": self._model,
                        "position": position,
                    },
                )
        return vectors


def _extract_vectors(payload: dict[str, Any]) -> list[list[float]]:
    """Le `data[].embedding` (ordenado por `index`) ou, alternativamente, `embeddings`."""
    data = payload.get("data")
    if isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
        if all(isinstance(item.get("index"), int) for item in items):
            items.sort(key=lambda item: int(item["index"]))
        return [_as_floats(item.get("embedding")) for item in items]
    fallback = payload.get("embeddings")
    if isinstance(fallback, list):
        return [_as_floats(item) for item in fallback]
    raise ProviderError(
        "resposta do hub de embeddings sem `data` nem `embeddings`",
        details={"keys": sorted(str(key) for key in payload)[:20]},
    )


def _as_floats(value: object) -> list[float]:
    """Converte a lista crua em `list[float]`, recusando formatos inesperados."""
    if not isinstance(value, list):
        raise ProviderError(
            "o hub de embeddings devolveu um vetor que nao e uma lista",
            details={"type": type(value).__name__},
        )
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            "o hub de embeddings devolveu um vetor com valores nao numericos",
            details={"cause": type(exc).__name__},
        ) from exc
