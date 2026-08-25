"""Adaptadores de embeddings do lukato: hub Qwen3 e hashing deterministico offline.

Importar este pacote nao abre conexao nem exige credencial — a escolha entre o hub e
o adaptador deterministico e feita por `build_embedder` a partir de `Settings`.
"""

from __future__ import annotations

from lukato.adapters.embeddings.factory import (
    HASHING_REASONS,
    build_embedder,
    build_embedder_with_health,
)
from lukato.adapters.embeddings.hashing import (
    DEFAULT_DIMENSIONS,
    HASHING_MODEL,
    NGRAM_SIZE,
    NGRAM_WEIGHT,
    TOKEN_WEIGHT,
    HashingEmbedder,
)
from lukato.adapters.embeddings.qwen import (
    DEFAULT_TIMEOUT_SECONDS,
    EMBEDDINGS_PATH,
    HEALTH_TIMEOUT_SECONDS,
    MAX_BODY_CHARS,
    RETRY_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
    QwenEmbedder,
)

__all__ = [
    "DEFAULT_DIMENSIONS",
    "DEFAULT_TIMEOUT_SECONDS",
    "EMBEDDINGS_PATH",
    "HASHING_MODEL",
    "HASHING_REASONS",
    "HEALTH_TIMEOUT_SECONDS",
    "MAX_BODY_CHARS",
    "NGRAM_SIZE",
    "NGRAM_WEIGHT",
    "RETRY_ATTEMPTS",
    "RETRY_WAIT_MAX",
    "RETRY_WAIT_MIN",
    "TOKEN_WEIGHT",
    "HashingEmbedder",
    "QwenEmbedder",
    "build_embedder",
    "build_embedder_with_health",
]
