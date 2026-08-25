"""Schemas do recurso `/api/v1/runs`: trilha de execucao dos building blocks.

Toda invocacao deixa um `AgentRun` com os passos na ordem em que aconteceram —
guardrail de entrada, prompt, LLM, ferramentas, guardrail de saida. A listagem
devolve o resumo (sem os passos) e o detalhe devolve a trilha inteira.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from lukato.domain.models.run import AgentRun, RunStatus, RunStep, StepKind
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import OutSchema, TokenUsageOut

__all__ = ["RunOut", "RunStepOut", "RunSummaryOut"]


class RunStepOut(OutSchema):
    """Passo individual da trilha de execucao."""

    id: Id
    run_id: Id
    index: int = Field(description="Posicao do passo na trilha, a partir de zero.")
    kind: StepKind = Field(description="Natureza do passo.")
    name: str = Field(description="Nome legivel do passo.")
    status: RunStatus = Field(default=RunStatus.SUCCEEDED, description="Desfecho do passo.")
    input: Json = Field(default_factory=dict, description="Entrada registrada do passo.")
    output: Json = Field(default_factory=dict, description="Saida registrada do passo.")
    usage: TokenUsageOut = Field(default_factory=TokenUsageOut, description="Consumo do passo.")
    cost_usd: float = Field(default=0.0, ge=0.0, description="Custo do passo em USD.")
    latency_ms: float = Field(default=0.0, ge=0.0, description="Duracao do passo em ms.")
    error: str | None = Field(default=None, description="Mensagem de falha, quando houver.")
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_domain(cls, step: RunStep) -> RunStepOut:
        """Converte o passo de dominio."""
        return cls(
            id=step.id,
            run_id=step.run_id,
            index=step.index,
            kind=step.kind,
            name=step.name,
            status=step.status,
            input=dict(step.input),
            output=dict(step.output),
            usage=TokenUsageOut.from_domain(step.usage),
            cost_usd=step.cost_usd,
            latency_ms=step.latency_ms,
            error=step.error,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )


class RunSummaryOut(OutSchema):
    """Execucao sem a trilha de passos — o suficiente para a listagem."""

    id: Id
    module_id: Id
    module_slug: str
    status: RunStatus = RunStatus.PENDING
    usage: TokenUsageOut = Field(default_factory=TokenUsageOut)
    cost_usd: float = Field(default=0.0, ge=0.0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    trace_id: str | None = None
    error: str | None = None
    tenant_id: str = "default"
    actor: str | None = None
    steps_count: int = Field(default=0, ge=0, description="Quantidade de passos registrados.")
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @classmethod
    def from_domain(cls, run: AgentRun) -> RunSummaryOut:
        """Converte a execucao de dominio, descartando a trilha."""
        return cls(
            id=run.id,
            module_id=run.module_id,
            module_slug=run.module_slug,
            status=run.status,
            usage=TokenUsageOut.from_domain(run.usage),
            cost_usd=run.cost_usd,
            latency_ms=run.latency_ms,
            trace_id=run.trace_id,
            error=run.error,
            tenant_id=run.tenant_id,
            actor=run.actor,
            steps_count=len(run.steps),
            created_at=run.created_at,
            updated_at=run.updated_at,
            finished_at=run.finished_at,
        )


class RunOut(RunSummaryOut):
    """Execucao completa, com entrada, saida e a trilha de passos."""

    input: Json = Field(default_factory=dict, description="Entrada da invocacao.")
    output: Json = Field(default_factory=dict, description="Saida final da invocacao.")
    steps: list[RunStepOut] = Field(
        default_factory=list, description="Passos na ordem de execucao."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "0f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
                "module_id": "11111111-2222-3333-4444-555555555555",
                "module_slug": "atendimento",
                "status": "succeeded",
                "usage": {"prompt_tokens": 312, "completion_tokens": 88, "total_tokens": 400},
                "cost_usd": 0.0012,
                "latency_ms": 842.5,
                "trace_id": None,
                "error": None,
                "tenant_id": "default",
                "actor": "anonymous",
                "steps_count": 4,
                "input": {"input": "Explique a cobranca de julho."},
                "output": {"output": "A cobranca inclui o plano e um servico avulso."},
                "steps": [],
            }
        }
    )

    @classmethod
    def from_domain(cls, run: AgentRun) -> RunOut:
        """Converte a execucao de dominio com a trilha inteira."""
        summary = RunSummaryOut.from_domain(run)
        return cls(
            **summary.model_dump(),
            input=dict(run.input),
            output=dict(run.output),
            steps=[RunStepOut.from_domain(step) for step in run.steps],
        )
