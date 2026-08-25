"""Motor de guardrails: aplica uma politica sobre um texto e devolve o veredito.

Implementa `GuardrailPort` (SPEC-0003, secao 2) sem qualquer dependencia de I/O:
os avaliadores de regra chegam por injecao e podem vir de qualquer adaptador.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from lukato.domain.errors import GuardrailViolation, UnsupportedCapability
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
from lukato.domain.ports.guardrail import GuardrailRuleEvaluator
from lukato.domain.types import Json

__all__ = ["GuardrailEngine"]

_CONTENT_ACTIONS = frozenset({GuardrailAction.REDACT, GuardrailAction.TRANSFORM})


class GuardrailEngine:
    """Aplica `GuardrailPolicy` sobre texto usando os avaliadores registrados.

    Determinismo: as regras habilitadas sao avaliadas na ordem `(order, id)`, e o
    conteudo de cada regra e o resultado da anterior (redacoes se acumulam).
    """

    def __init__(
        self,
        evaluators: Sequence[GuardrailRuleEvaluator],
        *,
        redaction_token: str = "[REDIGIDO]",  # noqa: S107 - marcador publico, nao e segredo
        fail_open: bool = False,
    ) -> None:
        self._evaluators: dict[GuardrailRuleKind, GuardrailRuleEvaluator] = {}
        for evaluator in evaluators:
            self.register(evaluator)
        self._redaction_token = redaction_token
        self._fail_open = fail_open

    def register(self, evaluator: GuardrailRuleEvaluator) -> None:
        """Registra (ou substitui) o avaliador responsavel por um tipo de regra."""
        self._evaluators[evaluator.kind] = evaluator

    @property
    def kinds(self) -> frozenset[GuardrailRuleKind]:
        """Tipos de regra que este motor sabe avaliar."""
        return frozenset(self._evaluators)

    @property
    def redaction_token(self) -> str:
        """Marcador oferecido aos avaliadores para substituir trechos sensiveis."""
        return self._redaction_token

    @property
    def fail_open(self) -> bool:
        """True quando falhas internas de regra viram aviso em vez de bloqueio."""
        return self._fail_open

    async def apply(
        self, content: str, policy: GuardrailPolicy | None, *, context: Json | None = None
    ) -> GuardrailVerdict:
        """Aplica a politica ao conteudo e devolve o veredito completo.

        Politica `None` ou inativa e uma escolha explicita de "sem restricao": o
        conteudo passa intacto, com `findings` vazio e `policy_id=None`.
        """
        started = time.perf_counter()
        stage = _resolve_stage(policy, context)

        if policy is None or not policy.is_active:
            return GuardrailVerdict(
                allowed=True,
                stage=stage,
                content=content,
                original_content=content,
                findings=[],
                policy_id=policy.id if policy is not None else None,
                latency_ms=_elapsed_ms(started),
            )

        fail_open = self._fail_open or policy.fail_open
        evaluation_context = self._build_context(policy, stage, context)
        current = content
        findings: list[GuardrailFinding] = []
        allowed = True

        for rule in _ordered_rules(policy):
            evaluator = self._evaluators.get(rule.kind)
            if evaluator is None:
                if not fail_open:
                    raise UnsupportedCapability(
                        f"Nenhum avaliador registrado para a regra '{rule.id}' "
                        f"({rule.kind.value}) da politica '{policy.slug}'.",
                        details={
                            "policy_id": policy.id,
                            "policy_slug": policy.slug,
                            "rule_id": rule.id,
                            "kind": rule.kind.value,
                            "stage": stage.value,
                            "registered_kinds": sorted(kind.value for kind in self.kinds),
                        },
                    )
                findings.append(
                    _warning_finding(
                        rule,
                        f"Regra '{rule.id}' nao avaliada: nenhum avaliador registrado "
                        f"para o tipo '{rule.kind.value}'.",
                    )
                )
                continue

            try:
                finding = await evaluator.evaluate(current, rule, evaluation_context)
            except Exception as exc:  # falha do avaliador vira politica explicita
                if not fail_open:
                    raise GuardrailViolation(
                        f"Falha ao avaliar a regra '{rule.id}' ({rule.kind.value}) "
                        f"da politica '{policy.slug}': {exc}",
                        details={
                            "policy_slug": policy.slug,
                            "kind": rule.kind.value,
                            "cause": type(exc).__name__,
                        },
                        policy_id=policy.id,
                        rule_id=rule.id,
                        stage=stage.value,
                    ) from exc
                findings.append(
                    _warning_finding(
                        rule,
                        f"Falha ao avaliar a regra '{rule.id}' "
                        f"({type(exc).__name__}: {exc}); politica em fail_open.",
                    )
                )
                continue

            if finding is None:
                continue

            findings.append(finding)
            if finding.action is GuardrailAction.BLOCK:
                allowed = False
                break
            if finding.action in _CONTENT_ACTIONS and finding.evidence:
                current = finding.evidence

        return GuardrailVerdict(
            allowed=allowed,
            stage=stage,
            content=current,
            original_content=content,
            findings=findings,
            policy_id=policy.id,
            latency_ms=_elapsed_ms(started),
        )

    def _build_context(
        self, policy: GuardrailPolicy, stage: GuardrailStage, context: Json | None
    ) -> Json:
        """Monta o contexto entregue aos avaliadores (politica + marcador de redacao)."""
        evaluation_context: Json = dict(context) if context else {}
        evaluation_context.setdefault("redaction_token", self._redaction_token)
        evaluation_context["stage"] = stage.value
        evaluation_context["policy_id"] = policy.id
        evaluation_context["policy_slug"] = policy.slug
        return evaluation_context


def _ordered_rules(policy: GuardrailPolicy) -> list[GuardrailRule]:
    """Regras habilitadas da politica, ordenadas por `(order, id)`."""
    return sorted(
        (rule for rule in policy.rules if rule.enabled),
        key=lambda rule: (rule.order, rule.id),
    )


def _warning_finding(rule: GuardrailRule, message: str) -> GuardrailFinding:
    """Achado de aviso usado quando a regra nao pode ser avaliada (fail_open)."""
    return GuardrailFinding(
        rule_id=rule.id,
        kind=rule.kind,
        action=GuardrailAction.WARN,
        severity=GuardrailSeverity.HIGH,
        message=message,
    )


def _resolve_stage(policy: GuardrailPolicy | None, context: Json | None) -> GuardrailStage:
    """Estagio do veredito: o da politica ou, sem politica, o informado no contexto."""
    if policy is not None:
        return policy.stage
    raw = context.get("stage") if context else None
    if raw is None:
        return GuardrailStage.INPUT
    try:
        return GuardrailStage(raw)
    except ValueError:
        return GuardrailStage.INPUT


def _elapsed_ms(started: float) -> float:
    """Tempo decorrido em milissegundos desde `started` (relogio monotonico)."""
    return round((time.perf_counter() - started) * 1000.0, 3)
