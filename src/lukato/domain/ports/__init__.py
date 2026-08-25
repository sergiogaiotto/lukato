"""Portas do hexagono: interfaces abstratas que o dominio exige do mundo externo.

Todas sao `typing.Protocol` (estruturais): os adaptadores as satisfazem sem
herdar nada, e o dominio permanece livre de qualquer dependencia de I/O.
"""

from __future__ import annotations

from lukato.domain.ports.embeddings import EmbeddingPort
from lukato.domain.ports.guardrail import GuardrailPort, GuardrailRuleEvaluator
from lukato.domain.ports.llm import ChatMessage, LLMPort, LLMResponse
from lukato.domain.ports.media import (
    ASRPort,
    MediaProbePort,
    MediaToolbox,
    OCRPort,
    SceneDetectorPort,
    VisionJudgePort,
)
from lukato.domain.ports.misc import (
    CachePort,
    ClockPort,
    IdGeneratorPort,
    PasswordHasherPort,
    TokenServicePort,
)
from lukato.domain.ports.observability import SpanHandle, TracerPort
from lukato.domain.ports.orchestrator import (
    OrchestratorPort,
    OrchestratorRequest,
    OrchestratorResult,
)
from lukato.domain.ports.repositories import (
    ApiKeyRepository,
    BudgetRepository,
    CommercialRepository,
    DetectionRepository,
    DocumentRepository,
    GuardrailRepository,
    MediaRepository,
    ModuleRepository,
    PromptRepository,
    RunRepository,
    UsageRepository,
    UserRepository,
)
from lukato.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from lukato.domain.ports.vector_store import VectorStorePort

__all__ = [
    "ASRPort",
    "ApiKeyRepository",
    "BudgetRepository",
    "CachePort",
    "ChatMessage",
    "ClockPort",
    "CommercialRepository",
    "DetectionRepository",
    "DocumentRepository",
    "EmbeddingPort",
    "GuardrailPort",
    "GuardrailRepository",
    "GuardrailRuleEvaluator",
    "IdGeneratorPort",
    "LLMPort",
    "LLMResponse",
    "MediaProbePort",
    "MediaRepository",
    "MediaToolbox",
    "ModuleRepository",
    "OCRPort",
    "OrchestratorPort",
    "OrchestratorRequest",
    "OrchestratorResult",
    "PasswordHasherPort",
    "PromptRepository",
    "RunRepository",
    "SceneDetectorPort",
    "SpanHandle",
    "TokenServicePort",
    "TracerPort",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UsageRepository",
    "UserRepository",
    "VectorStorePort",
    "VisionJudgePort",
]
