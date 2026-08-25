"""Motor de matching temporal do AdWatch: janelas, sinais, fusao, NMS e refino.

Implementa a secao 3 da SPEC-0010 (estagios normativos) e a secao 8 da SPEC-0000.
Tudo aqui e dominio puro: sem I/O, sem rede, sem numpy — apenas stdlib e pydantic.
O unico opcional e `rapidfuzz`, importado de forma preguicosa e com irmao
deterministico em `difflib` quando a biblioteca nao esta instalada.
"""

from __future__ import annotations

import difflib
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from types import ModuleType
from typing import Final, Protocol, TypeVar

from pydantic import Field

from lukato.domain.errors import ValidationError
from lukato.domain.models.adwatch import (
    AdFingerprint,
    DetectionCandidate,
    DetectionEvidence,
    DetectionStatus,
    OcrText,
    SceneCut,
    Transcript,
)
from lukato.domain.models.base import DomainModel
from lukato.domain.services.text_normalizer import jaccard, lcs_ratio, normalize, tokenize

__all__ = [
    "DEFAULT_ACCEPT_THRESHOLD",
    "DEFAULT_IOU_THRESHOLD",
    "DEFAULT_MAX_SHIFT",
    "DEFAULT_MIN_WORDS",
    "DEFAULT_ORDER_PENALTY",
    "DEFAULT_ORDER_THRESHOLD",
    "DEFAULT_REVIEW_THRESHOLD",
    "DEFAULT_WINDOW_SIZES",
    "DEFAULT_WINDOW_STRIDE",
    "WEIGHT_SUM_TOLERANCE",
    "BoundaryRefiner",
    "CandidateBuilder",
    "LexicalMatcher",
    "NonMaximumSuppression",
    "OrderMatcher",
    "ScoreFusion",
    "SemanticMatcher",
    "SlidingWindowBuilder",
    "TextWindow",
]

DEFAULT_WINDOW_SIZES: Final[tuple[float, ...]] = (15.0, 30.0, 60.0)
"""Tamanhos de janela deslizante em segundos (SPEC-0010 secao 3.3)."""

DEFAULT_WINDOW_STRIDE: Final[float] = 5.0
"""Passo entre janelas consecutivas, em segundos."""

DEFAULT_MIN_WORDS: Final[int] = 3
"""Minimo de palavras transcritas para uma janela ser considerada."""

DEFAULT_ORDER_THRESHOLD: Final[float] = 0.7
"""Limiar de `order_ratio` a partir do qual a ordem temporal e aceita."""

DEFAULT_ORDER_PENALTY: Final[float] = 0.85
"""Fator multiplicativo aplicado ao score quando `order_ok` e falso."""

DEFAULT_ACCEPT_THRESHOLD: Final[float] = 0.90
"""Score minimo para aceitar sem juiz multimodal."""

DEFAULT_REVIEW_THRESHOLD: Final[float] = 0.60
"""Score minimo para encaminhar a revisao (abaixo disso, rejeita)."""

DEFAULT_IOU_THRESHOLD: Final[float] = 0.5
"""Sobreposicao temporal a partir da qual dois candidatos sao fundidos."""

DEFAULT_MAX_SHIFT: Final[float] = 3.0
"""Deslocamento maximo, em segundos, permitido no refino de fronteira."""

WEIGHT_SUM_TOLERANCE: Final[float] = 1e-6
"""Tolerancia da soma dos pesos de fusao (a soma deve valer 1.0)."""

_TIME_PRECISION: Final[int] = 3
"""Casas decimais usadas para estabilizar fronteiras de janela."""


class _TimeSpan(Protocol):
    """Qualquer objeto com marcacao temporal `[start, end]`."""

    start: float
    end: float


