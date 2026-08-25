"""Servicos de dominio do lukato: logica pura, sem I/O e sem dependencia externa.

Reune o motor de guardrails, a calculadora de custo, o compositor da trinca dos
modulos e as funcoes de normalizacao de texto compartilhadas pelo ecossistema.
"""

from __future__ import annotations

from lukato.domain.services.cost_calculator import BudgetCheck, CostCalculator
from lukato.domain.services.guardrail_engine import GuardrailEngine
from lukato.domain.services.module_composer import ComposedPipeline, ModuleComposer
from lukato.domain.services.text_normalizer import (
    char_ngrams,
    clear_caches,
    jaccard,
    lcs_length,
    lcs_ratio,
    ngrams,
    normalize,
    strip_accents,
    tokenize,
    truncate_words,
)

__all__ = [
    "BudgetCheck",
    "ComposedPipeline",
    "CostCalculator",
    "GuardrailEngine",
    "ModuleComposer",
    "char_ngrams",
    "clear_caches",
    "jaccard",
    "lcs_length",
    "lcs_ratio",
    "ngrams",
    "normalize",
    "strip_accents",
    "tokenize",
    "truncate_words",
]
