"""Adaptador de LLM deterministico e 100% offline (ADR-0003, SPEC-0000 secao 14).

`EchoLLM` e o irmao offline de :class:`~lukato.adapters.llm.openai_compatible.OpenAICompatibleLLM`
e o provedor efetivo sempre que nao ha credencial para o hub. Ele nao e um stub: ecoa
a ultima mensagem do usuario com prefixo estavel, respeita `max_tokens` e `stop`,
transmite em fragmentos por `stream()` e sabe responder JSON valido de dois jeitos:

* `response_format={"type": "json_object"}` (ou `json_schema`) -> devolve um objeto
  minimo coerente, derivado do schema quando ele vem junto;
* marca ``[[JSON]]`` na ultima mensagem do usuario -> devolve **exatamente** o texto
  que vier depois dela, sem validar, o que permite exercitar o guardrail de saida por
  schema tanto com JSON valido quanto com JSON quebrado.

Mesma entrada produz sempre a mesma saida: nenhum relogio, nenhum sorteio, nenhuma rede.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar, Final

from lukato.config import Settings, get_logger
from lukato.domain.models.run import TokenUsage
from lukato.domain.ports.llm import ChatMessage, LLMResponse
from lukato.domain.types import Json

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_ECHO_MODEL",
    "ECHO_PREFIX",
    "ECHO_STREAM_CHUNK",
    "JSON_MARKER",
    "EchoLLM",
    "estimate_usage",
]

_logger = get_logger(__name__)

DEFAULT_ECHO_MODEL: Final[str] = "echo"
"""Nome do modelo reportado pelo adaptador offline."""

ECHO_PREFIX: Final[str] = "[echo] "
"""Prefixo estavel aplicado ao texto ecoado (permite asserts exatos em teste)."""

JSON_MARKER: Final[str] = "[[JSON]]"
"""Marca que instrui o eco a devolver literalmente o JSON que vier depois dela."""

ECHO_STREAM_CHUNK: Final[int] = 24
"""Tamanho, em caracteres, de cada fragmento emitido por `stream()`."""

CHARS_PER_TOKEN: Final[int] = 4
"""Divisor da estimativa de tokens usada quando o provedor nao reporta consumo."""

_JSON_FORMATS: Final[frozenset[str]] = frozenset({"json_object", "json_schema"})
"""Valores de `response_format["type"]` que exigem uma resposta JSON."""

_MAX_SCHEMA_DEPTH: Final[int] = 6
"""Profundidade maxima percorrida ao materializar um objeto a partir do schema."""

_SCHEMA_SAMPLES: Final[dict[str, Any]] = {
    "string": "",
    "integer": 0,
    "number": 0.0,
    "boolean": False,
    "array": [],
    "null": None,
}
"""Valor neutro por tipo JSON Schema usado para preencher campos obrigatorios."""


def estimate_usage(prompt_text: str, completion_text: str) -> TokenUsage:
    """Estima o consumo por `len(texto) // 4` quando o provedor nao devolve `usage`.

    Vive aqui, e nao no adaptador de rede, porque este modulo nao depende de nenhuma
    biblioteca externa — o adaptador HTTP importa a estimativa deste lado.
    """
    return TokenUsage.of(
        len(prompt_text) // CHARS_PER_TOKEN,
        len(completion_text) // CHARS_PER_TOKEN,
    )


def _last_user_text(messages: Sequence[ChatMessage]) -> str:
    """Texto da ultima mensagem `user`; na falta dela, o da ultima mensagem qualquer."""
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return messages[-1].content if messages else ""


def _wants_json(response_format: Json | None) -> bool:
    """Indica se o formato pedido exige uma resposta JSON."""
    if not response_format:
        return False
    return str(response_format.get("type", "")).strip() in _JSON_FORMATS


def _extract_schema(response_format: Json | None) -> Json | None:
    """Extrai o JSON Schema de `response_format`, aceitando as duas formas usuais."""
    if not response_format:
        return None
    envelope = response_format.get("json_schema")
    if isinstance(envelope, dict):
        inner = envelope.get("schema")
        if isinstance(inner, dict):
            return inner
        return envelope
    schema = response_format.get("schema")
    return schema if isinstance(schema, dict) else None


def _sample_for(schema: Json, depth: int) -> Any:
    """Materializa um valor neutro que satisfaz o tipo declarado no schema."""
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    if "const" in schema:
        return schema["const"]
    declared = schema.get("type")
    kind = declared[0] if isinstance(declared, list) and declared else declared
    if kind == "object" or (kind is None and "properties" in schema):
        return _object_from_schema(schema, depth + 1)
    if kind == "array":
        items = schema.get("items")
        if depth < _MAX_SCHEMA_DEPTH and isinstance(items, dict):
            return [_sample_for(items, depth + 1)]
        return []
    return _SCHEMA_SAMPLES.get(str(kind), None)


def _object_from_schema(schema: Json, depth: int = 0) -> Json:
    """Constroi um objeto minimo com todas as chaves obrigatorias do schema."""
    properties = schema.get("properties")
    if depth >= _MAX_SCHEMA_DEPTH or not isinstance(properties, dict) or not properties:
        return {}
    required = schema.get("required")
    keys = [str(key) for key in required] if isinstance(required, list) else list(properties)
    result: Json = {}
    for key in keys:
        definition = properties.get(key)
        result[key] = _sample_for(definition, depth) if isinstance(definition, dict) else None
    return result


def _chunks(text: str, size: int) -> list[str]:
    """Fatia o texto em pedacos de tamanho fixo; a concatenacao devolve o original."""
    if not text:
        return []
    return [text[start : start + size] for start in range(0, len(text), size)]


class EchoLLM:
    """Provedor de chat deterministico para desenvolvimento, testes e modo offline."""

    provider: ClassVar[str] = "echo"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str = DEFAULT_ECHO_MODEL,
        prefix: str = ECHO_PREFIX,
    ) -> None:
        self._settings = settings
        self._model = model
        self._prefix = prefix
        self._max_tokens = settings.llm.max_tokens if settings is not None else None

    @property
    def default_model(self) -> str:
        """Modelo reportado quando a chamada nao informa um explicitamente."""
        return self._model

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
        """Responde de forma deterministica, sem tocar a rede nem o relogio."""
        content, mode, finish_reason = self._render(
            messages,
            max_tokens=max_tokens,
            stop=stop,
            response_format=response_format,
        )
        prompt_text = "".join(message.content for message in messages)
        raw: Json = {
            "provider": self.provider,
            "echo_mode": mode,
            "usage_estimated": True,
            "offline": True,
        }
        if mode == "marker":
            raw["json_valid"] = _is_json(content)
        if metadata:
            _logger.debug("echo_chat_metadata", keys=sorted(str(key) for key in metadata))
        return LLMResponse(
            content=content,
            model=model or self._model,
            usage=estimate_usage(prompt_text, content),
            finish_reason=finish_reason,
            raw=raw,
            latency_ms=0.0,
        )

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """Emite a mesma resposta de `chat()` em fragmentos de tamanho fixo."""
        content, _mode, _finish = self._render(
            messages,
            max_tokens=kwargs.get("max_tokens"),
            stop=kwargs.get("stop"),
            response_format=kwargs.get("response_format"),
        )
        for chunk in _chunks(content, ECHO_STREAM_CHUNK):
            yield chunk

    async def list_models(self) -> list[str]:
        """Catalogo do provedor offline: apenas o proprio modelo de eco."""
        return [self._model]

    async def health(self) -> bool:
        """O eco esta sempre saudavel: nao depende de rede nem de credencial."""
        return True

    def _render(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int | None,
        stop: Sequence[str] | None,
        response_format: Json | None,
    ) -> tuple[str, str, str]:
        """Resolve `(conteudo, modo, finish_reason)` da resposta deterministica."""
        source = _last_user_text(messages)
        marker_at = source.find(JSON_MARKER)
        if marker_at >= 0:
            return source[marker_at + len(JSON_MARKER) :].strip(), "marker", "stop"
        if _wants_json(response_format):
            return self._json_answer(source, response_format), "json", "stop"
        content = f"{self._prefix}{source}"
        content, finish_reason = self._truncate(content, max_tokens=max_tokens, stop=stop)
        return content, "echo", finish_reason

    def _json_answer(self, source: str, response_format: Json | None) -> str:
        """Monta um objeto JSON minimo, guiado pelo schema quando ele e informado."""
        payload = _object_from_schema(_extract_schema(response_format) or {})
        if not payload:
            payload = {"echo": source, "model": self._model, "ok": True}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _truncate(
        self,
        content: str,
        *,
        max_tokens: int | None,
        stop: Sequence[str] | None,
    ) -> tuple[str, str]:
        """Aplica `stop` e o teto de tokens, devolvendo o `finish_reason` resultante."""
        for marker in stop or ():
            if marker:
                cut = content.find(marker)
                if cut >= 0:
                    content = content[:cut]
        limit = max_tokens if max_tokens is not None else self._max_tokens
        if limit is not None and limit > 0:
            budget = limit * CHARS_PER_TOKEN
            if len(content) > budget:
                return content[:budget], "length"
        return content, "stop"


def _is_json(text: str) -> bool:
    """Indica se o texto e um documento JSON valido (usado apenas em `raw`)."""
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return False
    return True
