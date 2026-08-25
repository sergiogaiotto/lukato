"""Embeddings deterministicos por hashing, sem rede (SPEC-0007 secao 1, ADR-0003).

`HashingEmbedder` e o irmao offline do `QwenEmbedder`. Ele projeta duas familias de
caracteristicas do texto normalizado em `dimensions` posicoes usando `blake2b`:

* **tokens** — capturam coincidencia de palavra inteira;
* **n-gramas de caractere (n=3) com marcas de fronteira** (`^plano$`) — capturam
  coincidencia parcial, e sao o que faz `"50 giga"` cair perto de `"50gb"` em vez de
  ficar tao longe quanto `"bolo de cenoura"`.

Cada caracteristica ganha um indice e um **sinal** derivados do mesmo digest (o truque
do hashing com sinal, que mantem o produto interno nao enviesado), a contagem entra
com amortecimento logaritmico e o vetor final e normalizado em L2. Nao ha relogio,
sorteio nem estado: o mesmo texto produz sempre exatamente o mesmo vetor.

Isto **nao** e qualidade semantica real. O adaptador se identifica como `hashing` em
`model`, em `/health` e no console justamente para que ninguem confunda os dois modos.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterator, Sequence
from functools import lru_cache
from typing import ClassVar, Final

from lukato.config import Settings, get_logger
from lukato.domain.errors import ValidationError
from lukato.domain.services.text_normalizer import normalize

__all__ = [
    "DEFAULT_DIMENSIONS",
    "HASHING_MODEL",
    "NGRAM_SIZE",
    "NGRAM_WEIGHT",
    "TOKEN_WEIGHT",
    "HashingEmbedder",
]

_logger = get_logger(__name__)

HASHING_MODEL: Final[str] = "hashing-local"
"""Nome reportado em `model` — sempre explicito sobre o modo degradado."""

DEFAULT_DIMENSIONS: Final[int] = 1024
"""Dimensao usada quando o adaptador e construido sem `Settings`."""

NGRAM_SIZE: Final[int] = 3
"""Tamanho dos n-gramas de caractere."""

TOKEN_WEIGHT: Final[float] = 1.0
"""Peso base de uma coincidencia de palavra inteira."""

NGRAM_WEIGHT: Final[float] = 0.5
"""Peso base de uma coincidencia parcial de caracteres."""

_DIGEST_SIZE: Final[int] = 8
"""Bytes de digest por caracteristica: 4 para o indice, 1 para o sinal."""

_PROJECTION_CACHE: Final[int] = 65536
"""Tamanho da memoria de projecoes (textos do pipeline repetem muito)."""

_BOUNDARY_START: Final[str] = "^"
_BOUNDARY_END: Final[str] = "$"
_TOKEN_PREFIX: Final[str] = "t:"
_NGRAM_PREFIX: Final[str] = "g:"
_EMPTY_FEATURE: Final[str] = "e:<vazio>"
"""Caracteristica unica de textos sem conteudo aproveitavel.

Devolver o vetor nulo produziria cosseno indefinido no `VectorStorePort`; um vetor
unitario estavel mantem a busca bem definida e continua deterministico.
"""


@lru_cache(maxsize=_PROJECTION_CACHE)
def _project(feature: str, dimensions: int) -> tuple[int, float]:
    """Projeta uma caracteristica em `(indice, sinal)` de forma estavel entre processos."""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=_DIGEST_SIZE).digest()
    index = int.from_bytes(digest[:4], "big") % dimensions
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def _features(text: str) -> Iterator[tuple[str, float]]:
    """Emite `(caracteristica, peso base)` do texto: tokens e n-gramas de caractere."""
    normalized = normalize(text)
    if not normalized:
        yield _EMPTY_FEATURE, TOKEN_WEIGHT
        return
    for token in normalized.split():
        yield f"{_TOKEN_PREFIX}{token}", TOKEN_WEIGHT
        padded = f"{_BOUNDARY_START}{token}{_BOUNDARY_END}"
        for start in range(len(padded) - NGRAM_SIZE + 1):
            yield f"{_NGRAM_PREFIX}{padded[start : start + NGRAM_SIZE]}", NGRAM_WEIGHT


class HashingEmbedder:
    """Provedor de embeddings deterministico para desenvolvimento, testes e offline."""

    provider: ClassVar[str] = "hashing"

    def __init__(self, settings: Settings | None = None, *, dimensions: int | None = None) -> None:
        resolved = dimensions
        if resolved is None:
            resolved = settings.embedding.dimensions if settings is not None else DEFAULT_DIMENSIONS
        if resolved <= 0:
            raise ValidationError(
                "a dimensao do embedding deve ser positiva",
                details={"dimensions": resolved},
            )
        self._dimensions = resolved
        self._settings = settings

    @property
    def model(self) -> str:
        """Identificacao explicita do modo degradado."""
        return HASHING_MODEL

    @property
    def dimensions(self) -> int:
        """Dimensao dos vetores produzidos."""
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Gera um vetor por texto, preservando a ordem de entrada."""
        return [self.vector(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        """Gera o vetor de um unico texto."""
        return self.vector(text)

    async def health(self) -> bool:
        """Sempre saudavel: nao depende de rede, credencial nem disco."""
        return True

    def vector(self, text: str) -> list[float]:
        """Calcula o vetor normalizado em L2 de um texto (funcao pura e sincrona)."""
        counts: Counter[str] = Counter()
        weights: dict[str, float] = {}
        for feature, base_weight in _features(text):
            counts[feature] += 1
            weights[feature] = base_weight
        vector = [0.0] * self._dimensions
        for feature, count in counts.items():
            index, sign = _project(feature, self._dimensions)
            vector[index] += sign * weights[feature] * (1.0 + math.log(count))
        return _l2_normalize(vector)


def _l2_normalize(vector: list[float]) -> list[float]:
    """Normaliza o vetor para norma 1; o vetor nulo (impossivel aqui) e devolvido intacto."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        _logger.debug("hashing_zero_vector")
        return vector
    return [value / norm for value in vector]
