"""Avaliador `language_allow`: restringe o idioma do conteudo, sem dependencia externa.

A deteccao e uma heuristica deliberadamente simples e deterministica: contagem de
palavras funcionais (stopwords) de cada idioma sobre o texto normalizado, somada a
bonus por marcadores ortograficos exclusivos (`ã`, `õ`, `ç` em portugues; `ñ`, `¿`,
`¡` em espanhol; contracoes `'s`/`'t` em ingles). A confianca e a participacao do
idioma vencedor no total de evidencias, atenuada quando ha pouca evidencia — texto
curto demais devolve confianca baixa e, por padrao, **nao** vira violacao.
"""

from __future__ import annotations

from typing import Final

from lukato.adapters.guardrails.regex_rules import (
    CONTENT_ACTIONS,
    build_finding,
    config_float,
    config_str_list,
    snippet,
)
from lukato.domain.errors import ValidationError
from lukato.domain.models.guardrail import (
    GuardrailFinding,
    GuardrailRule,
    GuardrailRuleKind,
)
from lukato.domain.services.text_normalizer import tokenize
from lukato.domain.types import Json

__all__ = [
    "SUPPORTED_LANGUAGES",
    "UNKNOWN_LANGUAGE",
    "LanguageAllowEvaluator",
    "detect_language",
    "language_scores",
    "validate_rule",
]

UNKNOWN_LANGUAGE: Final[str] = "unknown"
"""Rotulo devolvido quando nao ha evidencia suficiente para decidir."""

# Stopwords escritas ja sem acento: a normalizacao do dominio remove diacriticos.
_STOPWORDS: Final[dict[str, frozenset[str]]] = {
    "pt": frozenset(
        """a ao aos as até com como da das de dela dele depois do dos e ela ele eles em entre era
        essa esse esta este eu foi fomos for foram isso isto ja lhe mais mas me mesmo meu minha
        muito na nao nas nem no nos nossa numa o os ou para pela pelas pelo pelos por porque qual
        quando que quem se sem ser seu seus so sua suas tambem te tem tinha tu um uma voce voces
        vos entao onde entretanto porem alem assim cada coisa deve devem esta estao estamos fazer
        fez ficar isso mesma pode podem precisa qualquer sao seja sobre todos toda tudo""".split()
    ),
    "en": frozenset(
        """a about after all also an and any are as at be because been but by can could did do
        does for from get had has have he her here his how i if in into is it its just like make
        me more most my no not of on one only or other our out over said same she should so some
        such than that the their them then there these they this those to too up us use very was
        we were what when where which while who will with would you your""".split()
    ),
    "es": frozenset(
        """al algo algunos ante antes aunque cada como con contra cual cuando de del desde donde
        dos el ella ellos en entre era eran es esa ese eso esta estan este esto fue fueron ha
        hace hasta hay la las le les lo los mas me mi mucho muy nada ni no nos nuestro o otro
        para pero poco por porque que quien se sea segun ser si siempre sin sobre solo su sus
        tambien tanto te tiene todo todos tu un una uno unos usted ya yo""".split()
    ),
}

SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = tuple(sorted(_STOPWORDS))
"""Idiomas que a heuristica sabe distinguir."""

_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    "pt": ("ã", "õ", "ç", "ções", "ão"),
    "es": ("ñ", "¿", "¡", "ción"),
    "en": ("'s ", "'t ", "'re ", "'ve "),
}
_MARKER_WEIGHT: Final[float] = 1.5
"""Peso, em "stopwords virtuais", de cada marcador ortografico encontrado."""

_FULL_EVIDENCE: Final[float] = 3.0
"""Volume de evidencia a partir do qual a confianca deixa de ser atenuada."""

_MAX_MARKER_HITS: Final[int] = 4
"""Teto de marcadores contados por idioma (evita um texto longo dominar tudo)."""


def language_scores(text: str) -> dict[str, float]:
    """Pontua cada idioma suportado em "stopwords equivalentes" encontradas."""
    tokens = tokenize(text)
    scores: dict[str, float] = {}
    lowered = text.casefold()
    for language, stopwords in _STOPWORDS.items():
        hits = float(sum(1 for token in tokens if token in stopwords))
        markers = sum(1 for marker in _MARKERS.get(language, ()) if marker in lowered)
        hits += _MARKER_WEIGHT * min(markers, _MAX_MARKER_HITS)
        scores[language] = hits
    return scores


def detect_language(text: str) -> tuple[str, float]:
    """Detecta o idioma dominante e a confianca (0..1) da deteccao."""
    if not text.strip():
        return UNKNOWN_LANGUAGE, 0.0
    scores = language_scores(text)
    total = sum(scores.values())
    if total <= 0.0:
        return UNKNOWN_LANGUAGE, 0.0
    language = max(sorted(scores), key=lambda name: scores[name])
    share = scores[language] / total
    coverage = min(1.0, total / _FULL_EVIDENCE)
    return language, round(share * coverage, 4)


def validate_rule(rule: GuardrailRule) -> None:
    """Valida a config de uma regra `language_allow`."""
    languages = config_str_list(rule.config, "languages", rule_id=rule.id)
    if not languages:
        raise ValidationError(
            f"A regra language_allow '{rule.id}' exige 'languages' com ao menos um idioma.",
            details={"rule_id": rule.id, "supported": list(SUPPORTED_LANGUAGES)},
        )
    unknown = sorted({name for name in languages if name.lower() not in _STOPWORDS})
    if unknown:
        raise ValidationError(
            f"A regra language_allow '{rule.id}' cita idiomas nao suportados: "
            f"{', '.join(unknown)}.",
            details={
                "rule_id": rule.id,
                "unknown": unknown,
                "supported": list(SUPPORTED_LANGUAGES),
            },
        )
    config_float(rule.config, "min_confidence", rule_id=rule.id, minimum=0.0, maximum=1.0)


class LanguageAllowEvaluator:
    """`language_allow`: dispara quando o idioma detectado nao esta na lista permitida."""

    kind: GuardrailRuleKind = GuardrailRuleKind.LANGUAGE_ALLOW

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Detecta o idioma e compara com os permitidos, respeitando a confianca minima."""
        validate_rule(rule)
        allowed = {name.lower() for name in config_str_list(rule.config, "languages")}
        minimum = config_float(
            rule.config, "min_confidence", rule_id=rule.id, default=0.5, minimum=0.0, maximum=1.0
        )

        language, confidence = detect_language(content)
        if language in allowed:
            return None
        if confidence < minimum:
            # Evidencia fraca nunca bloqueia: o falso positivo custaria mais caro.
            return None

        message = (
            f"Idioma detectado '{language}' (confianca {confidence:.2f}) fora dos "
            f"permitidos: {', '.join(sorted(allowed))}."
        )
        evidence = content if rule.action in CONTENT_ACTIONS else snippet(
            f"{language}:{confidence:.2f}"
        )
        return build_finding(rule, message, evidence=evidence)