_SpanT = TypeVar("_SpanT", bound=_TimeSpan)
"""Qualquer modelo temporal fatiavel por intervalo (palavra transcrita, OCR)."""


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Limita `value` ao intervalo `[low, high]`."""
    return max(low, min(high, value))


def _round_time(value: float) -> float:
    """Arredonda um instante para evitar deriva de ponto flutuante."""
    return round(value, _TIME_PRECISION)


def _overlapping(
    items: Sequence[_SpanT],
    starts: Sequence[float],
    max_span: float,
    begin: float,
    end: float,
) -> list[_SpanT]:
    """Seleciona os itens (ordenados por `start`) que intersectam `[begin, end]`."""
    if not items:
        return []
    low = bisect_left(starts, begin - max_span)
    high = bisect_right(starts, end)
    return [item for item in items[low:high] if item.end >= begin]


class TextWindow(DomainModel):
    """Janela temporal com o texto ASR e o texto OCR do mesmo intervalo."""

    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str
    ocr_text: str = ""

    @property
    def duration(self) -> float:
        """Duracao da janela em segundos (nunca negativa)."""
        return max(0.0, self.end - self.start)


class SlidingWindowBuilder:
    """Gera janelas deslizantes multiescala sobre a linha do tempo da transcricao."""

    def __init__(
        self,
        sizes: Sequence[float] = DEFAULT_WINDOW_SIZES,
        stride: float = DEFAULT_WINDOW_STRIDE,
        min_words: int = DEFAULT_MIN_WORDS,
    ) -> None:
        """Valida os parametros de janelamento (SPEC-0010 secao 3.3)."""
        valid = sorted({float(size) for size in sizes if float(size) > 0.0})
        if not valid:
            raise ValidationError(
                "SlidingWindowBuilder exige ao menos um tamanho de janela positivo",
                details={"sizes": list(sizes)},
            )
        if stride <= 0.0:
            raise ValidationError(
                "o passo (stride) da janela deslizante deve ser positivo",
                details={"stride": stride},
            )
        if min_words < 0:
            raise ValidationError(
                "min_words nao pode ser negativo", details={"min_words": min_words}
            )
        self.sizes: tuple[float, ...] = tuple(valid)
        self.stride = float(stride)
        self.min_words = int(min_words)

    def build(self, transcript: Transcript, *, ocr: Sequence[OcrText] = ()) -> list[TextWindow]:
        """Constroi as janelas `[t, t + size]` cobrindo a transcricao inteira.

        Janelas com menos de `min_words` palavras sao descartadas e janelas com o
        mesmo par `(start, end)` aparecem uma unica vez.
        """
        words = sorted(transcript.words, key=lambda word: (word.start, word.end))
        if not words:
            return []
        word_starts = [word.start for word in words]
        word_span = max((word.end - word.start for word in words), default=0.0)
        first = min(word_starts)
        last = max(word.end for word in words)

        ocr_items = sorted(ocr, key=lambda item: (item.start, item.end))
        ocr_starts = [item.start for item in ocr_items]
        ocr_span = max((item.end - item.start for item in ocr_items), default=0.0)

        windows: dict[tuple[float, float], TextWindow] = {}
        for size in self.sizes:
            for start in self._starts(first, last):
                end = _round_time(start + size)
                key = (start, end)
                if key in windows:
                    continue
                selected = _overlapping(words, word_starts, word_span, start, end)
                if len(selected) < self.min_words:
                    continue
                spoken = " ".join(word.word.strip() for word in selected if word.word.strip())
                screen = _overlapping(ocr_items, ocr_starts, ocr_span, start, end)
                windows[key] = TextWindow(
                    start=start,
                    end=end,
                    text=spoken,
                    ocr_text=" ".join(item.text.strip() for item in screen if item.text.strip()),
                )
        return sorted(windows.values(), key=lambda window: (window.start, window.end))

    def _starts(self, first: float, last: float) -> list[float]:
        """Instantes iniciais das janelas, do primeiro ao ultimo timestamp de palavra."""
        span = max(0.0, last - first)
        steps = max(1, math.ceil(span / self.stride))
        origin = max(0.0, first)
        return [_round_time(origin + index * self.stride) for index in range(steps)]


class LexicalMatcher:
    """Similaridade lexica entre textos normalizados, com fallback puro-Python."""

    def __init__(self) -> None:
        """Detecta `rapidfuzz`; sem ele, opera no backend `difflib` + Jaccard."""
        fuzz: ModuleType | None
        try:
            from rapidfuzz import fuzz as rapidfuzz_fuzz
        except ImportError:
            fuzz = None
        else:
            fuzz = rapidfuzz_fuzz
        self._fuzz = fuzz
        self.backend: str = "rapidfuzz" if fuzz is not None else "difflib"

    def score(self, a: str, b: str) -> float:
        """Similaridade lexica em `[0, 1]` entre dois textos (normalizados internamente)."""
        left = normalize(a)
        right = normalize(b)
        if not left or not right:
            return 0.0
        if self._fuzz is not None:
            best = max(
                self._fuzz.token_set_ratio(left, right),
                self._fuzz.token_sort_ratio(left, right),
                self._fuzz.partial_ratio(left, right),
            )
            return _clamp(float(best) / 100.0)
        ratio = difflib.SequenceMatcher(None, left, right).ratio()
        overlap = jaccard(set(tokenize(left)), set(tokenize(right)))
        return _clamp(max(float(ratio), float(overlap)))

    def best_keyword_score(self, text: str, keywords: Sequence[str]) -> float:
        """Melhor similaridade entre `text` e qualquer uma das `keywords`."""
        haystack = normalize(text)
        if not haystack or not keywords:
            return 0.0
        padded = f" {haystack} "
        best = 0.0
        for keyword in keywords:
            needle = normalize(keyword)
            if not needle:
                continue
            if f" {needle} " in padded:
                return 1.0
            best = max(best, self.score(haystack, needle))
        return _clamp(best)


class SemanticMatcher:
    """Similaridade semantica por cosseno, em Python puro (sem numpy no dominio)."""

    @staticmethod
    def cosine(a: Sequence[float], b: Sequence[float]) -> float:
        """Cosseno em `[-1, 1]`; `0.0` quando algum vetor e vazio, nulo ou de outra dimensao."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(math.fsum(x * x for x in a))
        norm_b = math.sqrt(math.fsum(y * y for y in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return _clamp(dot / (norm_a * norm_b), -1.0, 1.0)

    @staticmethod
    def similarity(a: Sequence[float], b: Sequence[float]) -> float:
        """Cosseno reescalado de `[-1, 1]` para `[0, 1]` (SPEC-0010 secao 3.5)."""
        return _clamp((SemanticMatcher.cosine(a, b) + 1.0) / 2.0)

    def rank(
        self,
        query_vec: Sequence[float],
        candidates: Mapping[str, Sequence[float]],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """Ordena os candidatos por similaridade decrescente e devolve os `top_k` melhores."""
        if top_k <= 0 or not candidates:
            return []
        scored = [(key, self.similarity(query_vec, vector)) for key, vector in candidates.items()]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:top_k]


class OrderMatcher:
    """Verifica se as ancoras do comercial aparecem na ordem esperada na janela."""

    def __init__(self, threshold: float = DEFAULT_ORDER_THRESHOLD) -> None:
        """Guarda o limiar de `order_ratio` (SPEC-0010 secao 3.5)."""
        if not 0.0 <= threshold <= 1.0:
            raise ValidationError(
                "o limiar de ordem deve estar em [0, 1]", details={"threshold": threshold}
            )
        self.threshold = float(threshold)

    def evaluate(self, window_text: str, anchors: Sequence[str]) -> tuple[float, bool]:
        """Devolve `(order_ratio, order_ok)` para o texto da janela.

        As ancoras (`key_phrases`/`keywords`) sao reduzidas a tokens unicos na ordem
        esperada; monta-se a sequencia de indices de primeira ocorrencia das ancoras
        encontradas e mede-se o quanto ela e crescente (LCS contra a ordem esperada,
        dividido pelo numero de ancoras encontradas). Sem ancoras — ou sem nenhuma
        ancora presente no texto — a ordem nada afirma e o resultado e `(1.0, True)`.
        """
        expected = self._anchor_tokens(anchors)
        if not expected:
            return 1.0, True
        tokens = tokenize(window_text)
        positions: dict[str, int] = {}
        for index, token in enumerate(tokens):
            positions.setdefault(token, index)
        found = [token for token in expected if token in positions]
        if not found:
            return 1.0, True
        observed = sorted(found, key=lambda token: positions[token])
        ratio = _clamp(lcs_ratio(observed, found))
        return ratio, ratio >= self.threshold

    @staticmethod
    def _anchor_tokens(anchors: Iterable[str]) -> list[str]:
        """Achata as ancoras em tokens unicos preservando a ordem esperada."""
        tokens: list[str] = []
        for anchor in anchors:
            tokens.extend(tokenize(anchor))
        return list(dict.fromkeys(tokens))


class ScoreFusion:
    """Combina os sinais de evidencia no score final `S` e classifica a deteccao."""

    def __init__(
        self,
        *,
        weight_lexical: float = 0.40,
        weight_semantic: float = 0.25,
        weight_ocr: float = 0.15,
        weight_visual: float = 0.15,
        weight_duration: float = 0.05,
        order_penalty: float = DEFAULT_ORDER_PENALTY,
    ) -> None:
        """Valida que os pesos somam 1.0 (tolerancia `WEIGHT_SUM_TOLERANCE`)."""
        weights = {
            "lexical": float(weight_lexical),
            "semantic": float(weight_semantic),
            "ocr": float(weight_ocr),
            "visual": float(weight_visual),
            "duration": float(weight_duration),
        }
        invalid = {name: value for name, value in weights.items() if not 0.0 <= value <= 1.0}
        if invalid:
            raise ValidationError(
                "todo peso de fusao deve estar em [0, 1]", details={"weights": invalid}
            )
        total = math.fsum(weights.values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValidationError(
                f"a soma dos pesos de fusao deve ser 1.0 +/- {WEIGHT_SUM_TOLERANCE}, "
                f"obtido {total:.6f}",
                details={"weights": weights, "total": total},
            )
        if not 0.0 <= order_penalty <= 1.0:
            raise ValidationError(
                "a penalidade de ordem deve estar em [0, 1]",
                details={"order_penalty": order_penalty},
            )
        self.weight_lexical = weights["lexical"]
        self.weight_semantic = weights["semantic"]
        self.weight_ocr = weights["ocr"]
        self.weight_visual = weights["visual"]
        self.weight_duration = weights["duration"]
        self.order_penalty = float(order_penalty)

    def weights(self) -> dict[str, float]:
        """Pesos de fusao por sinal, na ordem normativa da SPEC-0010."""
        return {
            "lexical": self.weight_lexical,
            "semantic": self.weight_semantic,
            "ocr": self.weight_ocr,
            "visual": self.weight_visual,
            "duration": self.weight_duration,
        }

    def duration_score(self, window_duration: float, expected_duration: float) -> float:
        """`1 - min(1, |dur_janela - duracao_esperada| / max(duracao_esperada, 1))`."""
        reference = max(float(expected_duration), 1.0)
        deviation = abs(float(window_duration) - float(expected_duration)) / reference
        return _clamp(1.0 - min(1.0, deviation))

    def fuse(self, evidence: DetectionEvidence) -> float:
        """Aplica a formula normativa de fusao e a penalidade de ordem."""
        score = math.fsum(
            (
                self.weight_lexical * _clamp(evidence.speech_match),
                self.weight_semantic * _clamp(evidence.semantic_match),
                self.weight_ocr * _clamp(evidence.ocr_match),
                self.weight_visual * _clamp(evidence.visual_match),
                self.weight_duration * _clamp(evidence.duration_match),
            )
        )
        if not evidence.order_ok:
            score *= self.order_penalty
        return _clamp(score)

    def classify(
        self,
        score: float,
        *,
        accept: float = DEFAULT_ACCEPT_THRESHOLD,
        review: float = DEFAULT_REVIEW_THRESHOLD,
    ) -> DetectionStatus:
        """Traduz o score em `DetectionStatus` segundo os limiares da SPEC-0010."""
        if review > accept:
            raise ValidationError(
                "review_threshold deve ser menor ou igual a accept_threshold",
                details={"accept": accept, "review": review},
            )
        if score >= accept:
            return DetectionStatus.ACCEPTED
        if score >= review:
            return DetectionStatus.NEEDS_REVIEW
        return DetectionStatus.REJECTED


class NonMaximumSuppression:
    """Supressao temporal de sobreposicoes entre candidatos do mesmo comercial."""

    @staticmethod
    def iou(a: tuple[float, float], b: tuple[float, float]) -> float:
        """Intersecao sobre uniao de dois intervalos temporais."""
        intersection = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
        union = (a[1] - a[0]) + (b[1] - b[0]) - intersection
        if union <= 0.0:
            return 1.0 if a[0] == b[0] and a[1] == b[1] else 0.0
        return _clamp(intersection / union)

    @staticmethod
    def suppress(
        candidates: Sequence[DetectionCandidate], *, iou_threshold: float = DEFAULT_IOU_THRESHOLD
    ) -> list[DetectionCandidate]:
        """Funde candidatos do mesmo comercial com IoU acima do limiar.

        Mantem o candidato de maior score e expande o intervalo para a uniao dos
        candidatos absorvidos. A saida sai ordenada por `start`.
        """
        if not 0.0 <= iou_threshold <= 1.0:
            raise ValidationError(
                "o limiar de IoU deve estar em [0, 1]", details={"iou_threshold": iou_threshold}
            )
        if not candidates:
            return []
        groups: dict[str, list[DetectionCandidate]] = defaultdict(list)
        for candidate in candidates:
            groups[candidate.commercial_id].append(candidate)

        kept: list[DetectionCandidate] = []
        for group in groups.values():
            pending = sorted(group, key=lambda item: (-item.score, item.start, item.end))
            while pending:
                best = pending[0]
                pending = pending[1:]
                start, end = best.start, best.end
                absorbed = True
                while absorbed:
                    absorbed = False
                    survivors: list[DetectionCandidate] = []
                    for other in pending:
                        if (
                            NonMaximumSuppression.iou((start, end), (other.start, other.end))
                            > iou_threshold
                        ):
                            start = min(start, other.start)
                            end = max(end, other.end)
                            absorbed = True
                        else:
                            survivors.append(other)
                    pending = survivors
                if start != best.start or end != best.end:
                    best = best.model_copy(update={"start": start, "end": end})
                kept.append(best)
        kept.sort(key=lambda item: (item.start, item.end, -item.score))
        return kept


class BoundaryRefiner:
    """Encaixa as fronteiras de uma deteccao nos cortes de cena mais proximos."""

    def __init__(self, max_shift: float = DEFAULT_MAX_SHIFT) -> None:
        """Guarda o deslocamento maximo tolerado, em segundos."""
        if max_shift < 0.0:
            raise ValidationError(
                "max_shift nao pode ser negativo", details={"max_shift": max_shift}
            )
        self.max_shift = float(max_shift)

    def refine(
        self, start: float, end: float, cuts: Sequence[SceneCut]
    ) -> tuple[float, float, bool]:
        """Devolve `(start, end, refinado)` apos tentar encaixar nos cortes de cena."""
        boundaries = sorted({value for cut in cuts for value in (cut.start, cut.end)})
        if not boundaries:
            return start, end, False
        new_start = self._snap(start, boundaries)
        new_end = self._snap(end, boundaries)
        if new_end <= new_start:
            return start, end, False
        refined = new_start != start or new_end != end
        return new_start, new_end, refined

    def _snap(self, value: float, boundaries: Sequence[float]) -> float:
        """Aproxima `value` da fronteira mais proxima dentro de `max_shift`."""
        position = bisect_left(boundaries, value)
        best = value
        distance = self.max_shift
        for index in (position - 1, position):
            if 0 <= index < len(boundaries):
                candidate = boundaries[index]
                gap = abs(candidate - value)
                if gap <= distance and (gap < distance or candidate < best):
                    best = candidate
                    distance = gap
        return best


class CandidateBuilder:
    """Orquestra os sinais de matching de uma janela contra um fingerprint (sem I/O)."""

    def __init__(
        self,
        *,
        lexical: LexicalMatcher,
        semantic: SemanticMatcher,
        order: OrderMatcher,
        fusion: ScoreFusion,
    ) -> None:
        """Recebe os matchers ja configurados pelo caso de uso."""
        self.lexical = lexical
        self.semantic = semantic
        self.order = order
        self.fusion = fusion

    def evaluate(
        self,
        window: TextWindow,
        fingerprint: AdFingerprint,
        *,
        window_vec: Sequence[float] | None = None,
        visual_score: float | None = None,
    ) -> tuple[float, DetectionEvidence]:
        """Calcula `(score, evidencia)` da janela contra o fingerprint.

        Quando `visual_score` e `None` nao houve juiz multimodal: `visual_match`
        herda `speech_match` como proxy conservador — nunca `1.0` inventado — e a
        ausencia do juiz e sinalizada por `verified_by_vlm=False` na `Detection`
        resultante (SPEC-0010 secao 3.5).
        """
        speech = self.lexical.score(window.text, fingerprint.normalized_text)
        semantic = 0.0
        if window_vec and fingerprint.embedding:
            semantic = self.semantic.similarity(window_vec, fingerprint.embedding)
        ocr = 0.0
        if window.ocr_text.strip():
            ocr = max(
                self.lexical.score(window.ocr_text, fingerprint.normalized_text),
                self.lexical.best_keyword_score(window.ocr_text, fingerprint.keywords),
            )
        visual = speech if visual_score is None else _clamp(float(visual_score))
        duration = self.fusion.duration_score(window.duration, fingerprint.duration)
        anchors = fingerprint.key_phrases or fingerprint.keywords
        _, order_ok = self.order.evaluate(window.text, anchors)
        evidence = DetectionEvidence(
            speech_match=speech,
            semantic_match=semantic,
            ocr_match=ocr,
            visual_match=visual,
            duration_match=duration,
            order_ok=order_ok,
            brand_detected=self._brand(window, fingerprint),
            matched_text=window.text,
        )
        score = self.fusion.fuse(evidence)
        return score, evidence

    @staticmethod
    def _brand(window: TextWindow, fingerprint: AdFingerprint) -> str | None:
        """Reporta a marca esperada quando ela aparece na fala ou na tela."""
        brand = normalize(fingerprint.expected_brand)
        if not brand:
            return None
        haystack = f" {normalize(window.text)} {normalize(window.ocr_text)} "
        return fingerprint.expected_brand if f" {brand} " in haystack else None
