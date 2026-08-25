"""Observabilidade do lukato: tracing (Langfuse ou no-op) e metricas Prometheus.

Importar este pacote nao abre conexao, nao exige credencial e nao toca a rede: a
escolha entre o `LangfuseTracer` e o `NoopTracer` acontece em `build_tracer`, a partir
de `Settings`, e as metricas vivem em um `CollectorRegistry` proprio.
"""

from __future__ import annotations

from lukato.adapters.observability.factory import (
    NOOP_REASONS,
    build_tracer,
    build_tracer_with_health,
)
from lukato.adapters.observability.langfuse_tracer import (
    DEFAULT_TIMEOUT_SECONDS,
    FLUSH_TIMEOUT_SECONDS,
    HEALTH_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    OBSERVATION_TYPES,
    LangfuseSpanHandle,
    LangfuseTracer,
)
from lukato.adapters.observability.metrics import (
    HTTP_DURATION_BUCKETS,
    MAX_LABEL_LENGTH,
    METRIC_NAMES,
    MODULE_LATENCY_BUCKETS,
    UNKNOWN_LABEL,
    Metrics,
    get_metrics,
    normalize_path,
    reset_metrics,
)
from lukato.adapters.observability.noop_tracer import NOOP_SPAN, NoopSpan, NoopTracer

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FLUSH_TIMEOUT_SECONDS",
    "HEALTH_TIMEOUT_SECONDS",
    "HTTP_DURATION_BUCKETS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_LABEL_LENGTH",
    "METRIC_NAMES",
    "MODULE_LATENCY_BUCKETS",
    "NOOP_REASONS",
    "NOOP_SPAN",
    "OBSERVATION_TYPES",
    "UNKNOWN_LABEL",
    "LangfuseSpanHandle",
    "LangfuseTracer",
    "Metrics",
    "NoopSpan",
    "NoopTracer",
    "build_tracer",
    "build_tracer_with_health",
    "get_metrics",
    "normalize_path",
    "reset_metrics",
]
