"""Metricas Prometheus do lukato (SPEC-0008 secao 4).

As nove metricas normativas vivem em um `CollectorRegistry` **proprio**, nunca no
registro global do `prometheus_client`. Isso e deliberado: o registro global e um
singleton de processo e registrar a mesma metrica duas vezes levanta
`ValueError: Duplicated timeseries`, o que quebraria qualquer teste que monte a
aplicacao mais de uma vez. Com registro proprio, cada `Metrics()` e independente e
`reset_metrics()` devolve o processo ao estado inicial.

Cardinalidade e a outra preocupacao central. O label `path` guarda o **template** da
rota (`/api/v1/modules/{slug}`), jamais o valor concreto: um label por identificador
transformaria o Prometheus em um banco de series infinitas. `normalize_path` e a rede
de seguranca para quando o chamador esquecer disso.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client import generate_latest as _generate_latest

__all__ = [
    "HTTP_DURATION_BUCKETS",
    "MAX_LABEL_LENGTH",
    "METRIC_NAMES",
    "MODULE_LATENCY_BUCKETS",
    "UNKNOWN_LABEL",
    "Metrics",
    "get_metrics",
    "normalize_path",
    "reset_metrics",
]

UNKNOWN_LABEL: Final[str] = "unknown"
"""Valor usado quando um label chega vazio ou nulo (label vazio some dos graficos)."""

MAX_LABEL_LENGTH: Final[int] = 120
"""Corte defensivo no tamanho de um label, para conter cardinalidade e memoria."""

HTTP_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    float("inf"),
)
"""Faixas de latencia HTTP: resolucao fina no milissegundo, teto em 10s."""

MODULE_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    float("inf"),
)
"""Faixas de latencia de modulo: uma invocacao de agente leva segundos, nao milissegundos."""

METRIC_NAMES: Final[tuple[str, ...]] = (
    "lukato_http_requests_total",
    "lukato_http_request_duration_seconds",
    "lukato_module_invocations_total",
    "lukato_module_latency_seconds",
    "lukato_llm_tokens_total",
    "lukato_llm_cost_usd_total",
    "lukato_guardrail_findings_total",
    "lukato_guardrail_blocks_total",
    "lukato_provider_errors_total",
)
"""As nove metricas normativas da SPEC-0008, na ordem da especificacao."""

_UUID_SEGMENT: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_NUMERIC_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^\d+$")
_OPAQUE_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)

_ID_PLACEHOLDER: Final[str] = "{id}"


def normalize_path(path: str) -> str:
    """Reduz um caminho a um template estavel, trocando identificadores por `{id}`.

    A regra da SPEC-0008 e que o chamador ja passe o template da rota. Esta funcao
    existe como rede de seguranca: se um UUID, um numero ou um hash longo escapar
    para o label, ele vira `{id}` em vez de criar uma serie nova no Prometheus.
    """
    if not path:
        return "/"
    segments = [
        _ID_PLACEHOLDER
        if (
            _UUID_SEGMENT.match(segment)
            or _NUMERIC_SEGMENT.match(segment)
            or _OPAQUE_SEGMENT.match(segment)
        )
        else segment
        for segment in path.split("/")
    ]
    return "/".join(segments)[:MAX_LABEL_LENGTH]


def _label(value: object) -> str:
    """Converte qualquer valor em um label textual seguro e limitado."""
    if value is None:
        return UNKNOWN_LABEL
    text = str(value).strip()
    return text[:MAX_LABEL_LENGTH] if text else UNKNOWN_LABEL


def _non_negative(value: float | int | None) -> float:
    """Zera valores ausentes ou negativos: contador nunca aceita incremento negativo."""
    if value is None:
        return 0.0
    numeric = float(value)
    return numeric if numeric > 0.0 else 0.0


def _split_usage(usage: Any) -> tuple[float, float]:
    """Extrai (prompt, completion) de um `TokenUsage`, de um dicionario ou de nada.

    Aceita o modelo de dominio (`prompt_tokens`/`completion_tokens`) e tambem o
    formato bruto de provedores compativeis com OpenAI, sem importar o dominio aqui.
    """
    if usage is None:
        return 0.0, 0.0
    if isinstance(usage, Mapping):
        prompt = usage.get("prompt_tokens", usage.get("input", 0))
        completion = usage.get("completion_tokens", usage.get("output", 0))
    else:
        prompt = getattr(usage, "prompt_tokens", 0)
        completion = getattr(usage, "completion_tokens", 0)
    return _non_negative(prompt), _non_negative(completion)


class Metrics:
    """As nove metricas da SPEC-0008 em um registro isolado, com atalhos de escrita."""

    __slots__ = (
        "guardrail_blocks_total",
        "guardrail_findings_total",
        "http_request_duration_seconds",
        "http_requests_total",
        "llm_cost_usd_total",
        "llm_tokens_total",
        "module_invocations_total",
        "module_latency_seconds",
        "provider_errors_total",
        "registry",
    )

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry: CollectorRegistry = registry if registry is not None else CollectorRegistry()
        self.http_requests_total = Counter(
            "lukato_http_requests_total",
            "Requisicoes HTTP atendidas, por metodo, template de rota e status.",
            ("method", "path", "status"),
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "lukato_http_request_duration_seconds",
            "Duracao das requisicoes HTTP em segundos.",
            ("method", "path"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.module_invocations_total = Counter(
            "lukato_module_invocations_total",
            "Invocacoes de building blocks, por modulo e status final.",
            ("module", "status"),
            registry=self.registry,
        )
        self.module_latency_seconds = Histogram(
            "lukato_module_latency_seconds",
            "Latencia ponta a ponta da invocacao de um modulo, em segundos.",
            ("module", "runtime"),
            buckets=MODULE_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.llm_tokens_total = Counter(
            "lukato_llm_tokens_total",
            "Tokens consumidos no LLM, por modelo e tipo (prompt/completion).",
            ("model", "kind"),
            registry=self.registry,
        )
        self.llm_cost_usd_total = Counter(
            "lukato_llm_cost_usd_total",
            "Custo acumulado em USD das chamadas de LLM, por modelo e modulo.",
            ("model", "module"),
            registry=self.registry,
        )
        self.guardrail_findings_total = Counter(
            "lukato_guardrail_findings_total",
            "Achados de guardrail, por estagio, tipo de regra e acao aplicada.",
            ("stage", "kind", "action"),
            registry=self.registry,
        )
        self.guardrail_blocks_total = Counter(
            "lukato_guardrail_blocks_total",
            "Bloqueios efetivos de guardrail, por estagio e politica.",
            ("stage", "policy"),
            registry=self.registry,
        )
        self.provider_errors_total = Counter(
            "lukato_provider_errors_total",
            "Erros devolvidos por provedores externos, por provedor e codigo.",
            ("provider", "code"),
            registry=self.registry,
        )

    # ----------------------------------------------------------------- escrita

    def observe_http(self, method: str, path: str, status: int | str, duration: float) -> None:
        """Registra uma requisicao HTTP; `path` deve ser o template da rota."""
        method_label = _label(method).upper()
        path_label = normalize_path(_label(path))
        self.http_requests_total.labels(
            method=method_label, path=path_label, status=_label(status)
        ).inc()
        self.http_request_duration_seconds.labels(method=method_label, path=path_label).observe(
            _non_negative(duration)
        )

    def observe_module(self, module: str, runtime: str, status: str, duration: float) -> None:
        """Registra a invocacao de um building block e sua latencia."""
        module_label = _label(module)
        self.module_invocations_total.labels(module=module_label, status=_label(status)).inc()
        self.module_latency_seconds.labels(module=module_label, runtime=_label(runtime)).observe(
            _non_negative(duration)
        )

    def observe_llm(
        self, model: str, module: str, usage: Any = None, cost: float | None = None
    ) -> None:
        """Registra tokens e custo de uma chamada de LLM.

        `usage` aceita `TokenUsage`, dicionario do provedor ou `None`; os contadores
        sao sempre tocados (mesmo com zero) para que a serie exista apos a primeira
        invocacao, como exige o criterio de aceite 3 da SPEC-0008.
        """
        model_label = _label(model)
        prompt_tokens, completion_tokens = _split_usage(usage)
        self.llm_tokens_total.labels(model=model_label, kind="prompt").inc(prompt_tokens)
        self.llm_tokens_total.labels(model=model_label, kind="completion").inc(completion_tokens)
        self.llm_cost_usd_total.labels(model=model_label, module=_label(module)).inc(
            _non_negative(cost)
        )

    def observe_guardrail(
        self,
        stage: str,
        kind: str,
        action: str,
        blocked: bool = False,
        policy: str | None = None,
    ) -> None:
        """Registra um achado de guardrail e, quando houve bloqueio, tambem o bloqueio."""
        stage_label = _label(stage)
        self.guardrail_findings_total.labels(
            stage=stage_label, kind=_label(kind), action=_label(action)
        ).inc()
        if blocked:
            self.guardrail_blocks_total.labels(stage=stage_label, policy=_label(policy)).inc()

    def observe_provider_error(self, provider: str, code: str | int) -> None:
        """Registra um erro devolvido por um provedor externo (LLM, embeddings, tracer)."""
        self.provider_errors_total.labels(provider=_label(provider), code=_label(code)).inc()

    # ----------------------------------------------------------------- leitura

    def render(self) -> tuple[bytes, str]:
        """Serializa o registro no formato de exposicao do Prometheus.

        Devolve `(corpo, content_type)` pronto para virar a resposta de `/metrics`.
        """
        return _generate_latest(self.registry), CONTENT_TYPE_LATEST


@lru_cache(maxsize=1)
def get_metrics() -> Metrics:
    """Devolve a instancia unica de `Metrics` do processo (memoizada)."""
    return Metrics()


def reset_metrics() -> None:
    """Descarta o singleton de metricas, zerando os contadores (usado em testes)."""
    get_metrics.cache_clear()
