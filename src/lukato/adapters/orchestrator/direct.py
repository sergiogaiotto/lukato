"""Orquestrador `direct`: uma unica chamada de LLM (SPEC-0004 secao 1).

E o runtime mais barato do catalogo e a referencia de comportamento dos demais:
monta `[system?] + history + user`, chama o `LLMPort` injetado respeitando a
`ModuleBinding` do modulo e devolve um unico `RunStep` de tipo `LLM`.

Este modulo tambem concentra os utilitarios compartilhados pelos outros
orquestradores (montagem de mensagens, criacao de steps e chamada de LLM com
timeout), para que `langgraph_runtime` e `deep_agent_harness` nao repitam regra.
Nenhum deles cria cliente proprio: o provedor chega sempre por injecao.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from lukato.config import Settings, get_logger
from lukato.domain.errors import ProviderError
from lukato.domain.models.module import ModuleBinding
from lukato.domain.models.run import RunStatus, RunStep, StepKind, TokenUsage
from lukato.domain.ports.llm import ChatMessage, LLMPort, LLMResponse
from lukato.domain.ports.orchestrator import OrchestratorRequest, OrchestratorResult
from lukato.domain.types import Id, Json, new_id, utcnow

__all__ = [
    "MAX_STEP_TEXT_CHARS",
    "DirectOrchestrator",
    "build_messages",
    "call_llm",
    "clip_text",
    "new_step",
    "run_id_of",
]

_logger = get_logger(__name__)

MAX_STEP_TEXT_CHARS: Final[int] = 4000
"""Recorte do texto gravado em `RunStep.input`/`output` (a trilha nao e o dado)."""


def clip_text(text: str, limit: int = MAX_STEP_TEXT_CHARS) -> str:
    """Recorta textos longos antes de grava-los na trilha de execucao."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def run_id_of(request: OrchestratorRequest) -> Id:
    """Identificador da execucao: vem de `metadata['run_id']` quando o caso de uso o envia."""
    candidate = request.metadata.get("run_id")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return new_id()


def build_messages(request: OrchestratorRequest) -> list[ChatMessage]:
    """Monta `[system?] + history + user` na ordem exigida pela SPEC-0000 secao 10.2."""
    messages: list[ChatMessage] = []
    if request.system_prompt.strip():
        messages.append(ChatMessage.system(request.system_prompt))
    messages.extend(ChatMessage(role=item.role, content=item.content) for item in request.history)
    messages.append(ChatMessage.user(request.input_text))
    return messages


def new_step(
    *,
    run_id: Id,
    index: int,
    kind: StepKind,
    name: str,
    inputs: Json | None = None,
    outputs: Json | None = None,
    usage: TokenUsage | None = None,
    latency_ms: float = 0.0,
    status: RunStatus = RunStatus.SUCCEEDED,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> RunStep:
    """Cria um `RunStep` completo, com carimbos de inicio e fim sempre preenchidos."""
    ended = finished_at or utcnow()
    return RunStep(
        run_id=run_id,
        index=index,
        kind=kind,
        name=name,
        status=status,
        input=inputs or {},
        output=outputs or {},
        usage=usage or TokenUsage(),
        latency_ms=round(latency_ms, 3),
        error=error,
        started_at=started_at or ended,
        finished_at=ended,
    )


async def call_llm(
    llm: LLMPort,
    messages: Sequence[ChatMessage],
    *,
    binding: ModuleBinding,
    response_format: Json | None = None,
    metadata: Json | None = None,
) -> tuple[LLMResponse, float]:
    """Chama o `LLMPort` respeitando modelo, temperatura, teto de tokens e timeout.

    Estouro de tempo vira `ProviderError` (502): e uma falha do provedor, nao do
    dominio. Os demais erros ja chegam convertidos pelo adaptador de LLM.
    """
    started = time.perf_counter()
    timeout = binding.timeout_seconds if binding.timeout_seconds > 0 else None
    try:
        if timeout is None:
            response = await llm.chat(
                messages,
                model=binding.model,
                temperature=binding.temperature,
                max_tokens=binding.max_tokens,
                response_format=response_format,
                metadata=metadata,
            )
        else:
            async with asyncio.timeout(timeout):
                response = await llm.chat(
                    messages,
                    model=binding.model,
                    temperature=binding.temperature,
                    max_tokens=binding.max_tokens,
                    response_format=response_format,
                    metadata=metadata,
                )
    except TimeoutError as exc:
        raise ProviderError(
            f"O provedor de LLM nao respondeu em {timeout:.0f}s.",
            details={"timeout_seconds": timeout, "model": binding.model},
        ) from exc
    return response, (time.perf_counter() - started) * 1000.0


class DirectOrchestrator:
    """Runtime `direct`: uma chamada de LLM, um step, nenhum grafo."""

    name: str = "direct"

    def __init__(self, llm: LLMPort, *, settings: Settings | None = None) -> None:
        self._llm = llm
        self._settings = settings

    def supports(self, runtime: str) -> bool:
        """True apenas para o runtime `direct`."""
        return (runtime or "").strip().lower() == self.name

    async def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        """Executa a chamada unica e devolve o texto com o step `LLM` correspondente."""
        run_id = run_id_of(request)
        binding = request.module.binding
        messages = build_messages(request)
        started_at = utcnow()
        response, latency_ms = await call_llm(
            self._llm,
            messages,
            binding=binding,
            metadata={
                "module_slug": request.module.slug,
                "runtime": self.name,
                "run_id": run_id,
            },
        )
        step = new_step(
            run_id=run_id,
            index=0,
            kind=StepKind.LLM,
            name="direct.llm",
            inputs={
                "messages": len(messages),
                "model": binding.model or self._llm.default_model,
                "temperature": binding.temperature,
                "max_tokens": binding.max_tokens,
                "prompt": clip_text(request.input_text),
            },
            outputs={
                "content": clip_text(response.content),
                "finish_reason": response.finish_reason,
            },
            usage=response.usage,
            latency_ms=latency_ms,
            started_at=started_at,
        )
        _logger.info(
            "orchestrator_direct_completed",
            module=request.module.slug,
            run_id=run_id,
            model=response.model,
            latency_ms=round(latency_ms, 3),
            total_tokens=response.usage.total_tokens,
        )
        return OrchestratorResult(
            output_text=response.content,
            steps=[step],
            usage=response.usage,
            metadata={
                "runtime": self.name,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
        )
