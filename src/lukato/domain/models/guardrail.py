"""Modelos de guardrails: politicas, regras, achados e veredito."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.types import Id, Json

__all__ = [
    "GuardrailAction",
    "GuardrailFinding",
    "GuardrailPolicy",
    "GuardrailRule",
    "GuardrailRuleKind",
    "GuardrailSeverity",
    "GuardrailStage",
    "GuardrailVerdict",
]


class GuardrailStage(StrEnum):
    """Momento de aplicacao da politica no pipeline do modulo."""

    INPUT = "input"
    OUTPUT = "output"


class GuardrailAction(StrEnum):
    """Acao tomada quando a regra dispara."""

    ALLOW = "allow"
    WARN = "warn"
    REDACT = "redact"
    TRANSFORM = "transform"
    BLOCK = "block"


class GuardrailSeverity(StrEnum):
    """Gravidade do achado."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GuardrailRuleKind(StrEnum):
    """Tipo de avaliador que implementa a regra."""

    REGEX_BLOCK = "regex_block"
    REGEX_REQUIRE = "regex_require"
    KEYWORD_BLOCK = "keyword_block"
    PII_REDACT = "pii_redact"
    MAX_LENGTH = "max_length"
    MIN_LENGTH = "min_length"
    JSON_SCHEMA = "json_schema"
    LANGUAGE_ALLOW = "language_allow"
    TOPIC_BLOCK = "topic_block"
    LLM_JUDGE = "llm_judge"
    SECRET_SCAN = "secret_scan"  # noqa: S105 — nome de regra, nao credencial


class GuardrailRule(DomainModel):
    """Regra individual de uma politica; `id` e unico dentro da politica."""

    id: str
    kind: GuardrailRuleKind
    action: GuardrailAction = GuardrailAction.BLOCK
    severity: GuardrailSeverity = GuardrailSeverity.MEDIUM
    config: Json = Field(default_factory=dict)
    message: str = ""
    enabled: bool = True
    order: int = 0


class GuardrailPolicy(Entity):
    """Conjunto ordenado de regras aplicado em um estagio do pipeline."""

    slug: str
    name: str
    description: str = ""
    stage: GuardrailStage
    rules: list[GuardrailRule] = Field(default_factory=list)
    fail_open: bool = False
    is_active: bool = True


class GuardrailFinding(DomainModel):
    """Evidencia produzida por uma regra que disparou."""

    rule_id: str
    kind: GuardrailRuleKind
    action: GuardrailAction
    severity: GuardrailSeverity
    message: str
    evidence: str = ""
    span: tuple[int, int] | None = None


class GuardrailVerdict(DomainModel):
    """Resultado da aplicacao de uma politica sobre um conteudo."""

    allowed: bool
    stage: GuardrailStage
    content: str
    original_content: str
    findings: list[GuardrailFinding] = Field(default_factory=list)
    policy_id: Id | None = None
    latency_ms: float = 0.0

    @property
    def blocked(self) -> bool:
        """True quando o conteudo foi barrado pela politica."""
        return not self.allowed

    @property
    def modified(self) -> bool:
        """True quando o conteudo final difere do original (redacao/transformacao)."""
        return self.content != self.original_content
