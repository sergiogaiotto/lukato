"""Adaptadores de LLM do lukato: hub compativel com OpenAI e eco offline.

Importar este pacote nao abre conexao nem exige credencial — a escolha entre o hub
e o adaptador deterministico e feita por `build_llm` a partir de `Settings`.
"""

from __future__ import annotations

from lukato.adapters.llm.echo import (
    CHARS_PER_TOKEN,
    DEFAULT_ECHO_MODEL,
    ECHO_PREFIX,
    ECHO_STREAM_CHUNK,
    JSON_MARKER,
    EchoLLM,
    estimate_usage,
)
from lukato.adapters.llm.factory import ECHO_REASONS, build_llm, build_llm_with_health
from lukato.adapters.llm.openai_compatible import (
    HEALTH_TIMEOUT_SECONDS,
    MAX_BODY_CHARS,
    PLACEHOLDER_API_KEY,
    RETRY_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
    OpenAICompatibleLLM,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_ECHO_MODEL",
    "ECHO_PREFIX",
    "ECHO_REASONS",
    "ECHO_STREAM_CHUNK",
    "HEALTH_TIMEOUT_SECONDS",
    "JSON_MARKER",
    "MAX_BODY_CHARS",
    "PLACEHOLDER_API_KEY",
    "RETRY_ATTEMPTS",
    "RETRY_WAIT_MAX",
    "RETRY_WAIT_MIN",
    "EchoLLM",
    "OpenAICompatibleLLM",
    "build_llm",
    "build_llm_with_health",
    "estimate_usage",
]
