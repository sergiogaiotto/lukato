"""Modelos de dominio do lukato (entidades e value objects em pydantic v2)."""

from __future__ import annotations

from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionCandidate,
    DetectionEvidence,
    DetectionStatus,
    MediaAsset,
    MediaKind,
    OcrText,
    SceneCut,
    Transcript,
    TranscriptWord,
)
from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.models.finops import (
    Budget,
    BudgetPeriod,
    CostSummary,
    ModelPrice,
    UsageRecord,
)
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailFinding,
    GuardrailPolicy,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailSeverity,
    GuardrailStage,
    GuardrailVerdict,
)
from lukato.domain.models.identity import (
    ROLE_PERMISSIONS,
    ApiKey,
    Permission,
    Principal,
    Role,
    User,
    permissions_for,
)
from lukato.domain.models.knowledge import Chunk, Document, SearchHit
from lukato.domain.models.module import (
    MODULE_SLUG_PATTERN,
    ModuleBinding,
    ModuleDefinition,
    ModuleKind,
    ModuleStatus,
)
from lukato.domain.models.prompt import PromptRole, PromptTemplate, extract_variables
from lukato.domain.models.run import AgentRun, RunStatus, RunStep, StepKind, TokenUsage

__all__ = [
    "MODULE_SLUG_PATTERN",
    "ROLE_PERMISSIONS",
    "AdFingerprint",
    "AgentRun",
    "ApiKey",
    "Budget",
    "BudgetPeriod",
    "Chunk",
    "Commercial",
    "CostSummary",
    "Detection",
    "DetectionCandidate",
    "DetectionEvidence",
    "DetectionStatus",
    "Document",
    "DomainModel",
    "Entity",
    "GuardrailAction",
    "GuardrailFinding",
    "GuardrailPolicy",
    "GuardrailRule",
    "GuardrailRuleKind",
    "GuardrailSeverity",
    "GuardrailStage",
    "GuardrailVerdict",
    "MediaAsset",
    "MediaKind",
    "ModelPrice",
    "ModuleBinding",
    "ModuleDefinition",
    "ModuleKind",
    "ModuleStatus",
    "OcrText",
    "Permission",
    "Principal",
    "PromptRole",
    "PromptTemplate",
    "Role",
    "RunStatus",
    "RunStep",
    "SceneCut",
    "SearchHit",
    "StepKind",
    "TokenUsage",
    "Transcript",
    "TranscriptWord",
    "UsageRecord",
    "User",
    "extract_variables",
    "permissions_for",
]
