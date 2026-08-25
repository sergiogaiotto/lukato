"""Avaliador `llm_judge`: julgamento semantico delegado a um LLM.

E a unica regra nao deterministica do catalogo — por isso a SPEC-0003 recomenda
mante-la sempre como a **ultima** da politica. O contrato de saida e um JSON
estrito `{"violates": bool, "score": float, "reason": str}`; qualquer desvio
(juiz ausente, timeout, erro do provedor, resposta que nao e JSON) vira um achado
`WARN`, **nunca** um bloqueio: uma indisponibilidade de rede nao pode derrubar o
pipeline nem, pior, aprovar silenciosamente um conteudo como se fosse limpo.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    config_float,
    config_str,
    snippet,
)
from lukato.domain.errors import ValidationError
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailFinding,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailSeverity,
)
from lukato.domain.ports.llm import ChatMessage, LLMPort
from lukato.domain.types import Json

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_TIMEOUT_SECONDS",
    "JUDGE_SYSTEM_PROMPT",
    "LlmJudgeEvaluator",
    "validate_rule",
]

DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
"""Teto curto: o juiz e um adicional de qualidade, nao pode travar a requisicao."""

DEFAULT_THRESHOLD: Final[float] = 0.5
"""Pontuacao minima do juiz para considerar a violacao confirmada."""

DEFAULT_MAX_CONTENT_CHARS: Final[int] = 8000
"""Recorte do conteudo enviado ao juiz (custo e latencia previsiveis)."""

JUDGE_SYSTEM_PROMPT: Final[str] = (
    "Voce e um auditor de conformidade de conteudo. Avalie se o CONTEUDO viola o "
    "CRITERIO informado. Responda EXCLUSIVAMENTE com um unico objeto JSON valido, "
    "sem markdown, sem cercas de codigo e sem texto antes ou depois, no formato "
    '{"violates": true|false, "score": 0.0, "reason": "justificativa curta"}. '
    "O campo score e a sua confianca de 0.0 a 1.0 de que houve violacao."
)

_JSON_OBJECT: Final[Json] = {"type": "json_object"}
_FENCE = re.compile(r"^```[A-Za-z0-9_+\-]*\s*|\s*```$")
_MAX_TOKENS: Final[int] = 256


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `llm_judge`."""
    criteria = config_str(rule.config, "criteria", rule_id=rule.id)
    if not criteria.strip():
        raise ValidationError(
            f"A regra llm_judge '{rule.id}' exige 'criteria' descrevendo o que julgar.",
            details={"rule_id": rule.id},
        )
    config_float(
        rule.config,
        "threshold",
        rule_id=rule.id,
        default=DEFAULT_THRESHOLD,
        minimum=0.0,
        maximum=1.0,
    )
    config_str(rule.config, "model", rule_id=rule.id)


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """Converte a resposta do juiz no veredito estruturado; `None` se invalida."""
    payload = text.strip()
    if payload.startswith("```"):
        payload = _FENCE.sub("", payload).strip()
    if not payload.startswith("{"):
        start = payload.find("{")
        end = payload.rfind("}")
        if start < 0 or end <= start:
            return None
        payload = payload[start : end + 1]
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or "violates" not in parsed:
        return None
    violates = parsed.get("violates")
    if not isinstance(violates, bool):
        return None
    raw_score = parsed.get("score", 1.0 if violates else 0.0)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None
    reason = parsed.get("reason", "")
    return {
        "violates": violates,
        "score": min(max(score, 0.0), 1.0),
        "reason": str(reason) if reason is not None else "",
    }


class LlmJudgeEvaluator:
    """`llm_judge`: pede a um LLM um veredito JSON sobre o criterio da regra."""

    kind: GuardrailRuleKind = GuardrailRuleKind.LLM_JUDGE

    def __init__(
        self,
        llm: LLMPort | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        model: str | None = None,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    ) -> None:
        self._llm = llm
        self._timeout = max(0.1, float(timeout))
        self._model = model
        self._max_content_chars = max(1, int(max_content_chars))

    @property
    def available(self) -> bool:
        """True quando existe um provedor de LLM injetado para julgar."""
        return self._llm is not None

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Julga o conteudo; qualquer falha vira `WARN` e o pipeline segue."""
        validate_rule(rule)
        if self._llm is None:
            return self._warn(
                rule,
                f"Regra llm_judge '{rule.id}' nao avaliada: nenhum juiz LLM configurado "
                "neste ambiente.",
            )
        if not content.strip():
            return None

        criteria = config_str(rule.config, "criteria", rule_id=rule.id)
        threshold = config_float(
            rule.config,
            "threshold",
            rule_id=rule.id,
            default=DEFAULT_THRESHOLD,
            minimum=0.0,
            maximum=1.0,
        )
        model = config_str(rule.config, "model", rule_id=rule.id) or self._model
        excerpt = content[: self._max_content_chars]
        messages = [
            ChatMessage.system(JUDGE_SYSTEM_PROMPT),
            ChatMessage.user(f"CRITERIO:\n{criteria}\n\nCONTEUDO:\n{excerpt}"),
        ]

        try:
            response = await asyncio.wait_for(
                self._llm.chat(
                    messages,
                    model=model,
                    temperature=0.0,
                    max_tokens=_MAX_TOKENS,
                    response_format=_JSON_OBJECT,
                    metadata={"guardrail_rule": rule.id, "stage": context.get("stage", "")},
                ),
                timeout=self._timeout,
            )
        except TimeoutError:
            return self._warn(
                rule,
                f"Juiz LLM nao respondeu em {self._timeout:.1f}s; regra '{rule.id}' nao avaliada.",
            )
        except Exception as exc:  # provedor indisponivel nunca bloqueia o pipeline
            return self._warn(
                rule,
                f"Falha ao consultar o juiz LLM da regra '{rule.id}' "
                f"({type(exc).__name__}: {exc}).",
            )

        verdict = _parse_verdict(response.content)
        if verdict is None:
            return self._warn(
                rule,
                f"Resposta do juiz LLM da regra '{rule.id}' nao e o JSON esperado; "
                f"conteudo ignorado: {snippet(response.content, limit=120)}",
            )
        if not verdict["violates"] or verdict["score"] < threshold:
            return None

        reason = verdict["reason"] or "sem justificativa informada"
        message = (
            f"Juiz LLM apontou violacao (score {verdict['score']:.2f} >= {threshold:.2f}): {reason}"
        )
        # O juiz opina, nao reescreve: em acao de conteudo o texto segue intacto.
        evidence = content if rule.action in CONTENT_ACTIONS else snippet(reason)
        return build_finding(rule, message, evidence=evidence)

    @staticmethod
    def _warn(rule: GuardrailRule, message: str) -> GuardrailFinding:
        """Achado de aviso: o juiz nao pode ser consultado ou nao foi compreendido."""
        return build_finding(
            rule,
            message,
            action=GuardrailAction.WARN,
            severity=GuardrailSeverity.LOW,
            use_rule_message=False,
        )
