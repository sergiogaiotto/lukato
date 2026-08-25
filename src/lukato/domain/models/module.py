"""Modelos do building block: definicao do modulo e sua trinca parametrizavel."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.types import Id, Json

__all__ = [
    "MODULE_SLUG_PATTERN",
    "ModuleBinding",
    "ModuleDefinition",
    "ModuleKind",
    "ModuleStatus",
]

MODULE_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
"""Slug canonico de modulo: minusculas, digitos e hifens, de 2 a 63 caracteres."""


class ModuleKind(StrEnum):
    """Tipo funcional do building block."""

    AGENT = "agent"
    TOOL = "tool"
    PIPELINE = "pipeline"
    AUTH = "auth"
    FINOPS = "finops"
    KNOWLEDGE = "knowledge"
    CUSTOM = "custom"


class ModuleStatus(StrEnum):
    """Ciclo de vida do building block."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DEPRECATED = "deprecated"


class ModuleBinding(DomainModel):
    """Trinca parametrizavel exigida para TODO modulo.

    Ordem obrigatoria de execucao: guardrail de entrada -> system prompt -> guardrail de saida.
    """

    input_guardrail_id: Id | None = None
    system_prompt_id: Id | None = None
    output_guardrail_id: Id | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    timeout_seconds: float = 60.0
    tools: list[str] = Field(default_factory=list)


class ModuleDefinition(Entity):
    """Definicao persistida de um building block plugavel."""

    slug: str = Field(pattern=MODULE_SLUG_PATTERN)
    name: str
    description: str = ""
    kind: ModuleKind = ModuleKind.AGENT
    status: ModuleStatus = ModuleStatus.DRAFT
    runtime: str = "langgraph"
    binding: ModuleBinding = Field(default_factory=ModuleBinding)
    config: Json = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    version: str = "1.0.0"
