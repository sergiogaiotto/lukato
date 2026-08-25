"""Adaptadores de guardrail: os onze avaliadores da SPEC-0003 e as politicas de seed.

Cada modulo implementa a porta `GuardrailRuleEvaluator` para um `GuardrailRuleKind`
e nunca altera o conteudo por conta propria: quando a acao e `REDACT` ou
`TRANSFORM`, o texto resultante viaja em `finding.evidence` e quem substitui e o
`GuardrailEngine`. Todo o pacote e deterministico e offline — a unica excecao,
`llm_judge`, degrada para `WARN` quando nao ha `LLMPort` injetada.

Uso tipico no *composition root*:

```python
engine = GuardrailEngine(build_default_evaluators(llm, settings=settings),
                         redaction_token=settings.guardrails.redaction_token)
```
"""

from __future__ import annotations

from lukato.adapters.guardrails.composite import (
    RULE_VALIDATORS,
    build_default_evaluators,
    evaluator_kinds,
    validate_policy,
    validate_rule,
)
from lukato.adapters.guardrails.keywords import (
    KeywordBlockEvaluator,
    fold_with_offsets,
    keyword_spans,
)
from lukato.adapters.guardrails.language import (
    SUPPORTED_LANGUAGES,
    UNKNOWN_LANGUAGE,
    LanguageAllowEvaluator,
    detect_language,
)
from lukato.adapters.guardrails.length import (
    CHARS_PER_TOKEN,
    MaxLengthEvaluator,
    MinLengthEvaluator,
    estimate_tokens,
)
from lukato.adapters.guardrails.llm_judge import (
    DEFAULT_TIMEOUT_SECONDS,
    JUDGE_SYSTEM_PROMPT,
    LlmJudgeEvaluator,
)
from lukato.adapters.guardrails.pii import (
    PII_TYPES,
    PiiMatch,
    PiiRedactEvaluator,
    detect_pii,
    is_valid_cnpj,
    is_valid_cpf,
    is_valid_credit_card,
)
from lukato.adapters.guardrails.policies import (
    DEFAULT_JSON_SCHEMA,
    POLICY_SLUGS,
    PROMPT_INJECTION_KEYWORDS,
    SENSITIVE_TOPICS,
    default_policies,
    policy_by_slug,
    policy_id_for,
)
from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    DEFAULT_REDACTION_TOKEN,
    MAX_PATTERN_LENGTH,
    RegexBlockEvaluator,
    RegexRequireEvaluator,
    build_finding,
    compile_pattern,
    redact_spans,
    validate_pattern,
)
from lukato.adapters.guardrails.schema_json import (
    JsonSchemaEvaluator,
    extract_json,
    validate_instance,
)
from lukato.adapters.guardrails.secrets_scan import (
    SECRET_PATTERNS,
    SecretScanEvaluator,
    detect_secrets,
    mask_secret,
)
from lukato.adapters.guardrails.topic import (
    DEFAULT_THRESHOLD,
    TopicBlockEvaluator,
    TopicHit,
    score_topics,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "CONTENT_ACTIONS",
    "DEFAULT_JSON_SCHEMA",
    "DEFAULT_REDACTION_TOKEN",
    "DEFAULT_THRESHOLD",
    "DEFAULT_TIMEOUT_SECONDS",
    "JUDGE_SYSTEM_PROMPT",
    "MAX_PATTERN_LENGTH",
    "PII_TYPES",
    "POLICY_SLUGS",
    "PROMPT_INJECTION_KEYWORDS",
    "RULE_VALIDATORS",
    "SECRET_PATTERNS",
    "SENSITIVE_TOPICS",
    "SUPPORTED_LANGUAGES",
    "UNKNOWN_LANGUAGE",
    "JsonSchemaEvaluator",
    "KeywordBlockEvaluator",
    "LanguageAllowEvaluator",
    "LlmJudgeEvaluator",
    "MaxLengthEvaluator",
    "MinLengthEvaluator",
    "PiiMatch",
    "PiiRedactEvaluator",
    "RegexBlockEvaluator",
    "RegexRequireEvaluator",
    "SecretScanEvaluator",
    "TopicBlockEvaluator",
    "TopicHit",
    "build_default_evaluators",
    "build_finding",
    "compile_pattern",
    "default_policies",
    "detect_language",
    "detect_pii",
    "detect_secrets",
    "estimate_tokens",
    "evaluator_kinds",
    "extract_json",
    "fold_with_offsets",
    "is_valid_cnpj",
    "is_valid_cpf",
    "is_valid_credit_card",
    "keyword_spans",
    "mask_secret",
    "policy_by_slug",
    "policy_id_for",
    "redact_spans",
    "score_topics",
    "validate_instance",
    "validate_pattern",
    "validate_policy",
    "validate_rule",
]
