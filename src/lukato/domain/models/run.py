"""Modelos de execucao: passos, consumo de tokens e a execucao completa do modulo."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.types import DEFAULT_TENANT, Id, Json, new_id

__all__ = ["AgentRun", "RunStatus", "RunStep", "StepKind", "TokenUsage"]


class RunStatus(StrEnum):
    """Estado de uma execucao (ou de um passo dela)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class StepKind(StrEnum):
    """Natureza do passo executado dentro do pipeline do modulo."""

    GUARDRAIL_IN = "guardrail_in"
    PROMPT = "prompt"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    PLAN = "plan"
    REFLECT = "reflect"
    GUARDRAIL_OUT = "guardrail_out"
    ERROR = "error"


class TokenUsage(DomainModel):
    """Consumo de tokens de uma chamada de LLM."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @model_validator(mode="after")
    def _autofill_total(self) -> TokenUsage:
        """Calcula `total_tokens` quando nao informado."""
        if self.total_tokens == 0:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self

    @classmethod
    def of(cls, prompt: int, completion: int) -> TokenUsage:
        """Cria um consumo somando prompt + completion no total."""
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Soma dois consumos campo a campo."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class RunStep(DomainModel):
    """Passo individual registrado durante a execucao de um modulo."""

    id: Id = Field(default_factory=new_id)
    run_id: Id
    index: int
    kind: StepKind
    name: str
    status: RunStatus = RunStatus.SUCCEEDED
    input: Json = Field(default_factory=dict)
    output: Json = Field(default_factory=dict)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentRun(Entity):
    """Execucao completa de um building block, com trilha de passos e custo."""

    module_id: Id
    module_slug: str
    status: RunStatus = RunStatus.PENDING
    input: Json = Field(default_factory=dict)
    output: Json = Field(default_factory=dict)
    steps: list[RunStep] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace_id: str | None = None
    error: str | None = None
    tenant_id: str = DEFAULT_TENANT
    actor: str | None = None
    finished_at: datetime | None = None
