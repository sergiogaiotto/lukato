"""Funcoes puras de normalizacao e comparacao lexica de texto.

Base compartilhada pelos avaliadores de guardrail (SPEC-0003) e pelo motor de
matching do AdWatch (SPEC-0010). Nada aqui faz I/O nem depende de configuracao:
a mesma entrada sempre produz a mesma saida.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from functools import lru_cache

from lukato.domain.errors import ValidationError

__all__ = [
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

_CACHE_SIZE = 4096
"""Tamanho das memorias de normalizacao (textos repetem muito no pipeline)."""

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PARTIAL_WORD = re.compile(r"\s+\S*\Z")


@lru_cache(maxsize=_CACHE_SIZE)
def strip_accents(text: str) -> str:
    """Remove diacriticos por decomposicao NFKD, preservando os demais caracteres."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@lru_cache(maxsize=_CACHE_SIZE)
def normalize(text: str) -> str:
    """Normaliza para comparacao: minusculas, sem acento, sem pontuacao, espacos colapsados.

    Qualquer caractere que nao seja alfanumerico vira espaco, e os espacos sao
    colapsados em um unico separador (sem espacos nas pontas).
    """
    folded = strip_accents(text).casefold()
    cleaned = "".join(char if (char.isalnum() or char.isspace()) else " " for char in folded)
    return _WHITESPACE.sub(" ", cleaned).strip()


@lru_cache(maxsize=_CACHE_SIZE)
def _token_tuple(text: str) -> tuple[str, ...]:
    """Tokens normalizados em forma imutavel (o que permite memorizar o resultado)."""
    normalized = normalize(text)
    return tuple(normalized.split()) if normalized else ()


def tokenize(text: str) -> list[str]:
    """Divide o texto normalizado em tokens separados por espaco."""
    return list(_token_tuple(text))


def ngrams(tokens: Sequence[str], n: int) -> list[tuple[str, ...]]:
    """Gera os n-gramas contiguos de uma sequencia de tokens."""
    if n < 1:
        raise ValidationError("O tamanho do n-grama deve ser >= 1.", details={"n": n})
    if len(tokens) < n:
        return []
    return [tuple(tokens[index : index + n]) for index in range(len(tokens) - n + 1)]


@lru_cache(maxsize=_CACHE_SIZE)
def _char_ngram_tuple(text: str, n: int) -> tuple[str, ...]:
    """N-gramas de caracteres do texto ja normalizado, em forma imutavel."""
    normalized = normalize(text)
    if len(normalized) < n:
        return ()
    return tuple(normalized[index : index + n] for index in range(len(normalized) - n + 1))


def char_ngrams(text: str, n: int) -> list[str]:
    """Gera os n-gramas de caracteres sobre o texto normalizado."""
    if n < 1:
        raise ValidationError("O tamanho do n-grama deve ser >= 1.", details={"n": n})
    return list(_char_ngram_tuple(text, n))


def jaccard(a: set[str], b: set[str]) -> float:
    """Similaridade de Jaccard entre dois conjuntos.

    Conjuntos sem uniao (ambos vazios) devolvem `0.0` — no matching, ausencia de
    evidencia nunca pode virar similaridade maxima.
    """
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if intersection == 0:
        return 0.0
    return intersection / len(a | b)


def lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    """Comprimento da maior subsequencia comum entre duas sequencias de tokens."""
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for item_a in a:
        current = [0] * (len(b) + 1)
        for index_b, item_b in enumerate(b, start=1):
            if item_a == item_b:
                current[index_b] = previous[index_b - 1] + 1
            else:
                current[index_b] = max(previous[index_b], current[index_b - 1])
        previous = current
    return previous[-1]


def lcs_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """Fracao de `a` que aparece em `b` na ordem esperada (LCS / len(a))."""
    if not a:
        return 0.0
    return lcs_length(a, b) / len(a)


def truncate_words(text: str, max_chars: int) -> str:
    """Corta o texto em no maximo `max_chars`, respeitando a fronteira de palavra.

    Quando a primeira palavra ja excede o limite nao existe fronteira util e o
    corte e feito no caractere exato — melhor que devolver texto vazio.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    if text[max_chars].isspace():
        return window.rstrip()
    trimmed = _TRAILING_PARTIAL_WORD.sub("", window).rstrip()
    return trimmed or window.rstrip()


def clear_caches() -> None:
    """Esvazia as memorias de normalizacao (util em testes e em rotinas longas)."""
    strip_accents.cache_clear()
    normalize.cache_clear()
    _token_tuple.cache_clear()
    _char_ngram_tuple.cache_clear()
