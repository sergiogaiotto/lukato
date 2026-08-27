"""Container da aplicacao: o feixe de portas que os casos de uso recebem pronto.

O `Container` e montado uma unica vez pelo *composition root*
(`lukato.composition`) e injetado em todo caso de uso. Ele so conhece **portas**
(`lukato.domain.ports`), **servicos de dominio** (`lukato.domain.services`) e o
**registry** de building blocks: nenhum adaptador concreto aparece aqui, o que
mantem a regra hexagonal verificavel por import.

Alem de transportar dependencias, o container resolve o runtime de um modulo
(`orchestrator_for`) e responde pela saude agregada da instalacao (`health`),
que alimenta `/readyz` e a barra de status do console.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

from lukato.config import Settings, get_logger
from lukato.domain.errors import UnsupportedCapability
from lukato.domain.ports.embeddings import EmbeddingPort
from lukato.domain.ports.guardrail import GuardrailPort
from lukato.domain.ports.llm import LLMPort
from lukato.domain.ports.media import MediaToolbox
from lukato.domain.ports.misc import CachePort, PasswordHasherPort, TokenServicePort
from lukato.domain.ports.observability import MetricsPort, TracerPort
from lukato.domain.ports.orchestrator import OrchestratorPort
from lukato.domain.ports.unit_of_work import UnitOfWorkFactory
from lukato.domain.ports.vector_store import VectorStorePort
from lukato.domain.services.cost_calculator import CostCalculator
from lukato.domain.services.module_composer import ModuleComposer
from lukato.domain.types import Json
from lukato.modules.registry import ModuleRegistry

__all__ = [
    "DEFAULT_RUNTIME",
    "HEALTH_TIMEOUT_SECONDS",
    "KNOWN_RUNTIMES",
    "STATUS_DEGRADED",
    "STATUS_DOWN",
    "STATUS_OK",
    "Container",
    "ToolCatalog",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")
"""Tipo do valor lido por `Container._safe`."""

DEFAULT_RUNTIME: Final[str] = "langgraph"
"""Runtime usado como rede de seguranca quando o declarado nao esta disponivel."""

KNOWN_RUNTIMES: Final[frozenset[str]] = frozenset({"direct", "langgraph", "deepagent"})
"""Runtimes normativos (SPEC-0004 secao 1); outros so existem se registrados."""

HEALTH_TIMEOUT_SECONDS: Final[float] = 5.0
"""Teto de espera por componente em `health()`: a sonda nunca pode pendurar o `/readyz`."""

STATUS_OK: Final[str] = "ok"
STATUS_DEGRADED: Final[str] = "degraded"
STATUS_DOWN: Final[str] = "down"


@runtime_checkable
class ToolCatalog(Protocol):
    """Superficie minima do registro de ferramentas exigida pela aplicacao.

    Em producao quem ocupa este lugar e o `ToolRegistry` de
    `lukato.adapters.orchestrator.tools`; o protocolo existe para que a camada de
    aplicacao nao precise importar o adaptador (SPEC-0000 secao 2, regra 2).
    """

    def names(self) -> list[str]:
        """Nomes das ferramentas registradas, em ordem alfabetica."""
        ...

    def describe(self, names: Sequence[str] | None = None) -> list[Json]:
        """Contratos das ferramentas pedidas (ou de todas), prontos para o prompt."""
        ...

    async def execute(self, name: str, args: Json | None, ctx: Any) -> Json:
        """Executa a ferramenta pelo nome e devolve o JSON de resultado."""
        ...


def _component(status: str, detail: str) -> dict[str, str]:
    """Monta a entrada de saude de um componente."""
    return {"status": status, "detail": detail}


@dataclass(slots=True)
class Container:
    """Feixe de dependencias da aplicacao (SPEC-0000 secao 10.1)."""

    settings: Settings
    llm: LLMPort
    embeddings: EmbeddingPort
    vector_store: VectorStorePort
    guardrails: GuardrailPort
    tracer: TracerPort
    uow_factory: UnitOfWorkFactory
    orchestrators: dict[str, OrchestratorPort]
    registry: ModuleRegistry
    cost_calculator: CostCalculator
    composer: ModuleComposer
    hasher: PasswordHasherPort
    tokens: TokenServicePort
    media: MediaToolbox = field(default_factory=MediaToolbox)
    tools: ToolCatalog | None = None
    metrics: MetricsPort | None = None
    """Contadores de negocio (SPEC-0008 secao 4); `None` desliga a instrumentacao."""
    cache: CachePort | None = None
    """Cache compartilhado do processo; alimenta o rate limit da borda HTTP.

    Sem ele o `RateLimitMiddleware` cai numa janela local por instancia de
    middleware, e os adaptadores de cache ficam sem uso. `None` e legitimo (o
    middleware degrada para a janela local), mas o composition root preenche.
    """

    # -- runtimes ----------------------------------------------------------
    @property
    def runtimes(self) -> list[str]:
        """Nomes dos orquestradores registrados, em ordem alfabetica."""
        return sorted(self.orchestrators)

    def orchestrator_for(self, runtime: str) -> OrchestratorPort:
        """Resolve o orquestrador do runtime declarado pelo modulo.

        Ordem de resolucao: chave exata que aceite o runtime, depois qualquer
        orquestrador cujo `supports()` responda `True`. Um runtime conhecido cujo
        adaptador esta indisponivel (tipicamente `deepagent` sem `deepagents`
        instalado) degrada para :data:`DEFAULT_RUNTIME` com WARNING; um runtime
        desconhecido levanta :class:`UnsupportedCapability` (SPEC-0004 secao 1).
        """
        requested = (runtime or "").strip().lower() or DEFAULT_RUNTIME

        candidate = self.orchestrators.get(requested)
        if candidate is not None and self._accepts(candidate, requested):
            return candidate
        for orchestrator in self.orchestrators.values():
            if self._accepts(orchestrator, requested):
                return orchestrator

        if requested in KNOWN_RUNTIMES or requested in self.orchestrators:
            fallback = self.orchestrators.get(DEFAULT_RUNTIME)
            if fallback is not None:
                _logger.warning(
                    "orchestrator_fallback",
                    requested=requested,
                    fallback=DEFAULT_RUNTIME,
                    reason="runtime declarado indisponivel nesta instalacao",
                )
                return fallback

        raise UnsupportedCapability(
            f"Runtime '{requested}' nao tem orquestrador disponivel nesta instalacao.",
            details={"runtime": requested, "available": self.runtimes},
        )

    @staticmethod
    def _accepts(orchestrator: OrchestratorPort, runtime: str) -> bool:
        """True quando o orquestrador declara suportar o runtime, sem propagar erro."""
        try:
            return bool(orchestrator.supports(runtime))
        except Exception as exc:  # pragma: no cover - adaptador defeituoso
            _logger.warning(
                "orchestrator_supports_failed",
                orchestrator=getattr(orchestrator, "name", type(orchestrator).__name__),
                runtime=runtime,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    # -- saude -------------------------------------------------------------
    async def health(self) -> dict[str, dict[str, str]]:
        """Saude agregada dos componentes, para `/readyz` (SPEC-0008 secao 5).

        Cada componente reporta `{"status": "ok|degraded|down", "detail": "..."}`.
        Este metodo **nunca** levanta: uma sonda que falha e ela propria um
        sintoma, e o relatorio precisa chegar inteiro ate o endpoint.
        """
        database, llm, embeddings = await asyncio.gather(
            self._check_database(),
            self._check_llm(),
            self._check_embeddings(),
        )
        return {
            "database": database,
            "registry": self._check_registry(),
            "llm": llm,
            "embeddings": embeddings,
            "tracer": self._check_tracer(),
        }

    async def _check_database(self) -> dict[str, str]:
        """Ping barato no banco por uma contagem de definicoes de modulo."""
        try:
            async with (
                asyncio.timeout(HEALTH_TIMEOUT_SECONDS),
                self.uow_factory() as uow,
            ):
                total = await uow.modules.count()
            return _component(STATUS_OK, f"{total} definicao(oes) de modulo")
        except TimeoutError:
            return _component(STATUS_DOWN, f"sem resposta em {HEALTH_TIMEOUT_SECONDS:.0f}s")
        except Exception as exc:
            _logger.warning("health_database_failed", error=f"{type(exc).__name__}: {exc}")
            return _component(STATUS_DOWN, f"{type(exc).__name__}: {exc}")

    async def _check_llm(self) -> dict[str, str]:
        """Consulta `llm.health()`; provedor fora do ar e degradacao, nao queda."""
        model = self._safe(lambda: self.llm.default_model, "desconhecido")
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
                healthy = await self.llm.health()
        except TimeoutError:
            return _component(STATUS_DEGRADED, f"'{model}' sem resposta em tempo habil")
        except Exception as exc:
            _logger.warning("health_llm_failed", error=f"{type(exc).__name__}: {exc}")
            return _component(STATUS_DEGRADED, f"{type(exc).__name__}: {exc}")
        if healthy:
            return _component(STATUS_OK, f"modelo '{model}'")
        # A causa, quando o adaptador a guardou: "indisponivel" seco ja mandou
        # um diagnostico inteiro atras de alias de modelo quando o problema era
        # um 401 de chave ausente.
        motivo = getattr(self.llm, "last_health_error", None)
        detalhe = (
            f"sonda de '{model}' falhou: {motivo}" if motivo else f"modelo '{model}' indisponivel"
        )
        return _component(STATUS_DEGRADED, detalhe)

    async def _check_embeddings(self) -> dict[str, str]:
        """Consulta `embeddings.health()` reportando modelo e dimensao."""
        model = self._safe(lambda: self.embeddings.model, "desconhecido")
        dimensions = self._safe(lambda: self.embeddings.dimensions, 0)
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
                healthy = await self.embeddings.health()
        except TimeoutError:
            return _component(STATUS_DEGRADED, f"'{model}' sem resposta em tempo habil")
        except Exception as exc:
            _logger.warning("health_embeddings_failed", error=f"{type(exc).__name__}: {exc}")
            return _component(STATUS_DEGRADED, f"{type(exc).__name__}: {exc}")
        if healthy:
            return _component(STATUS_OK, f"modelo '{model}' com {dimensions} dimensoes")
        motivo = getattr(self.embeddings, "last_health_error", None)
        detalhe = (
            f"sonda de '{model}' falhou: {motivo}" if motivo else f"modelo '{model}' indisponivel"
        )
        return _component(STATUS_DEGRADED, detalhe)

    def _check_tracer(self) -> dict[str, str]:
        """Tracer no-op e degradacao esperada, nunca falha (SPEC-0008 secao 3)."""
        try:
            enabled = bool(self.tracer.enabled)
        except Exception as exc:  # pragma: no cover - adaptador defeituoso
            _logger.warning("health_tracer_failed", error=f"{type(exc).__name__}: {exc}")
            return _component(STATUS_DEGRADED, f"{type(exc).__name__}: {exc}")
        if enabled:
            return _component(STATUS_OK, "tracing ativo")
        return _component(STATUS_DEGRADED, "tracer no-op: traces nao sao enviados")

    def _check_registry(self) -> dict[str, str]:
        """Conta building blocks registrados e sinaliza falhas de descoberta."""
        try:
            total = len(self.registry)
            failures = len(self.registry.discover_errors)
        except Exception as exc:  # pragma: no cover - registry defeituoso
            return _component(STATUS_DEGRADED, f"{type(exc).__name__}: {exc}")
        if total == 0:
            return _component(STATUS_DEGRADED, "nenhum building block registrado")
        if failures:
            return _component(
                STATUS_DEGRADED, f"{total} modulo(s) registrado(s), {failures} com falha de carga"
            )
        return _component(STATUS_OK, f"{total} modulo(s) registrado(s)")

    @staticmethod
    def _safe(read: Callable[[], _T], fallback: _T) -> _T:
        """Le uma propriedade de porta sem deixar a sonda de saude levantar."""
        try:
            return read()
        except Exception:  # pragma: no cover - adaptador defeituoso
            return fallback
