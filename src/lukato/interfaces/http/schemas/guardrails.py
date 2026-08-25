"""Schemas do recurso `/api/v1/guardrails`: politicas, regras e veredito.

O guardrail e a primeira e a ultima etapa de todo modulo. Estes schemas cobrem o
CRUD das politicas, o catalogo de tipos de regra que alimenta o editor do console
e o **testador**, que devolve o veredito completo — conteudo ja redigido, achados
por regra e latencia — sem persistir nada.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from lukato.application.dto import UNSET, Maybe
from lukato.application.use_cases.guardrails import PolicyCreateInput, PolicyUpdateInput
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
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import InSchema, OutSchema

__all__ = [
    "GuardrailFindingOut",
    "GuardrailRuleIn",
    "GuardrailRuleOut",
    "PolicyCreate",
    "PolicyDraft",
    "PolicyOut",
    "PolicyTestRequest",
    "PolicyTestResponse",
    "PolicyUpdate",
    "RuleKindInfo",
]

_RULE_EXAMPLE: dict[str, Any] = {
    "id": "sem-segredo",
    "kind": "secret_scan",
    "action": "redact",
    "severity": "high",
    "config": {"extra_patterns": []},
    "message": "Credencial detectada e redigida.",
    "enabled": True,
    "order": 10,
}


# ---------------------------------------------------------------------------
# Regras
# ---------------------------------------------------------------------------
class GuardrailRuleIn(InSchema):
    """Regra enviada na criacao ou atualizacao de uma politica."""

    id: str = Field(min_length=1, description="Identificador da regra, unico dentro da politica.")
    kind: GuardrailRuleKind = Field(description="Tipo de avaliador que implementa a regra.")
    action: GuardrailAction = Field(
        default=GuardrailAction.BLOCK, description="O que fazer quando a regra dispara."
    )
    severity: GuardrailSeverity = Field(
        default=GuardrailSeverity.MEDIUM, description="Gravidade do achado."
    )
    config: Json = Field(default_factory=dict, description="Parametros proprios do tipo de regra.")
    message: str = Field(default="", description="Mensagem exibida quando a regra dispara.")
    enabled: bool = Field(default=True, description="Regra desligada nao e avaliada.")
    order: int = Field(default=0, description="Ordem de avaliacao dentro da politica.")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": _RULE_EXAMPLE})

    def to_domain(self) -> GuardrailRule:
        """Converte para a regra de dominio."""
        return GuardrailRule(
            id=self.id,
            kind=self.kind,
            action=self.action,
            severity=self.severity,
            config=dict(self.config),
            message=self.message,
            enabled=self.enabled,
            order=self.order,
        )


class GuardrailRuleOut(OutSchema):
    """Regra devolvida com a politica."""

    id: str
    kind: GuardrailRuleKind
    action: GuardrailAction = GuardrailAction.BLOCK
    severity: GuardrailSeverity = GuardrailSeverity.MEDIUM
    config: Json = Field(default_factory=dict)
    message: str = ""
    enabled: bool = True
    order: int = 0

    @classmethod
    def from_domain(cls, rule: GuardrailRule) -> GuardrailRuleOut:
        """Converte a regra de dominio."""
        return cls(
            id=rule.id,
            kind=rule.kind,
            action=rule.action,
            severity=rule.severity,
            config=dict(rule.config),
            message=rule.message,
            enabled=rule.enabled,
            order=rule.order,
        )


class RuleKindInfo(OutSchema):
    """Descritor de um tipo de regra, usado pelo editor de politicas do console."""

    kind: GuardrailRuleKind = Field(description="Tipo de regra.")
    descricao: str = Field(default="", description="O que a regra faz, em portugues.")
    config_schema: Json = Field(
        default_factory=dict, description="JSON Schema dos parametros aceitos em `config`."
    )
    acoes_suportadas: list[GuardrailAction] = Field(
        default_factory=list, description="Acoes que fazem sentido para este tipo."
    )

    @classmethod
    def from_catalog(cls, entry: Json) -> RuleKindInfo:
        """Converte uma entrada de `ListRuleKinds`."""
        return cls(
            kind=GuardrailRuleKind(entry["kind"]),
            descricao=str(entry.get("descricao", "")),
            config_schema=dict(entry.get("config_schema") or {}),
            acoes_suportadas=[
                GuardrailAction(item) for item in entry.get("acoes_suportadas") or []
            ],
        )


# ---------------------------------------------------------------------------
# Politicas
# ---------------------------------------------------------------------------
class PolicyCreate(InSchema):
    """Corpo de `POST /api/v1/guardrails/policies`."""

    slug: str = Field(min_length=1, description="Identificador estavel da politica.")
    name: str = Field(default="", description="Nome exibido no console.")
    description: str = Field(default="", description="O que esta politica protege.")
    stage: GuardrailStage = Field(
        default=GuardrailStage.INPUT, description="Estagio de aplicacao (entrada ou saida)."
    )
    rules: list[GuardrailRuleIn] = Field(default_factory=list, description="Regras da politica.")
    fail_open: bool = Field(
        default=False, description="True deixa passar quando a propria regra falha."
    )
    is_active: bool = Field(default=True, description="Politica inativa nao e aplicada.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "slug": "entrada-padrao",
                "name": "Entrada padrao",
                "description": "Bloqueia injecao de prompt e redige dados pessoais.",
                "stage": "input",
                "rules": [_RULE_EXAMPLE],
                "fail_open": False,
                "is_active": True,
            }
        },
    )

    def to_input(self) -> PolicyCreateInput:
        """Converte para o DTO do caso de uso `CreatePolicy`."""
        return PolicyCreateInput(
            slug=self.slug,
            name=self.name,
            description=self.description,
            stage=self.stage,
            rules=[rule.to_domain() for rule in self.rules],
            fail_open=self.fail_open,
            is_active=self.is_active,
        )


class PolicyUpdate(InSchema):
    """Corpo de `PUT /api/v1/guardrails/policies/{ref}`: so muda o que foi enviado."""

    name: str | None = None
    description: str | None = None
    stage: GuardrailStage | None = None
    rules: list[GuardrailRuleIn] | None = None
    fail_open: bool | None = None
    is_active: bool | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"is_active": False, "fail_open": True}},
    )

    def to_input(self) -> PolicyUpdateInput:
        """Converte para o DTO parcial, preservando o que nao foi enviado."""
        sent = self.model_fields_set

        def maybe(field: str, value: Any) -> Maybe[Any]:
            return value if field in sent else UNSET

        rules = None if self.rules is None else [rule.to_domain() for rule in self.rules]
        return PolicyUpdateInput(
            name=maybe("name", self.name),
            description=maybe("description", self.description),
            stage=maybe("stage", self.stage),
            rules=maybe("rules", rules),
            fail_open=maybe("fail_open", self.fail_open),
            is_active=maybe("is_active", self.is_active),
        )


class PolicyOut(OutSchema):
    """Politica devolvida pela API."""

    id: Id
    slug: str
    name: str
    description: str = ""
    stage: GuardrailStage
    rules: list[GuardrailRuleOut] = Field(default_factory=list)
    fail_open: bool = False
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, policy: GuardrailPolicy) -> PolicyOut:
        """Converte a entidade de dominio."""
        return cls(
            id=policy.id,
            slug=policy.slug,
            name=policy.name,
            description=policy.description,
            stage=policy.stage,
            rules=[GuardrailRuleOut.from_domain(rule) for rule in policy.rules],
            fail_open=policy.fail_open,
            is_active=policy.is_active,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
        )


# ---------------------------------------------------------------------------
# Testador
# ---------------------------------------------------------------------------
class PolicyDraft(InSchema):
    """Politica avulsa (rascunho do editor) enviada ao testador sem ser salva."""

    slug: str = Field(default="politica-em-teste", description="Slug provisorio do rascunho.")
    name: str = Field(default="Politica em teste", description="Nome provisorio do rascunho.")
    description: str = Field(default="", description="Anotacao livre do autor.")
    stage: GuardrailStage | None = Field(
        default=None, description="Estagio; ausente herda o do pedido."
    )
    rules: list[GuardrailRuleIn] = Field(
        default_factory=list, description="Regras do rascunho, na ordem desejada."
    )
    fail_open: bool = Field(default=False, description="Comportamento diante de regra defeituosa.")
    is_active: bool = Field(default=True, description="Mantido para espelhar a politica salva.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "slug": "rascunho",
                "name": "Rascunho",
                "stage": "output",
                "rules": [_RULE_EXAMPLE],
                "fail_open": False,
            }
        },
    )

    def to_mapping(self) -> Json:
        """Converte para o mapa aceito por `TestPolicy` (politica avulsa)."""
        payload: Json = {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "rules": [rule.to_domain() for rule in self.rules],
            "fail_open": self.fail_open,
            "is_active": self.is_active,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        return payload


class PolicyTestRequest(InSchema):
    """Corpo de `POST /api/v1/guardrails/test`.

    Informe **ou** `policy` (slug/id de uma politica salva) **ou** `draft` (o
    rascunho aberto no editor). Sem nenhum dos dois, o teste exercita o caminho
    permissivo do estagio, util para comparar o antes e o depois.
    """

    content: str = Field(description="Conteudo submetido a politica.")
    policy: str | None = Field(default=None, description="Slug ou id de uma politica salva.")
    draft: PolicyDraft | None = Field(default=None, description="Politica avulsa, nao persistida.")
    stage: GuardrailStage | None = Field(
        default=None, description="Estagio a aplicar quando a politica nao o define."
    )
    context: Json = Field(
        default_factory=dict, description="Contexto extra repassado aos avaliadores."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "content": "Meu CPF e 529.982.247-25, pode confirmar?",
                "policy": "entrada-padrao",
                "stage": "input",
                "context": {},
            }
        },
    )

    def policy_argument(self) -> str | Json | None:
        """Resolve o argumento `policy` de `TestPolicy` a partir do corpo recebido."""
        if self.draft is not None:
            return self.draft.to_mapping()
        return self.policy


class GuardrailFindingOut(OutSchema):
    """Achado produzido por uma regra que disparou."""

    rule_id: str = Field(description="Regra que produziu o achado.")
    kind: GuardrailRuleKind = Field(description="Tipo do avaliador.")
    action: GuardrailAction = Field(description="Acao aplicada.")
    severity: GuardrailSeverity = Field(description="Gravidade.")
    message: str = Field(default="", description="Explicacao legivel do achado.")
    evidence: str = Field(default="", description="Trecho que sustenta o achado.")
    span: tuple[int, int] | None = Field(
        default=None, description="Intervalo `[inicio, fim)` no conteudo original."
    )

    @classmethod
    def from_domain(cls, finding: GuardrailFinding) -> GuardrailFindingOut:
        """Converte o achado de dominio."""
        return cls(
            rule_id=finding.rule_id,
            kind=finding.kind,
            action=finding.action,
            severity=finding.severity,
            message=finding.message,
            evidence=finding.evidence,
            span=finding.span,
        )


class PolicyTestResponse(OutSchema):
    """Veredito completo devolvido pelo testador de politicas."""

    allowed: bool = Field(description="False quando o conteudo foi barrado.")
    blocked: bool = Field(description="Inverso de `allowed`, para leitura direta na UI.")
    modified: bool = Field(description="True quando houve redacao ou transformacao.")
    stage: GuardrailStage = Field(description="Estagio efetivamente aplicado.")
    content: str = Field(description="Conteudo final, ja redigido quando for o caso.")
    original_content: str = Field(description="Conteudo exatamente como chegou.")
    findings: list[GuardrailFindingOut] = Field(
        default_factory=list, description="Achados, na ordem de avaliacao."
    )
    policy_id: Id | None = Field(default=None, description="Politica aplicada, quando salva.")
    latency_ms: float = Field(default=0.0, ge=0.0, description="Tempo de avaliacao em ms.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "allowed": True,
                "blocked": False,
                "modified": True,
                "stage": "input",
                "content": "Meu CPF e [REDIGIDO], pode confirmar?",
                "original_content": "Meu CPF e 529.982.247-25, pode confirmar?",
                "findings": [
                    {
                        "rule_id": "pii",
                        "kind": "pii_redact",
                        "action": "redact",
                        "severity": "high",
                        "message": "CPF redigido.",
                        "evidence": "529.982.247-25",
                        "span": [9, 23],
                    }
                ],
                "policy_id": "9f2a1b0c-1111-2222-3333-444455556666",
                "latency_ms": 1.8,
            }
        }
    )

    @classmethod
    def from_domain(cls, verdict: GuardrailVerdict) -> PolicyTestResponse:
        """Converte o veredito de dominio."""
        return cls(
            allowed=verdict.allowed,
            blocked=verdict.blocked,
            modified=verdict.modified,
            stage=verdict.stage,
            content=verdict.content,
            original_content=verdict.original_content,
            findings=[GuardrailFindingOut.from_domain(item) for item in verdict.findings],
            policy_id=verdict.policy_id,
            latency_ms=verdict.latency_ms,
        )
