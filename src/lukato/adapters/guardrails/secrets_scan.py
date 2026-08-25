"""Avaliador `secret_scan`: procura credenciais vazadas no texto.

Cobre os formatos que mais aparecem em prompts e respostas de agentes: chaves de
API estilo OpenAI (`sk-…`), chave de acesso AWS (`AKIA…`), tokens do GitHub
(`ghp_`/`gho_`/`ghs_`), blocos PEM de chave privada, JWT (tres segmentos
base64url), cabecalhos `Bearer <token>` e tokens do Slack (`xox[baprs]-`).
Padroes adicionais chegam por `config["extra_patterns"]`.

Todo achado sai com severidade `CRITICAL` e o valor **mascarado** — nem a mensagem
nem a evidencia descritiva podem repetir a credencial, sob pena de vazar de novo
no log e no trace.
"""

from __future__ import annotations

import re
from typing import Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    compile_pattern,
    config_str,
    config_str_list,
    merge_spans,
    redact_spans,
    redaction_token,
    snippet,
)
from lukato.domain.models.guardrail import (
    GuardrailFinding,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailSeverity,
)
from lukato.domain.types import Json

__all__ = [
    "SECRET_PATTERNS",
    "SecretScanEvaluator",
    "detect_secrets",
    "mask_secret",
    "validate_rule",
]

SECRET_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("openai_api_key", r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"),
    ("aws_access_key_id", r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    ("github_token", r"(?<![A-Za-z0-9_])gh[posu]_[A-Za-z0-9]{16,}"),
    ("private_key_pem", r"-----BEGIN(?: [A-Z]{2,20}){0,4} PRIVATE KEY-----"),
    (
        "jwt",
        r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}",
    ),
    ("bearer_token", r"(?<![A-Za-z0-9])[Bb]earer\s+[A-Za-z0-9._~+/\-]{12,}=*"),
    ("slack_token", r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9\-]{8,}"),
)
"""Catalogo normativo de credenciais reconhecidas (nome do detector -> padrao)."""

_MASK_PREFIX: Final[int] = 4
_COMPILED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(pattern)) for name, pattern in SECRET_PATTERNS
)


def mask_secret(value: str) -> str:
    """Mascara uma credencial preservando apenas o prefixo e o tamanho."""
    flat = " ".join(value.split())
    prefix = flat[:_MASK_PREFIX]
    return f"{prefix}***({len(flat)} caracteres)"


def detect_secrets(
    content: str, extra_patterns: tuple[str, ...] = (), flags: str = ""
) -> list[tuple[str, int, int]]:
    """Localiza credenciais no texto; devolve `(detector, inicio, fim)` ordenado."""
    if not content:
        return []
    found: list[tuple[str, int, int]] = []
    for name, pattern in _COMPILED:
        found.extend(
            (name, match.start(), match.end())
            for match in pattern.finditer(content)
            if match.end() > match.start()
        )
    for index, raw in enumerate(extra_patterns):
        regex = compile_pattern(raw, flags)
        found.extend(
            (f"extra_{index + 1}", match.start(), match.end())
            for match in regex.finditer(content)
            if match.end() > match.start()
        )
    found.sort(key=lambda item: (item[1], item[2]))
    return found


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `secret_scan` (padroes extras opcionais)."""
    extras = config_str_list(rule.config, "extra_patterns", rule_id=rule.id)
    flags = config_str(rule.config, "flags", rule_id=rule.id)
    for pattern in extras:
        compile_pattern(pattern, flags)


class SecretScanEvaluator:
    """`secret_scan`: dispara com severidade critica ao encontrar credenciais."""

    kind: GuardrailRuleKind = GuardrailRuleKind.SECRET_SCAN

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Varre o conteudo e devolve o achado critico com o valor mascarado."""
        validate_rule(rule)
        extras = tuple(config_str_list(rule.config, "extra_patterns", rule_id=rule.id))
        flags = config_str(rule.config, "flags", rule_id=rule.id)
        hits = detect_secrets(content, extras, flags)
        if not hits:
            return None

        spans = merge_spans((start, end) for _, start, end in hits)
        first = spans[0]
        detectors = sorted({name for name, _, _ in hits})
        masked = mask_secret(content[first[0] : first[1]])

        if rule.action in CONTENT_ACTIONS:
            evidence = redact_spans(content, spans, redaction_token(context))
        else:
            evidence = snippet(f"{detectors[0]}: {masked}")
        message = (
            f"Credencial detectada no conteudo ({len(spans)} ocorrencia(s), "
            f"detector(es): {', '.join(detectors)}); primeira: {masked}."
        )
        return build_finding(
            rule, message, evidence=evidence, span=first, severity=GuardrailSeverity.CRITICAL
        )
