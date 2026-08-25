"""Montagem e escolha dos orquestradores (SPEC-0004 secao 1, SPEC-0000 secao 7.8).

`build_orchestrators` devolve o mapa que o *composition root* injeta no `Container`.
Os tres runtimes recebem o **mesmo** `LLMPort` e o **mesmo** `ToolRegistry`; nenhum
deles cria cliente proprio de LLM (a unica excecao autorizada e o `ChatOpenAI` que o
Deep-Agent Harness exige, e que so nasce dentro do `run()` daquele adaptador).

`resolve` implementa a regra de degradacao: runtime conhecido porem indisponivel cai
para `langgraph` com WARNING (o endpoint continua respondendo 200), runtime
desconhecido levanta `UnsupportedCapability` (501).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from lukato.adapters.orchestrator.deep_agent_harness import DeepAgentOrchestrator
from lukato.adapters.orchestrator.direct import DirectOrchestrator
from lukato.adapters.orchestrator.langgraph_runtime import LangGraphOrchestrator
from lukato.adapters.orchestrator.tools import ToolContext, ToolRegistry
from lukato.config import Settings, get_logger
from lukato.domain.errors import UnsupportedCapability
from lukato.domain.ports.llm import LLMPort
from lukato.domain.ports.orchestrator import OrchestratorPort

__all__ = ["DEFAULT_RUNTIME", "KNOWN_RUNTIMES", "build_orchestrators", "resolve"]

_logger = get_logger(__name__)

DEFAULT_RUNTIME: Final[str] = "langgraph"
"""Runtime de referencia: para ele degradam os runtimes conhecidos porem indisponiveis."""

KNOWN_RUNTIMES: Final[frozenset[str]] = frozenset({"direct", "langgraph", "deepagent"})
"""Runtimes previstos pela SPEC-0004; qualquer outro nome e desconhecido."""


def build_orchestrators(
    llm: LLMPort,
    *,
    settings: Settings,
    tools: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, OrchestratorPort]:
    """Monta `direct`, `langgraph` e — quando disponivel — `deepagent`."""
    registry = tools if tools is not None else ToolRegistry()
    context = tool_context if tool_context is not None else ToolContext(settings=settings)
    orchestrators: dict[str, OrchestratorPort] = {
        "direct": DirectOrchestrator(llm, settings=settings),
        "langgraph": LangGraphOrchestrator(
            llm, tools=registry, tool_context=context, settings=settings
        ),
    }
    harness = DeepAgentOrchestrator(llm, settings=settings, tools=registry, tool_context=context)
    if harness.available:
        orchestrators["deepagent"] = harness
        _logger.info(
            "orchestrator_deepagent_enabled",
            runtime=harness.name,
            reason=harness.unavailable_reason,
        )
    else:
        _logger.info(
            "orchestrator_deepagent_disabled",
            runtime=harness.name,
            fallback=DEFAULT_RUNTIME,
            reason=harness.unavailable_reason,
        )
    _logger.info(
        "orchestrators_built",
        runtimes=sorted(orchestrators),
        tools=registry.names(),
    )
    return orchestrators


def resolve(orchestrators: Mapping[str, OrchestratorPort], runtime: str) -> OrchestratorPort:
    """Escolhe o orquestrador do runtime pedido, degradando para `langgraph` quando preciso.

    * runtime disponivel -> devolve o proprio;
    * runtime conhecido porem indisponivel (ex.: `deepagent` sem `deepagents` ou sem
      chave) -> devolve `langgraph` e registra WARNING com a razao da degradacao;
    * runtime desconhecido -> `UnsupportedCapability` (HTTP 501).
    """
    key = (runtime or DEFAULT_RUNTIME).strip().lower() or DEFAULT_RUNTIME
    candidate = orchestrators.get(key)
    if candidate is not None and candidate.supports(key):
        return candidate
    if key not in KNOWN_RUNTIMES and candidate is None:
        raise UnsupportedCapability(
            f"Runtime de agente desconhecido: '{key}'.",
            details={"runtime": key, "available": sorted(orchestrators)},
        )
    fallback = orchestrators.get(DEFAULT_RUNTIME)
    if fallback is None or not fallback.supports(DEFAULT_RUNTIME):
        raise UnsupportedCapability(
            f"O runtime '{key}' nao esta disponivel e nao ha '{DEFAULT_RUNTIME}' para assumir.",
            details={"runtime": key, "available": sorted(orchestrators)},
        )
    _logger.warning(
        "orchestrator_runtime_degraded",
        requested=key,
        fallback=DEFAULT_RUNTIME,
        available=sorted(orchestrators),
    )
    return fallback
